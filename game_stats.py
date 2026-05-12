"""
game_stats.py — Per-game RTF learning for the SiKAT highlight pipeline.

After each calibrated game, saves confirmed RTF segments to
game_stats/{game_id}.json.  These are aggregated across games to build a
league-specific RTF profile that makes initial timestamp projections
progressively more accurate — reducing the number of manual anchors needed.

RTF (real-time factor) = real video seconds elapsed / game-clock seconds elapsed.
  RTF ~1.0  → pure running clock, no stoppages
  RTF ~2.0  → normal MPBL play with regular fouls
  RTF ~5+   → intentional fouling / reviews in the final minute
"""

import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

STATS_DIR = "game_stats"

# Each quarter is divided into four clock-remaining buckets.
# The bucket of a segment is determined by the clock remaining at the START
# of that segment.  Thresholds are in seconds of game clock remaining.
BUCKETS = [
    ("early",   480, 600),  # 8:00 – 10:00 remaining  (running clock)
    ("mid",     240, 480),  # 4:00 –  8:00 remaining  (normal stoppages)
    ("late",     60, 240),  # 1:00 –  4:00 remaining  (foul accumulation)
    ("crunch",    0,  60),  # 0:00 –  1:00 remaining  (intentional fouling)
]


def clock_bucket(clock_remaining_secs: int) -> str:
    """Map game clock remaining (seconds) to a bucket label."""
    for label, lo, hi in BUCKETS:
        if lo < clock_remaining_secs <= hi:
            return label
    return "crunch"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_gt(gt: str) -> int:
    m, s = gt.split(":")
    return int(m) * 60 + int(s)


def _to_abs(q: int, gt: str, period_len: int) -> int:
    """(quarter, game_clock_remaining) → absolute game-seconds from tip-off."""
    return (q - 1) * period_len + (period_len - _parse_gt(gt))


# ── Core extraction ───────────────────────────────────────────────────────────

def extract_segments(anchors: list, tipoff_video_secs: int,
                     period_len: int = 600) -> dict:
    """
    Extract per-segment RTF values and quarter break durations from a
    confirmed CALIBRATION_ANCHORS list.

    Intra-quarter segments with RTF < 0.9 are silently dropped (bad anchors).
    Cross-quarter segments are used only to derive break durations.

    Returns:
        {
            "segments": [
                {"quarter": int, "clock_from": "MM:SS", "clock_to": "MM:SS",
                 "rtf": float, "bucket": str},
                ...
            ],
            "breaks": {
                "q1_q2":   int | None,   # broadcast seconds for Q1→Q2 break
                "halftime": int | None,
                "q3_q4":   int | None,
            }
        }
    """
    # Build sorted, deduplicated (video_secs, abs_game_secs) table
    points = [(tipoff_video_secs, 0)]
    for v, q, gt in anchors:
        points.append((v, _to_abs(q, gt, period_len)))
    points.sort(key=lambda x: x[1])

    seen_abs = set()
    deduped = []
    for p in points:
        if p[1] not in seen_abs:
            seen_abs.add(p[1])
            deduped.append(p)
    points = deduped

    segments = []
    breaks = {"q1_q2": None, "halftime": None, "q3_q4": None}

    for i in range(len(points) - 1):
        v0, a0 = points[i]
        v1, a1 = points[i + 1]

        if a1 <= a0:
            continue

        video_delta = v1 - v0
        game_delta  = a1 - a0
        rtf = video_delta / game_delta

        q0 = a0 // period_len + 1
        q1 = a1 // period_len + 1

        if q0 == q1:
            # ── Intra-quarter segment ─────────────────────────────────────
            if rtf < 0.9:
                continue  # physically impossible → bad anchor, skip

            clock_from = period_len - (a0 - (q0 - 1) * period_len)
            clock_to   = period_len - (a1 - (q0 - 1) * period_len)
            segments.append({
                "quarter":    q0,
                "clock_from": f"{clock_from // 60:02d}:{clock_from % 60:02d}",
                "clock_to":   f"{clock_to   // 60:02d}:{clock_to   % 60:02d}",
                "rtf":        round(rtf, 3),
                "bucket":     clock_bucket(clock_from),
            })

        else:
            # ── Cross-quarter segment — derive break duration ─────────────
            # Only attempt this for adjacent quarters.  A jump from e.g. Q2
            # to Q4 (no Q3 anchors) spans an unknown number of breaks and
            # cannot produce a meaningful single break estimate.
            if q1 != q0 + 1:
                continue

            prev_segs = [s for s in segments if s["quarter"] == q0]
            next_segs = [s for s in segments if s["quarter"] == q1]
            rtf_prev = (sum(s["rtf"] for s in prev_segs) / len(prev_segs)
                        if prev_segs else 2.0)
            rtf_next = (sum(s["rtf"] for s in next_segs) / len(next_segs)
                        if next_segs else 2.0)

            remaining_prev  = q0 * period_len - a0   # game secs left in q0
            elapsed_next    = a1 - (q1 - 1) * period_len  # game secs into q1
            est_game_video  = remaining_prev * rtf_prev + elapsed_next * rtf_next
            break_dur       = round(video_delta - est_game_video)

            if break_dur > 30:   # sanity floor — breaks are always > 30 s
                if q0 == 1:
                    breaks["q1_q2"]   = break_dur
                elif q0 == 2:
                    breaks["halftime"] = break_dur
                elif q0 == 3:
                    breaks["q3_q4"]   = break_dur

    return {"segments": segments, "breaks": breaks}


# ── Persistence ───────────────────────────────────────────────────────────────

def save(config_module) -> str:
    """
    Extract timing stats from the current config and write
    game_stats/{game_id}.json.  Called automatically at the end of
    each --calibrate run.  Overwrites any previous file for this game_id.
    """
    Path(STATS_DIR).mkdir(exist_ok=True)
    period_len = config_module.PERIOD_LENGTH * 60

    result = extract_segments(
        config_module.CALIBRATION_ANCHORS,
        config_module.TIPOFF_VIDEO_SECONDS,
        period_len,
    )

    stats = {
        "game_id":       config_module.GAME_ID,
        "league":        config_module.LEAGUE,
        "team":          config_module.TEAM,
        "opponent":      config_module.OPPONENT,
        "date":          time.strftime("%Y-%m-%d"),
        "period_length": config_module.PERIOD_LENGTH,
        "anchor_count":  len(config_module.CALIBRATION_ANCHORS),
        "segments":      result["segments"],
        "breaks":        result["breaks"],
    }

    path = os.path.join(STATS_DIR, f"{config_module.GAME_ID}.json")
    with open(path, "w") as f:
        json.dump(stats, f, indent=2)
    return path


# ── Profile loading ───────────────────────────────────────────────────────────

def load_profile(league: str, stats_dir: str = STATS_DIR) -> dict:
    """
    Read all game_stats/*.json files for this league and compute the
    average RTF per (quarter, bucket) across all confirmed segments.

    Returns a dict keyed by (quarter: int, bucket: str) → avg_rtf: float.
    Returns {} if no stats exist yet (caller falls back to REAL_TIME_FACTOR).
    """
    if not os.path.isdir(stats_dir):
        return {}

    groups: dict = defaultdict(list)
    loaded = 0

    for fname in sorted(os.listdir(stats_dir)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(stats_dir, fname)) as f:
                data = json.load(f)
        except Exception:
            continue
        if data.get("league") != league:
            continue
        for seg in data.get("segments", []):
            groups[(seg["quarter"], seg["bucket"])].append(seg["rtf"])
        loaded += 1

    if not loaded:
        return {}

    return {k: round(sum(v) / len(v), 3) for k, v in groups.items()}


def load_break_profile(league: str, stats_dir: str = STATS_DIR) -> dict:
    """
    Read all game_stats/*.json files for this league and compute the average
    break duration for each quarter transition.

    Returns {"q1_q2": int|None, "halftime": int|None, "q3_q4": int|None}.
    A key is None when no games have measured that break yet.
    """
    if not os.path.isdir(stats_dir):
        return {"q1_q2": None, "halftime": None, "q3_q4": None}

    groups: dict = defaultdict(list)

    for fname in sorted(os.listdir(stats_dir)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(stats_dir, fname)) as f:
                data = json.load(f)
        except Exception:
            continue
        if data.get("league") != league:
            continue
        for key in ("q1_q2", "halftime", "q3_q4"):
            val = data.get("breaks", {}).get(key)
            if val is not None and val > 30:
                groups[key].append(val)

    return {
        key: (round(sum(vals) / len(vals)) if vals else None)
        for key, vals in [
            ("q1_q2",   groups.get("q1_q2",   [])),
            ("halftime", groups.get("halftime", [])),
            ("q3_q4",   groups.get("q3_q4",   [])),
        ]
    }


def profile_summary(profile: dict) -> str:
    """Return a human-readable summary of a loaded RTF profile."""
    if not profile:
        return "  (no historical data — using config.REAL_TIME_FACTOR fallback)"
    lines = []
    for q in range(1, 5):
        parts = []
        for label, _, _ in BUCKETS:
            rtf = profile.get((q, label))
            if rtf is not None:
                parts.append(f"{label}={rtf:.2f}")
        if parts:
            lines.append(f"  Q{q}: {', '.join(parts)}")
    return "\n".join(lines) if lines else "  (no data)"
