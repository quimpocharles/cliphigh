#!/usr/bin/env python3
"""
vod_replay.py — SiKAT Highlight Generator for Completed / Archived Games
=========================================================================
Downloads the YouTube VOD once, then uses the FIBA pbp data to cut a clip
for every SiKAT scoring play and compile per-quarter highlight reels.

Timestamp mapping
-----------------
Uses piecewise linear interpolation between CALIBRATION_ANCHORS defined in
config.py. Each anchor is a confirmed (video_seconds, quarter, game_clock)
triple. Between two known anchors the local real-time factor is derived
exactly. Beyond the last anchor REAL_TIME_FACTOR is used to extrapolate.

Workflow per quarter
--------------------
Default (recommended):
    python3 vod_replay.py --quarters N [--skip-download]

    This always runs dry-run → calibrate → generate in a single session.
    You will be prompted for the quarter start timestamp if not yet set,
    shown all estimated timestamps, asked to correct any that are wrong,
    then clips are cut and compiled automatically.

Flags:
    --dry-run         Show estimated timestamps only, then exit.
    --calibrate       Show timestamps + calibrate, then exit (no clip generation).
    --skip-download   Reuse the existing VOD file instead of downloading again.
"""

import argparse
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

import requests

import config
import game_stats
from fiba_poller import ScoringEvent, QuarterEndEvent, _fetch_data, _scores, _points_for
from clipper import Clipper
from stream_recorder import StreamRecorder
from publisher import Publisher
from audio_verifier import verify as audio_verify, move_to_review

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vod_replay")

VOD_FILE = os.path.join(config.RECORDING_DIR, "stream.mp4")

# RTF profile loaded once per run from game_stats/*.json
_rtf_profile: Optional[dict] = None


def _get_rtf_profile() -> dict:
    global _rtf_profile
    if _rtf_profile is None:
        _rtf_profile = game_stats.load_profile(config.LEAGUE)
    return _rtf_profile


# ── Anchor table ─────────────────────────────────────────────────────────────

def _parse_gt(gt: str) -> int:
    """Parse 'MM:SS' game clock string → seconds remaining."""
    try:
        m, s = gt.split(":")
        return int(m) * 60 + int(s)
    except (ValueError, IndexError):
        return 0


def _quarter_period_length(period: int) -> int:
    """Game-clock seconds in a period."""
    return config.OT_PERIOD_LENGTH * 60 if period > 4 else config.PERIOD_LENGTH * 60


def _build_anchor_table() -> list:
    """
    Build a sorted list of (video_seconds, absolute_game_seconds) pairs from:
      1. The tip-off (always first)
      2. CALIBRATION_ANCHORS from config

    'absolute_game_seconds' counts continuously from the tip-off, ignoring
    breaks, so it purely represents game-clock progress.
    """
    period_len = config.PERIOD_LENGTH * 60

    def to_absolute(quarter: int, gt_str: str) -> int:
        """Convert (quarter, game_clock_remaining) → absolute game seconds."""
        elapsed_in_q = period_len - _parse_gt(gt_str)
        return (quarter - 1) * period_len + elapsed_in_q

    table = [(config.TIPOFF_VIDEO_SECONDS, 0)]  # tip-off anchor

    for video_sec, quarter, gt_str in config.CALIBRATION_ANCHORS:
        abs_sec = to_absolute(quarter, gt_str)
        table.append((video_sec, abs_sec))

    # Sort by absolute game seconds
    table.sort(key=lambda x: x[1])
    return table


def _extrapolate(v_last: float, a_last: int, target_abs: int,
                 period_len: int) -> float:
    """
    Extrapolate video seconds from a_last → target_abs using a
    position-aware RTF profile learned from previous games.

    Steps through the game clock in bucket-sized increments, applying the
    average RTF observed for that (quarter, clock-bucket) combination.
    Falls back to config.REAL_TIME_FACTOR for any bucket with no history.
    Quarter and halftime breaks are added when a quarter boundary is crossed.
    """
    profile = _get_rtf_profile()
    extra = 0.0
    a = a_last

    while a < target_abs:
        q = a // period_len + 1
        q_end_abs     = q * period_len
        clock_remaining = q_end_abs - a

        # Advance only to the nearest bucket boundary, quarter end, or target
        next_stop = min(target_abs, q_end_abs)
        for _, lo, _ in game_stats.BUCKETS:
            boundary = q_end_abs - lo          # absolute secs where bucket starts
            if a < boundary < next_stop:
                next_stop = boundary

        step = next_stop - a
        bucket = game_stats.clock_bucket(clock_remaining)
        rtf = profile.get((q, bucket), config.REAL_TIME_FACTOR)
        extra += step * rtf
        a = next_stop

        # If we just finished a quarter and haven't reached target yet, add break
        if a == q_end_abs and a < target_abs:
            extra += (config.HALFTIME_BREAK_SECONDS if q == 2
                      else config.QUARTER_BREAK_SECONDS)

    return v_last + extra


def event_video_timestamp(period: int, gt: str) -> float:
    """
    Convert a pbp entry's (period, gt) to a video timestamp (seconds).

    Uses piecewise linear interpolation between calibration anchors.
    Beyond the last anchor, extrapolates using a position-aware RTF profile
    built from confirmed anchor data across previous games (game_stats/).
    Falls back to config.REAL_TIME_FACTOR when no historical data exists.
    """
    period_len = config.PERIOD_LENGTH * 60
    gt_remaining = _parse_gt(gt)
    elapsed_in_q = period_len - gt_remaining
    # Absolute game seconds (pure game-clock progress, no breaks)
    target_abs = (period - 1) * period_len + elapsed_in_q

    table = _build_anchor_table()

    # Find the two anchors that bracket target_abs → interpolate
    for i in range(len(table) - 1):
        v0, a0 = table[i]
        v1, a1 = table[i + 1]
        if a0 <= target_abs <= a1:
            frac = (target_abs - a0) / (a1 - a0)
            return v0 + frac * (v1 - v0)

    # Beyond last anchor → position-aware extrapolation
    v_last, a_last = table[-1]
    return _extrapolate(v_last, a_last, target_abs, period_len)


# ── Anchor lookup ────────────────────────────────────────────────────────────

def _is_anchored(period: int, gt: str) -> bool:
    """Return True if this exact (quarter, game_clock) was manually confirmed."""
    return any(q == period and g == gt for _, q, g in config.CALIBRATION_ANCHORS)


# ── Quarter start prompts ─────────────────────────────────────────────────────

def _has_quarter_start_anchor(quarter: int) -> bool:
    """Return True if CALIBRATION_ANCHORS already has a start-of-quarter anchor."""
    clock = f"{config.PERIOD_LENGTH}:00"
    return any(q == quarter and g == clock for _, q, g in config.CALIBRATION_ANCHORS)


def _ask_quarter_starts(quarters: list) -> None:
    """
    For each quarter > 1, prompt for the video timestamp when the game clock
    shows PERIOD_LENGTH:00 (e.g. 10:00 for MPBL).  Skips quarters that already
    have a start anchor in CALIBRATION_ANCHORS.  Writes any provided anchors
    to config.py immediately.
    """
    quarter_clock = f"{config.PERIOD_LENGTH}:00"
    new_anchors = []

    for q in sorted(quarters):
        if q == 1:
            continue  # Q1 start == tipoff, already anchored
        if _has_quarter_start_anchor(q):
            continue  # already set from a previous run

        print(f"\n  Q{q} start anchor not set.")
        print(f"  Pause the video when the Q{q} clock shows {quarter_clock}.")
        raw = input(f"  Q{q} start video time (M:SS or MM:SS, Enter to skip): ").strip()
        if not raw:
            print(f"  Skipped — timestamp accuracy for Q{q} will rely on extrapolation.")
            continue
        secs = _parse_video_time(raw)
        if secs is None:
            print("  Invalid format — skipping. Add it manually to CALIBRATION_ANCHORS.")
            continue
        new_anchors.append((secs, q, quarter_clock))
        config.CALIBRATION_ANCHORS.append((secs, q, quarter_clock))
        log.info("Q%d start anchor set: %s", q, _fmt(secs))

    if new_anchors:
        _append_anchors_to_config(new_anchors)


# ── Calibration helpers ───────────────────────────────────────────────────────

def _parse_video_time(s: str) -> Optional[int]:
    """Parse user-entered video time to seconds. Handles M:SS, MM:SS, H:MM:SS."""
    parts = s.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return None


def _append_anchors_to_config(new_anchors: list) -> None:
    """Persist new calibration anchors to config.py.

    In-memory config.CALIBRATION_ANCHORS is already updated by the calibrate
    loop before this is called, so we only touch the file here.
    Existing file entries for the same (quarter, gt) are replaced to prevent
    duplicates when re-calibrating a play.
    """
    if not new_anchors:
        return
    with open("config.py", "r") as f:
        content = f.read()

    # Remove any existing confirmed lines for (quarter, gt) pairs we're updating
    replace_keys = {(q, g) for _, q, g in new_anchors}
    filtered_lines = []
    for line in content.splitlines(keepends=True):
        drop = False
        for (q, g) in replace_keys:
            if f", {q}, \"{g}\")" in line and "# confirmed" in line:
                drop = True
                break
        if not drop:
            filtered_lines.append(line)
    content = "".join(filtered_lines)

    new_lines = []
    for video_secs, quarter, gt in new_anchors:
        new_lines.append(f"    ({video_secs}, {quarter}, \"{gt}\"),  # confirmed")

    # Find the closing ] of CALIBRATION_ANCHORS
    start = content.find("CALIBRATION_ANCHORS")
    end   = content.find("\n]", start)
    if end == -1:
        log.error("Could not find CALIBRATION_ANCHORS closing bracket in config.py")
        return

    new_content = content[:end] + "\n" + "\n".join(new_lines) + content[end:]
    with open("config.py", "w") as f:
        f.write(new_content)

    log.info("config.py updated with %d new anchor(s).", len(new_anchors))


def _fmt(video_secs: float) -> str:
    """Format video seconds as M:SS or H:MM:SS."""
    total = int(video_secs)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ── VOD download ──────────────────────────────────────────────────────────────

URL_RECORD_FILE = os.path.join(config.RECORDING_DIR, "stream.url")


def _saved_url() -> str:
    """Return the YouTube URL that was used for the current recording, or ''."""
    if os.path.isfile(URL_RECORD_FILE):
        return open(URL_RECORD_FILE).read().strip()
    return ""


def download_vod(url: str, output: str) -> bool:
    Path(config.RECORDING_DIR).mkdir(exist_ok=True)
    log.info("Downloading VOD → %s  (this may take a while…)", output)
    cmd = [
        "yt-dlp",
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--force-overwrites",
        "-o", output,
        url,
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.error("yt-dlp download failed (exit %d).", result.returncode)
        return False
    # Save the URL so future runs can verify they're using the right video
    with open(URL_RECORD_FILE, "w") as f:
        f.write(url)
    log.info("Download complete: %s", output)
    return True


# ── Clip helper (direct ffmpeg, no StreamRecorder needed for VOD) ─────────────

def cut_clip(video_file: str, start: float, duration: float, output: str) -> bool:
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-loglevel", "warning",
        "-ss", f"{max(0, start):.3f}",
        "-i", video_file,
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-y", output,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("ffmpeg error:\n%s", result.stderr[-600:])
        return False
    return True


def compile_quarter(quarter: int, clip_paths: List[str]) -> Optional[str]:
    if not clip_paths:
        log.warning("Q%d: no clips to compile.", quarter)
        return None
    import time
    date_str  = time.strftime("%Y%m%d")
    quarter_dir = os.path.join(config.HIGHLIGHTS_DIR, config.LEAGUE, config.TEAM, config.OPPONENT, str(quarter))
    Path(quarter_dir).mkdir(parents=True, exist_ok=True)
    output     = os.path.join(quarter_dir, f"SiKAT_Q{quarter}_{date_str}.mp4")
    concat_txt = os.path.join(quarter_dir, f"concat_Q{quarter}.txt")

    with open(concat_txt, "w") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    cmd = [
        "ffmpeg", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", concat_txt,
        "-c", "copy", "-y", output,
    ]
    log.info("Compiling Q%d highlight: %d clips → %s", quarter, len(clip_paths), output)
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(concat_txt)

    if result.returncode != 0:
        log.error("Compile failed:\n%s", result.stderr[-600:])
        return None
    log.info("Q%d highlight ready: %s", quarter, output)
    return output


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SiKAT VOD Highlight Replay")
    parser.add_argument("--skip-download", action="store_true",
                        help=f"Reuse existing {VOD_FILE} instead of downloading")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print estimated timestamps only, no ffmpeg")
    parser.add_argument("--quarters", nargs="+", type=int, default=[1, 2, 3, 4],
                        help="Which quarters to process (default: 1 2 3 4)")
    parser.add_argument("--min-clock", default=None,
                        help="Skip plays with less game clock than this (MM:SS). "
                             "Use when the stream ends before the quarter finishes.")
    parser.add_argument("--calibrate", action="store_true",
                        help="Interactive mode: verify each play's timestamp and "
                             "write confirmed anchors to config.py before cutting clips.")
    args = parser.parse_args()

    log.info("═" * 60)
    log.info("  SiKAT VOD Replay — Game %s", config.GAME_ID)
    log.info("  Video  : %s", config.YOUTUBE_STREAM_URL)
    log.info("  Tip-off: %ds into video (%dm%02ds)",
             config.TIPOFF_VIDEO_SECONDS,
             config.TIPOFF_VIDEO_SECONDS // 60,
             config.TIPOFF_VIDEO_SECONDS % 60)
    log.info("  Quarters: %s", args.quarters)
    profile = _get_rtf_profile()
    if profile:
        log.info("  RTF profile loaded (%d bucket(s) from game_stats/)", len(profile))
    else:
        log.info("  RTF profile: none yet — using REAL_TIME_FACTOR=%.1f fallback",
                 config.REAL_TIME_FACTOR)
    log.info("═" * 60)

    # ── Fetch pbp data ────────────────────────────────────────────────────────
    log.info("Fetching FIBA play-by-play data…")
    data = _fetch_data()
    if not data:
        log.error("Could not fetch FIBA data. Exiting.")
        sys.exit(1)

    pbp = sorted(data.get("pbp") or [], key=lambda e: e.get("actionNumber", 0))

    clip_types = ["2pt", "3pt"]
    if config.INCLUDE_FREETHROWS:
        clip_types.append("freethrow")

    min_clock_secs = _parse_gt(args.min_clock) if args.min_clock else 0

    sikat_events = [
        e for e in pbp
        if e.get("tno") == config.TEAM_TNO
        and int(e.get("success", 0)) == 1
        and int(e.get("scoring", 0)) == 1
        and e.get("actionType") in clip_types
        and e.get("period") in args.quarters
        and _parse_gt(e.get("gt", "00:00")) >= min_clock_secs
    ]

    log.info("Found %d SiKAT scoring plays across Q%s (freethrows %s)",
             len(sikat_events), args.quarters,
             "included" if config.INCLUDE_FREETHROWS else "excluded")

    # ── Quarter start anchors (prompt if missing) ─────────────────────────────
    _ask_quarter_starts(args.quarters)

    # ── Show estimated timestamps (always) ────────────────────────────────────
    print()
    for evt in sikat_events:
        period = evt.get("period")
        gt     = evt.get("gt", "00:00")
        player = evt.get("player", "Unknown")
        shirt  = evt.get("shirtNumber", "")
        a_type = evt.get("actionType", "2pt")
        sub    = evt.get("subType", "")
        qualif = ", ".join(evt.get("qualifier") or [])
        vid_ts = event_video_timestamp(period, gt)
        log.info(
            "Q%d %s  #%s %-20s  %-10s %-18s  [%s]  → video ~%s",
            period, gt, shirt, player, a_type, sub, qualif, _fmt(vid_ts),
        )

    if args.dry_run:
        log.info("[dry-run] No files written.")
        return

    # ── Calibrate (always in default flow) ────────────────────────────────────
    print("\n" + "=" * 62)
    print("  CALIBRATION — Q" + "/Q".join(str(q) for q in args.quarters))
    print("  Open the YouTube video alongside this prompt.")
    print("  For each play: press Enter if the estimate is correct,")
    print("  or type the actual video time (e.g.  7:39  or  1:02:07).")
    print("=" * 62 + "\n")

    new_anchors = []  # (video_secs, period, gt) added this session

    def _last_confirmed_secs():
        """Highest video timestamp confirmed so far in this session."""
        return max((v for v, q, g in new_anchors), default=0)

    def _remove_anchor(period, gt):
        """Remove an anchor from both new_anchors and in-memory config."""
        new_anchors[:] = [(v,q,g) for v,q,g in new_anchors
                          if not (q == period and g == gt)]
        config.CALIBRATION_ANCHORS[:] = [(v,q,g) for v,q,g in config.CALIBRATION_ANCHORS
                                         if not (q == period and g == gt)]

    i = 0
    while i < len(sikat_events):
        evt     = sikat_events[i]
        period  = evt.get("period")
        gt      = evt.get("gt", "00:00")
        player  = evt.get("player", "Unknown")
        a_type  = evt.get("actionType", "2pt")
        sub     = evt.get("subType", "")
        vid_ts  = event_video_timestamp(period, gt)
        team_s, opp_s = _scores(evt)
        score_tag = f"{config.TEAM.upper()} {team_s} - {opp_s} OPP"

        while True:
            raw = input(
                f"  Q{period} {gt}  {player} ({a_type} {sub})"
                f"  [{score_tag}]"
                f"  estimated {_fmt(vid_ts)}"
                f"  → actual [Enter=correct, b=back]: "
            ).strip()

            if raw.lower() in ("b", "back"):
                if i > 0:
                    prev = sikat_events[i - 1]
                    _remove_anchor(prev.get("period"), prev.get("gt", "00:00"))
                    i -= 1
                    print("  ↩ Going back to previous play.")
                else:
                    print("  Already at the first play.")
                break

            if not raw:
                i += 1
                break  # estimate accepted, no anchor needed

            actual = _parse_video_time(raw)
            if actual is not None:
                last = _last_confirmed_secs()
                if actual <= last:
                    print(f"    WARNING: {_fmt(actual)} is not after the previous play"
                          f" at {_fmt(last)}.")
                    print(f"    Video timestamps must increase as the game progresses.")
                    print(f"    Please re-check the video and try again.")
                    continue
                # Remove any stale anchor for this exact play before adding the new one
                _remove_anchor(period, gt)
                new_anchors.append((actual, period, gt))
                config.CALIBRATION_ANCHORS.append((actual, period, gt))
                i += 1
                break

            print("    Invalid format. Use M:SS, MM:SS, or H:MM:SS (e.g. 1:07:39).")

    if new_anchors:
        _append_anchors_to_config(new_anchors)
        print(f"\n  {len(new_anchors)} anchor(s) saved to config.py.")

        # Persist game stats and reload the RTF profile so the
        # "Updated estimates" display below uses the freshest data
        stats_path = game_stats.save(config)
        log.info("Game stats saved → %s", stats_path)
        global _rtf_profile
        _rtf_profile = None   # force reload with new anchors included

        print("\n  Updated estimates:")
        for evt in sikat_events:
            period = evt.get("period")
            gt     = evt.get("gt", "00:00")
            player = evt.get("player", "Unknown")
            a_type = evt.get("actionType", "2pt")
            vid_ts = event_video_timestamp(period, gt)
            print(f"    Q{period} {gt}  {player} ({a_type})  → {_fmt(vid_ts)}")
    else:
        print("\n  No corrections made.")

    # --calibrate flag: stop here, don't generate clips
    if args.calibrate:
        print("\n── Next step ────────────────────────────────────────────────")
        qs = ' '.join(str(q) for q in args.quarters)
        min_flag = f" --min-clock {args.min_clock}" if args.min_clock else ""
        print(f"  To generate clips with these anchors:")
        print(f"  python3 vod_replay.py --quarters {qs} --skip-download{min_flag}")
        print("  To calibrate again with more anchors, re-run --calibrate.")
        print("─────────────────────────────────────────────────────────────\n")
        return

    # ── Download VOD ──────────────────────────────────────────────────────────
    if args.skip_download and os.path.isfile(VOD_FILE):
        saved = _saved_url()
        if saved and saved != config.YOUTUBE_STREAM_URL:
            log.warning("═" * 60)
            log.warning("  WRONG VIDEO DETECTED")
            log.warning("  Recording on disk : %s", saved)
            log.warning("  Current game URL  : %s", config.YOUTUBE_STREAM_URL)
            log.warning("  Re-downloading the correct video…")
            log.warning("═" * 60)
            if not download_vod(config.YOUTUBE_STREAM_URL, VOD_FILE):
                sys.exit(1)
        else:
            log.info("Reusing existing file: %s", VOD_FILE)
    else:
        if not download_vod(config.YOUTUBE_STREAM_URL, VOD_FILE):
            sys.exit(1)

    # ── Process each scoring play ─────────────────────────────────────────────
    publisher = Publisher()
    quarter_clips: dict = {}

    for evt in sikat_events:
        period = evt.get("period")
        gt     = evt.get("gt", "00:00")
        player = evt.get("player", "Unknown")
        shirt  = evt.get("shirtNumber", "")
        a_type = evt.get("actionType", "2pt")
        sub    = evt.get("subType", "")
        qualif = ", ".join(evt.get("qualifier") or [])

        vid_ts   = event_video_timestamp(period, gt)
        start    = vid_ts - config.CLIP_LEAD_SECONDS
        duration = config.CLIP_LEAD_SECONDS + config.CLIP_TAIL_SECONDS

        log.info(
            "Q%d %s  #%s %-20s  %-10s %-18s  [%s]  → video ~%s",
            period, gt, shirt, player, a_type, sub, qualif, _fmt(vid_ts),
        )

        safe_player = "".join(c if c.isalnum() else "_" for c in player)
        gt_tag      = gt.replace(":", "")
        clip_name   = f"Q{period}_{gt_tag}_{safe_player}_{a_type}.mp4"
        clip_path   = os.path.join(config.CLIPS_DIR, config.LEAGUE, config.TEAM, config.OPPONENT, clip_name)

        ok = cut_clip(VOD_FILE, start, duration, clip_path)
        if not ok:
            log.warning("Skipping clip for %s at ~%s", player, _fmt(vid_ts))
            continue

        if _is_anchored(period, gt):
            # Manually calibrated — timestamp is confirmed, skip audio check
            log.info("  ANCHORED    %-20s  timestamp manually confirmed", player)
            quarter_clips.setdefault(period, []).append(clip_path)
        elif config.AUDIO_VERIFY:
            # Interpolated — run audio verification as safety check
            result = audio_verify(clip_path, player_name=player)
            if result.passed:
                log.info("  AUDIO PASS  %-20s  %s", player, result.reason)
                quarter_clips.setdefault(period, []).append(clip_path)
            else:
                log.warning("  AUDIO FAIL  %-20s  %s", player, result.reason)
                if result.transcript:
                    log.warning("  Transcript: %s", result.transcript)
                move_to_review(clip_path)
        else:
            quarter_clips.setdefault(period, []).append(clip_path)

    # ── Compile quarter highlight reels ───────────────────────────────────────
    for q in sorted(quarter_clips):
        output = compile_quarter(q, quarter_clips[q])
        if output:
            publisher.publish(output, q)

    # ── Next step ─────────────────────────────────────────────────────────────
    done_quarters = sorted(args.quarters)
    last_q = done_quarters[-1]
    remaining = [q for q in [1, 2, 3, 4] if q > last_q]

    print("\n── Next step ────────────────────────────────────────────────")
    if remaining:
        next_q = remaining[0]
        print(f"  Q{last_q} done. Move on to Q{next_q}:")
        print(f"  python3 vod_replay.py --quarters {next_q} --skip-download")
    else:
        print(f"  All quarters done!")
        print(f"  Highlights are in: highlights/{config.LEAGUE}/{config.TEAM}/{config.OPPONENT}/")
    print("─────────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
