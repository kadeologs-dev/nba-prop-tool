"""
NBA Prop Confluence Tool - Fixed Version
Scores player props (Points, Rebounds, Assists) using your confluence system.
Run: python app.py  -- then open http://localhost:5000 in your browser
"""

from flask import Flask, render_template, jsonify
from nba_api.stats.endpoints import leaguedashteamstats, leaguedashplayerstats
from nba_api.stats.static import teams as nba_teams_static
import pandas as pd
import time
import traceback
import os

app = Flask(__name__)

SEASON = "2025-26"

# Build abbreviation lookup from static data (always available, no API needed)
TEAM_ABBR_MAP = {t["id"]: t["abbreviation"] for t in nba_teams_static.get_teams()}
TEAM_NAME_TO_ABBR = {t["full_name"].upper(): t["abbreviation"] for t in nba_teams_static.get_teams()}
ABBR_TO_ID = {t["abbreviation"].upper(): t["id"] for t in nba_teams_static.get_teams()}

# ---------------------------------------------------------------------------
# Helper: safe API call with retry
# ---------------------------------------------------------------------------
def safe_call(fn, **kwargs):
    for attempt in range(3):
        try:
            time.sleep(0.8)
            return fn(**kwargs).get_data_frames()[0]
        except Exception as e:
            if attempt == 2:
                print(f"[ERROR] {fn.__name__}: {e}")
                return pd.DataFrame()
            time.sleep(2)

# ---------------------------------------------------------------------------
# Load league-wide team stats (ORtg, DRtg, Pace)
# ---------------------------------------------------------------------------
def get_team_stats():
    df = safe_call(leaguedashteamstats.LeagueDashTeamStats,
                   season=SEASON,
                   per_mode_detailed="PerGame",
                   measure_type_detailed_defense="Advanced")
    if df.empty:
        return {}

    base = safe_call(leaguedashteamstats.LeagueDashTeamStats,
                     season=SEASON,
                     per_mode_detailed="PerGame",
                     measure_type_detailed_defense="Base")

    result = {}
    for _, row in df.iterrows():
        tid = row["TEAM_ID"]
        abbr = TEAM_ABBR_MAP.get(tid, "")
        base_row = base[base["TEAM_ID"] == tid] if not base.empty else pd.DataFrame()
        opp_pts = float(base_row["OPP_PTS"].values[0]) if not base_row.empty and "OPP_PTS" in base_row.columns else 114.0

        result[tid] = {
            "team_name": row["TEAM_NAME"],
            "team_abbr": abbr,
            "pace": float(row.get("PACE", 98)),
            "off_rtg": float(row.get("OFF_RATING", 114)),
            "def_rtg": float(row.get("DEF_RATING", 114)),
            "opp_pts": opp_pts,
        }

    # Add league ranks
    sorted_pace = sorted(result.items(), key=lambda x: x[1]["pace"], reverse=True)
    sorted_def  = sorted(result.items(), key=lambda x: x[1]["def_rtg"])
    sorted_off  = sorted(result.items(), key=lambda x: x[1]["off_rtg"], reverse=True)
    for rank, (tid, _) in enumerate(sorted_pace, 1):
        result[tid]["pace_rank"] = rank
    for rank, (tid, _) in enumerate(sorted_def, 1):
        result[tid]["def_rtg_rank"] = rank
    for rank, (tid, _) in enumerate(sorted_off, 1):
        result[tid]["off_rtg_rank"] = rank

    return result

# ---------------------------------------------------------------------------
# Load player stats
# ---------------------------------------------------------------------------
def get_player_stats():
    adv = safe_call(leaguedashplayerstats.LeagueDashPlayerStats,
                    season=SEASON,
                    per_mode_detailed="PerGame",
                    measure_type_detailed_defense="Advanced")
    base = safe_call(leaguedashplayerstats.LeagueDashPlayerStats,
                     season=SEASON,
                     per_mode_detailed="PerGame",
                     measure_type_detailed_defense="Base")

    if adv.empty or base.empty:
        return {}

    # Get abbreviation from static data via team_id
    result = {}
    for _, row in adv.iterrows():
        pid = row["PLAYER_ID"]
        base_row = base[base["PLAYER_ID"] == pid]
        if base_row.empty:
            continue
        br = base_row.iloc[0]
        tid = int(row["TEAM_ID"]) if "TEAM_ID" in row else 0
        abbr = TEAM_ABBR_MAP.get(tid, "")

        usg = float(row.get("USG_PCT", 0))
        ast_pct = float(row.get("AST_PCT", 0))
        reb_pct = float(row.get("REB_PCT", 0))

        # nba_api returns these as decimals (0.25) or percentages (25.0) depending on version
        # Normalise: if all values are under 1.0, multiply by 100
        if usg <= 1.0:
            usg *= 100
        if ast_pct <= 1.0:
            ast_pct *= 100
        if reb_pct <= 1.0:
            reb_pct *= 100

        result[pid] = {
            "name": row["PLAYER_NAME"],
            "team_id": tid,
            "team_abbr": abbr,
            "gp": int(br.get("GP", 0)),
            "pts": float(br.get("PTS", 0)),
            "reb": float(br.get("REB", 0)),
            "ast": float(br.get("AST", 0)),
            "usg_pct": round(usg, 1),
            "ast_pct": round(ast_pct, 1),
            "reb_pct": round(reb_pct, 1),
        }
    return result

# ---------------------------------------------------------------------------
# Confluence scoring engine
# ---------------------------------------------------------------------------
def score_points(player, opp_team, team_stats):
    score = 0
    factors = []

    my_pace = team_stats.get(player["team_id"], {}).get("pace", 98)
    opp_pace = opp_team.get("pace", 98)
    avg_pace = (my_pace + opp_pace) / 2
    if avg_pace > 100:
        score += 1
        factors.append(f"✅ Fast pace matchup (avg {avg_pace:.1f}) → more possessions")
    else:
        score -= 1
        factors.append(f"⚠️ Slow pace matchup (avg {avg_pace:.1f}) → fewer possessions")

    usg = player.get("usg_pct", 0)
    if usg >= 25:
        score += 2
        factors.append(f"✅ High USG% ({usg:.1f}%) → team heavily reliant on this player")
    elif usg >= 20:
        score += 1
        factors.append(f"➡️ Moderate USG% ({usg:.1f}%)")
    else:
        score -= 1
        factors.append(f"⚠️ Low USG% ({usg:.1f}%) → limited offensive role")

    off_rank = team_stats.get(player["team_id"], {}).get("off_rtg_rank", 15)
    if off_rank <= 10:
        score += 1
        factors.append(f"✅ Team ORtg ranked #{off_rank} → efficient offense")
    elif off_rank >= 20:
        score -= 1
        factors.append(f"⚠️ Team ORtg ranked #{off_rank} → struggles scoring")

    opp_def_rank = opp_team.get("def_rtg_rank", 15)
    opp_pts_allowed = opp_team.get("opp_pts", 114)
    if opp_def_rank <= 8:
        score -= 2
        factors.append(f"🛑 Tough defense (DRtg rank #{opp_def_rank}, allows {opp_pts_allowed:.1f} pts/g) → scoring harder")
    elif opp_def_rank >= 20:
        score += 2
        factors.append(f"✅ Weak defense (DRtg rank #{opp_def_rank}, allows {opp_pts_allowed:.1f} pts/g) → easy scoring")
    else:
        factors.append(f"➡️ Average defense (DRtg rank #{opp_def_rank})")

    pts_avg = player.get("pts", 0)
    if pts_avg >= 20:
        score += 1
        factors.append(f"✅ Strong scorer ({pts_avg:.1f} pts/g avg)")
    elif pts_avg < 10:
        score -= 1
        factors.append(f"⚠️ Low scoring avg ({pts_avg:.1f} pts/g)")

    return score, factors

def score_assists(player, opp_team, team_stats):
    score = 0
    factors = []

    ast_pct = player.get("ast_pct", 0)
    if ast_pct >= 25:
        score += 2
        factors.append(f"✅ High AST% ({ast_pct:.1f}%) → major playmaking role")
    elif ast_pct >= 15:
        score += 1
        factors.append(f"➡️ Moderate AST% ({ast_pct:.1f}%)")
    else:
        score -= 1
        factors.append(f"⚠️ Low AST% ({ast_pct:.1f}%) → not a primary playmaker")

    opp_def_rank = opp_team.get("def_rtg_rank", 15)
    if opp_def_rank <= 8:
        score -= 2
        factors.append(f"🛑 Opp defense ranked #{opp_def_rank} → good rotation reduces kick-out assists")
    elif opp_def_rank >= 20:
        score += 2
        factors.append(f"✅ Opp defense ranked #{opp_def_rank} → poor rotation = more passing lanes")
    else:
        factors.append(f"➡️ Average opp team defense (#{opp_def_rank})")

    my_pace = team_stats.get(player["team_id"], {}).get("pace", 98)
    opp_pace = opp_team.get("pace", 98)
    avg_pace = (my_pace + opp_pace) / 2
    if avg_pace > 100:
        score += 1
        factors.append(f"✅ Fast pace ({avg_pace:.1f}) → more possessions = more assist chances")
    else:
        score -= 1
        factors.append(f"⚠️ Slow pace ({avg_pace:.1f}) → fewer possessions")

    ast_avg = player.get("ast", 0)
    if ast_avg >= 7:
        score += 1
        factors.append(f"✅ High assist avg ({ast_avg:.1f} ast/g)")
    elif ast_avg < 3:
        score -= 1
        factors.append(f"⚠️ Low assist avg ({ast_avg:.1f} ast/g)")

    return score, factors

def score_rebounds(player, opp_team, team_stats):
    score = 0
    factors = []

    reb_pct = player.get("reb_pct", 0)
    if reb_pct >= 15:
        score += 2
        factors.append(f"✅ High REB% ({reb_pct:.1f}%) → dominant rebounder")
    elif reb_pct >= 10:
        score += 1
        factors.append(f"➡️ Decent REB% ({reb_pct:.1f}%)")
    else:
        score -= 1
        factors.append(f"⚠️ Low REB% ({reb_pct:.1f}%) → not a primary rebounder")

    opp_off_rank = opp_team.get("off_rtg_rank", 15)
    if opp_off_rank <= 8:
        score -= 1
        factors.append(f"⚠️ Opp offense ranked #{opp_off_rank} → fewer misses = fewer rebound chances")
    elif opp_off_rank >= 20:
        score += 1
        factors.append(f"✅ Opp offense ranked #{opp_off_rank} → more misses = more rebound chances")
    else:
        factors.append(f"➡️ Average opp offense (#{opp_off_rank})")

    my_pace = team_stats.get(player["team_id"], {}).get("pace", 98)
    opp_pace = opp_team.get("pace", 98)
    avg_pace = (my_pace + opp_pace) / 2
    if avg_pace > 100:
        score += 1
        factors.append(f"✅ Fast pace ({avg_pace:.1f}) → more possessions = more rebound chances")
    else:
        factors.append(f"➡️ Slower pace ({avg_pace:.1f})")

    reb_avg = player.get("reb", 0)
    if reb_avg >= 8:
        score += 1
        factors.append(f"✅ Strong rebound avg ({reb_avg:.1f} reb/g)")
    elif reb_avg < 4:
        score -= 1
        factors.append(f"⚠️ Low rebound avg ({reb_avg:.1f} reb/g)")

    return score, factors

def get_confidence_label(score):
    if score >= 5:
        return "STRONG OVER", "#00e676"
    elif score >= 3:
        return "LEAN OVER", "#69f0ae"
    elif score >= 1:
        return "SLIGHT OVER", "#b9f6ca"
    elif score <= -5:
        return "STRONG UNDER", "#ff1744"
    elif score <= -3:
        return "LEAN UNDER", "#ff5252"
    elif score <= -1:
        return "SLIGHT UNDER", "#ff8a80"
    else:
        return "NEUTRAL", "#90a4ae"

# ---------------------------------------------------------------------------
# Build top plays (vs league median)
# ---------------------------------------------------------------------------
def build_analysis(team_stats, player_stats, min_gp=20):
    results = []
    team_ids = list(team_stats.keys())
    if not team_ids:
        return results

    # League median opponent
    all_pace = [t["pace"] for t in team_stats.values()]
    all_def  = [t["def_rtg"] for t in team_stats.values()]
    all_off  = [t["off_rtg"] for t in team_stats.values()]
    median_opp = {
        "pace": sorted(all_pace)[len(all_pace)//2] if all_pace else 98,
        "pace_rank": 15,
        "off_rtg": sorted(all_off)[len(all_off)//2] if all_off else 114,
        "off_rtg_rank": 15,
        "def_rtg": sorted(all_def)[len(all_def)//2] if all_def else 114,
        "def_rtg_rank": 15,
        "opp_pts": 114.0,
        "team_name": "League Avg"
    }

    processed = 0
    for pid, player in player_stats.items():
        if player.get("gp", 0) < min_gp:
            continue

        best_prop = None
        best_score = 0

        for prop, score_fn in [("Points", score_points), ("Assists", score_assists), ("Rebounds", score_rebounds)]:
            s, factors = score_fn(player, median_opp, team_stats)
            if abs(s) > best_score:
                best_score = abs(s)
                best_prop = (prop, s, factors)

        if best_prop and best_score >= 2:
            prop_name, score, factors = best_prop
            label, color = get_confidence_label(score)
            avg_val = player.get(prop_name.lower()[:3], 0)
            results.append({
                "player": player["name"],
                "team": player.get("team_abbr", ""),
                "prop": prop_name,
                "avg": round(avg_val, 1),
                "score": score,
                "abs_score": abs(score),
                "label": label,
                "color": color,
                "factors": factors,
                "usg": round(player.get("usg_pct", 0), 1),
                "ast_pct": round(player.get("ast_pct", 0), 1),
                "reb_pct": round(player.get("reb_pct", 0), 1),
            })
        processed += 1
        if processed >= 200:
            break

    results.sort(key=lambda x: x["abs_score"], reverse=True)
    return results[:40]

# ---------------------------------------------------------------------------
# Matchup analyzer
# ---------------------------------------------------------------------------
def analyze_matchup(team1_input, team2_input, prop_type, team_stats, player_stats):
    # Try abbreviation lookup first, then partial name match
    t1_id = ABBR_TO_ID.get(team1_input.upper())
    t2_id = ABBR_TO_ID.get(team2_input.upper())

    # Fallback: match by team name fragment
    if not t1_id:
        for t in nba_teams_static.get_teams():
            if team1_input.upper() in t["full_name"].upper() or team1_input.upper() in t["nickname"].upper():
                t1_id = t["id"]
                break
    if not t2_id:
        for t in nba_teams_static.get_teams():
            if team2_input.upper() in t["full_name"].upper() or team2_input.upper() in t["nickname"].upper():
                t2_id = t["id"]
                break

    if not t1_id or not t2_id:
        return [], f"Could not find teams: '{team1_input}' or '{team2_input}'. Use 3-letter abbreviations like LAL, BOS, GSW."

    if t1_id not in team_stats or t2_id not in team_stats:
        return [], "Teams found in static data but not in season stats. Check season."

    t1 = team_stats[t1_id]
    t2 = team_stats[t2_id]

    score_fn = {"Points": score_points, "Assists": score_assists, "Rebounds": score_rebounds}.get(prop_type, score_points)
    results = []

    for pid, player in player_stats.items():
        if player.get("gp", 0) < 10:
            continue
        if player["team_id"] not in (t1_id, t2_id):
            continue
        opp = t2 if player["team_id"] == t1_id else t1
        score, factors = score_fn(player, opp, team_stats)
        label, color = get_confidence_label(score)
        avg_val = player.get(prop_type.lower()[:3], 0)
        results.append({
            "player": player["name"],
            "team": player.get("team_abbr", ""),
            "prop": prop_type,
            "avg": round(avg_val, 1),
            "score": score,
            "abs_score": abs(score),
            "label": label,
            "color": color,
            "factors": factors,
            "usg": round(player.get("usg_pct", 0), 1),
            "ast_pct": round(player.get("ast_pct", 0), 1),
            "reb_pct": round(player.get("reb_pct", 0), 1),
        })

    results.sort(key=lambda x: x["abs_score"], reverse=True)
    return results, None

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache = {}

def load_data():
    if "team_stats" not in _cache:
        print("Loading team stats...")
        _cache["team_stats"] = get_team_stats()
        print(f"  → {len(_cache['team_stats'])} teams loaded")
    if "player_stats" not in _cache:
        print("Loading player stats...")
        _cache["player_stats"] = get_player_stats()
        print(f"  → {len(_cache['player_stats'])} players loaded")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/top-plays")
def top_plays():
    try:
        load_data()
        results = build_analysis(_cache["team_stats"], _cache["player_stats"])
        return jsonify({"status": "ok", "data": results})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "trace": traceback.format_exc()})

@app.route("/api/matchup/<team1>/<team2>/<prop>")
def matchup(team1, team2, prop):
    try:
        load_data()
        results, err = analyze_matchup(team1, team2, prop, _cache["team_stats"], _cache["player_stats"])
        if err:
            return jsonify({"status": "error", "message": err})
        return jsonify({"status": "ok", "data": results})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/api/teams")
def get_teams():
    try:
        team_list = [{"abbr": t["abbreviation"], "name": t["full_name"]}
                     for t in nba_teams_static.get_teams()]
        team_list.sort(key=lambda x: x["name"])
        return jsonify({"status": "ok", "data": team_list})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == "__main__":
    print("=" * 50)
    print("NBA Prop Confluence Tool")
    print("Loading data from NBA API...")
    print("This takes ~30 seconds on first run.")
    print("=" * 50)
port = int(os.environ.get("PORT", 8080))
app.run(debug=False, port=port, host='0.0.0.0')
