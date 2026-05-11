#!/usr/bin/env python3
"""
new_game.py — Interactive setup for a new highlight game.

Run:
    python3 new_game.py

Prompts for all required details, writes a fresh config.py, and prints
the next steps to follow.
"""

import os
import re
import shutil
import sys

try:
    import requests
except ImportError:
    print("requests is not installed. Run: pip3 install requests")
    sys.exit(1)


LEAGUES = {
    "1": ("mpbl",    "MPBL",              10),
    "2": ("uaap",    "UAAP",              10),
    "3": ("ncaa",    "NCAA Philippines",  10),
    "4": ("jr_mpbl", "Jr. MPBL",          10),
}

LEAGUE_HASHTAGS = {
    "mpbl":    "#MPBL #PusoPangBayan",
    "uaap":    "#UAAP",
    "ncaa":    "#NCAAPhilippines",
    "jr_mpbl": "#JrMPBL",
}

FIBA_URL = "https://fibalivestats.dcd.shared.geniussports.com/data/{game_id}/data.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def ask(prompt, default=None, required=True):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default is not None:
            return str(default)
        if val:
            return val
        if not required:
            return ""
        print("  This field is required.")


def ask_int(prompt, default=None):
    while True:
        raw = ask(prompt, default=default)
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a whole number.")


def ask_time(prompt):
    """Ask for a time in M:SS or MM:SS and return total seconds, or None to skip."""
    while True:
        raw = ask(prompt + " (M:SS or MM:SS — or press Enter to skip)", required=False)
        if not raw:
            return None
        parts = raw.strip().split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            pass
        print("  Format must be M:SS, MM:SS, or H:MM:SS.")


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def confirm(prompt):
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


def fetch_teams(game_id: str):
    """Fetch the FIBA feed and return a list of (tno, team_name) tuples."""
    try:
        resp = requests.get(FIBA_URL.format(game_id=game_id), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        teams = []
        for tno_key, info in (data.get("tm") or {}).items():
            name = info.get("name") or info.get("shortName") or f"Team {tno_key}"
            teams.append((int(tno_key), name))
        return sorted(teams)
    except Exception as exc:
        print(f"  Could not fetch teams: {exc}")
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print()
    print("=" * 62)
    print("  New Game Setup — SiKAT Highlight Pipeline")
    print("=" * 62)
    print()

    # ── League ────────────────────────────────────────────────────────────────
    print("[ League ]")
    for key, (slug, label, _) in LEAGUES.items():
        print(f"  {key}. {label}")
    while True:
        choice = ask("  Select league (1-4)")
        if choice in LEAGUES:
            league_slug, league_label, period_length = LEAGUES[choice]
            break
        print("  Please enter a number from 1 to 4.")
    print()

    # ── FIBA game ID ──────────────────────────────────────────────────────────
    print("[ FIBA LiveStats ]")
    while True:
        game_id = ask("  Game ID (numbers only — e.g. 2836518, not the full URL)")
        if game_id.isdigit():
            break
        print("  Game ID must be a number.")
    print()

    # ── Team identity — auto-detect from feed ─────────────────────────────────
    print("[ Your Team ]")
    print("  Fetching teams from FIBA feed…")
    teams = fetch_teams(game_id)

    team_tno = None
    team_name_default = ""
    if teams:
        print("  Teams found in this game:")
        for tno, name in teams:
            print(f"    {tno}. {name}")
        while True:
            raw = ask("  Enter your team's number (tno) from the list above")
            try:
                team_tno = int(raw)
                match = [name for t, name in teams if t == team_tno]
                if match:
                    team_name_default = match[0]
                    break
                print(f"  {raw} not found. Choose from: {[t for t, _ in teams]}")
            except ValueError:
                print("  Please enter a number.")
    else:
        team_tno = ask_int("  Team TNO (1 or 2 — check the FIBA feed manually)")

    team_name  = ask("  Team name", default=team_name_default)
    team_slug  = slugify(ask("  Short folder name", default=slugify(team_name.split()[0])))
    print()

    # ── Opponent ──────────────────────────────────────────────────────────────
    print("[ Opponent ]")
    opp_teams = [(t, n) for t, n in teams if t != team_tno] if teams else []
    opp_default = slugify(opp_teams[0][1].split()[0]) if opp_teams else ""
    opponent_slug = slugify(ask("  Opponent folder name (e.g. ilagan)", default=opp_default))
    reject_names  = ask("  Audio reject keywords (comma-separated opponent city/nickname)")
    reject_list   = [w.strip().lower() for w in reject_names.split(",") if w.strip()]
    print()

    # ── YouTube ───────────────────────────────────────────────────────────────
    print("[ YouTube VOD ]")
    youtube_url = ask("  YouTube URL")
    print()
    print("  Tip-off time: pause the video at the tip-off and note the timestamp.")
    tipoff_secs = ask_time("  Tip-off video time")
    print()

    # ── Clip settings ─────────────────────────────────────────────────────────
    print("[ Clip settings ]")
    lead = ask_int("  Seconds of footage BEFORE each basket (lead)", default=3.5)
    tail = ask_int("  Seconds of footage AFTER each basket (tail)", default=8)
    include_ft = confirm("  Include free throw clips in highlights?")
    print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("=" * 62)
    print("  Summary")
    print("=" * 62)
    print(f"  League    : {league_label}  (folder: {league_slug})")
    print(f"  Game ID   : {game_id}")
    print(f"  Team      : {team_name}  (tno: {team_tno}, folder: {team_slug})")
    print(f"  Opponent  : {opponent_slug}")
    print(f"  YouTube   : {youtube_url}")
    if tipoff_secs is not None:
        m, s = tipoff_secs // 60, tipoff_secs % 60
        print(f"  Tip-off   : {m}m{s:02d}s into video ({tipoff_secs}s)")
    else:
        print(f"  Tip-off   : NOT SET — update TIPOFF_VIDEO_SECONDS in config.py before running")
    print(f"  Clip      : {lead}s lead + {tail}s tail")
    print(f"  Free throws in highlights: {'yes' if include_ft else 'no'}")
    print(f"  Audio reject: {reject_list}")
    print(f"  Output    : highlights/{league_slug}/{team_slug}/{opponent_slug}/{{1,2,3,4}}/")
    print()

    if not confirm("Write config.py with these settings?"):
        print("Aborted. No files changed.")
        sys.exit(0)

    # ── Write config.py ───────────────────────────────────────────────────────
    tipoff_line = (
        f"TIPOFF_VIDEO_SECONDS = {tipoff_secs}"
        if tipoff_secs is not None
        else "TIPOFF_VIDEO_SECONDS = 0   # TODO: set this before running"
    )
    hashtags = LEAGUE_HASHTAGS.get(league_slug, f"#{league_slug.upper()}")

    config_content = f"""# Highlight Pipeline — Configuration
# Generated by new_game.py — update CALIBRATION_ANCHORS as you confirm timestamps.

# -- FIBA LiveStats -----------------------------------------------------------
GAME_ID = "{game_id}"
FIBA_URL_TEMPLATE = (
    "https://fibalivestats.dcd.shared.geniussports.com/data/{{game_id}}/data.json"
)
POLL_INTERVAL = 20  # seconds between feed checks (live mode)

# -- League -------------------------------------------------------------------
# Supported: mpbl, uaap, ncaa, jr_mpbl
LEAGUE = "{league_slug}"

# -- Team identity ------------------------------------------------------------
TEAM      = "{team_slug}"        # short folder name
TEAM_TNO  = {team_tno}           # tno value in the FIBA feed (1 or 2)
TEAM_NAME = "{team_name}"

# -- Opponent / game identifier -----------------------------------------------
# Output folders: highlights/{{LEAGUE}}/{{TEAM}}/{{OPPONENT}}/{{quarter}}/
OPPONENT = "{opponent_slug}"

# -- YouTube stream / VOD -----------------------------------------------------
YOUTUBE_STREAM_URL = "{youtube_url}"

# -- VOD replay timing --------------------------------------------------------
{tipoff_line}

# Calibration anchors — piecewise linear interpolation
# Format: list of (video_seconds, quarter, game_clock_string)
# The tip-off is always the base anchor. Add confirmed plays here.
# More anchors = more accurate timestamps throughout the game.
#
CALIBRATION_ANCHORS = [
    # (video_seconds, quarter, "game_clock MM:SS")
    # Q1
    # Q2
    # Q3
    # Q4
]

# Fallback real-time factor (real seconds per game-clock second).
# Used only when extrapolating beyond the last anchor.
REAL_TIME_FACTOR = 2.2

# Seconds of broadcast time between quarters and at halftime.
# Re-confirm from anchor data each game — varies by venue and broadcaster.
QUARTER_BREAK_SECONDS  = 190   # after Q1 and Q3
HALFTIME_BREAK_SECONDS = 700   # halftime break visible in broadcast

# -- Game timing --------------------------------------------------------------
PERIOD_LENGTH    = {period_length}   # minutes per regular quarter
OT_PERIOD_LENGTH = 5    # minutes per overtime period

# -- Video clipping -----------------------------------------------------------
INCLUDE_FREETHROWS = {include_ft}

CLIP_LEAD_SECONDS = {lead}    # seconds of footage before detected score
CLIP_TAIL_SECONDS = {tail}     # seconds of footage after detected score

QUARTER_COMPILE_DELAY = 90    # seconds to wait before compiling (live mode)

# -- Audio verification (Whisper) ---------------------------------------------
AUDIO_VERIFY = True

REVIEW_DIR = "review"

# Crowd noise thresholds (16-bit PCM scale: 0-32767)
CROWD_NOISE_SILENCE_THRESHOLD  = 800
CROWD_NOISE_SPIKE_RATIO        = 1.2
CROWD_NOISE_CONSISTENT_FACTOR  = 4

# -- Paths --------------------------------------------------------------------
RECORDING_DIR  = "recording"
RECORDING_FILE = "recording/stream.ts"
CLIPS_DIR      = "clips"
HIGHLIGHTS_DIR = "highlights"

# -- Facebook (optional) ------------------------------------------------------
FB_ACCESS_TOKEN = ""
FB_PAGE_ID      = ""
FB_POST_MESSAGE_TEMPLATE = (
    "{team_name} Q{{quarter}} Highlights — {{date}}\\n"
    "{hashtags}"
)
"""

    with open("config.py", "w") as f:
        f.write(config_content)
    print()
    print("  config.py written.")

    # ── Optional cleanup ──────────────────────────────────────────────────────
    print()
    for old_file in ("recording/stream.mp4", "recording/stream.url"):
        if os.path.isfile(old_file):
            os.remove(old_file)
            print(f"  {old_file} deleted.")

    # Check if this specific game's output folder already exists (re-run scenario)
    game_clips_dir = os.path.join("clips", league_slug, team_slug, opponent_slug)
    game_highlights_dir = os.path.join("highlights", league_slug, team_slug, opponent_slug)
    for folder in (game_clips_dir, game_highlights_dir):
        if os.path.isdir(folder) and os.listdir(folder):
            if confirm(f"  {folder}/ already has content. Clear it?"):
                shutil.rmtree(folder)
                print(f"  {folder}/ cleared.")

    # ── Next steps ────────────────────────────────────────────────────────────
    print()
    print("=" * 62)
    print("  Next steps")
    print("=" * 62)

    if tipoff_secs is None:
        print("  1. Open the YouTube video, find the tip-off moment,")
        print("     and set TIPOFF_VIDEO_SECONDS in config.py.")
        print()

    print("  After each quarter ends:")
    print("  1. Generate highlights (you will be prompted for the quarter")
    print("     start timestamp if not yet set):")
    print("     python3 vod_replay.py --quarters N --skip-download")
    print()
    print("  2. If clips look off, calibrate and regenerate:")
    print("     python3 vod_replay.py --calibrate --quarters N")
    print("     python3 vod_replay.py --quarters N --skip-download")

    print()
    print("  Refer to RUNBOOK.md for the full workflow.")
    print()


if __name__ == "__main__":
    main()
