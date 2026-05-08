"""
fiba_poller.py
Polls the FIBA LiveStats JSON feed and emits ScoringEvent / QuarterEndEvent
objects based on the `pbp` (play-by-play) array.

Real data structure (confirmed against game 2835583):
  pbp[n] = {
    "actionNumber":  847,          # monotonically increasing — key for diffs
    "tno":           1,            # team number (SIKAT_TNO for SiKAT)
    "actionType":    "2pt",        # "2pt" | "3pt" | "freethrow" | "period" | ...
    "subType":       "layup",      # shot style or "1of2"/"2of2" for FTs
    "success":       1,            # 1=made, 0=missed
    "scoring":       1,            # 1=points scored on this action
    "gt":            "09:38",      # game clock (time REMAINING in quarter MM:SS)
    "clock":         "09:38:00",   # same with sub-seconds
    "s1":            "92",         # team-1 running score
    "s2":            "74",         # team-2 running score
    "period":        4,            # quarter (1-4, 5+ for OT)
    "periodType":    "REGULAR",
    "player":        "J. Felicilda",
    "pno":           19,
    "shirtNumber":   "9",
    "qualifier":     ["fastbreak", "pointsinthepaint"],
  }

Quarter-end events:
  actionType == "period" AND subType == "end"   →  quarter just ended
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import requests

import config

log = logging.getLogger(__name__)


# ── Event types ───────────────────────────────────────────────────────────────

@dataclass
class ScoringEvent:
    """Represents one SiKAT scoring play detected from the feed."""
    wall_time:    float    # time.time() when the event was detected
    quarter:      int      # 1-4 (or 5+ for OT)
    game_clock:   str      # time remaining in quarter, e.g. "09:38"
    player:       str      # e.g. "J. Felicilda"
    shirt_number: str      # e.g. "9"
    action_type:  str      # "2pt" | "3pt" | "freethrow"
    sub_type:     str      # "drivinglayup", "jumpshot", "2of2", etc.
    qualifier:    list     # e.g. ["fastbreak", "pointsinthepaint"]
    sikat_score:  int      # SiKAT running score at this moment
    opp_score:    int      # Opponent running score at this moment
    points:       int      # 1, 2, or 3
    action_number: int     # from feed — unique event ID


@dataclass
class QuarterEndEvent:
    wall_time:   float
    quarter:     int
    sikat_score: int
    opp_score:   int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fiba_url() -> str:
    return config.FIBA_URL_TEMPLATE.format(game_id=config.GAME_ID)


def _fetch_data() -> Optional[dict]:
    try:
        resp = requests.get(_fiba_url(), timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("Feed fetch failed: %s", exc)
        return None


def _scores(entry: dict) -> tuple[int, int]:
    """
    Return (team_score, opp_score) from a pbp entry.
    s1 = team-1 score, s2 = team-2 score.
    """
    s1 = int(entry.get("s1", 0) or 0)
    s2 = int(entry.get("s2", 0) or 0)
    if config.TEAM_TNO == 1:
        return s1, s2
    else:
        return s2, s1


def _points_for(action_type: str) -> int:
    return {"freethrow": 1, "2pt": 2, "3pt": 3}.get(action_type, 2)


# ── Main polling loop ─────────────────────────────────────────────────────────

def run_poller(
    on_score: Callable[[ScoringEvent], None],
    on_quarter_end: Callable[[QuarterEndEvent], None],
    stop_flag: Callable[[], bool],
) -> None:
    """
    Polls the FIBA feed every POLL_INTERVAL seconds.
    Calls on_score() for every new SiKAT scoring event.
    Calls on_quarter_end() when a quarter-end marker appears.
    Runs until stop_flag() returns True.
    """
    max_action_seen: int = -1   # highest actionNumber processed so far
    log.info(
        "Poller started — game %s, polling every %ds",
        config.GAME_ID, config.POLL_INTERVAL,
    )

    while not stop_flag():
        data = _fetch_data()
        if data is None:
            time.sleep(config.POLL_INTERVAL)
            continue

        now = time.time()
        pbp: List[dict] = data.get("pbp") or []

        # Sort by actionNumber so we process in chronological order
        pbp_sorted = sorted(pbp, key=lambda e: e.get("actionNumber", 0))

        # Only look at events we haven't seen yet
        new_events = [e for e in pbp_sorted if e.get("actionNumber", 0) > max_action_seen]

        for entry in new_events:
            a_type  = entry.get("actionType", "")
            sub     = entry.get("subType", "")
            tno     = entry.get("tno", 0)
            success = int(entry.get("success", 0))
            scoring = int(entry.get("scoring", 0))
            period  = int(entry.get("period", 0))
            gt      = entry.get("gt", "")
            an      = entry.get("actionNumber", 0)
            sikat_s, opp_s = _scores(entry)

            # ── Quarter-end marker ────────────────────────────────────────────
            if a_type == "period" and sub == "end":
                log.info("Quarter %d ended | SiKAT %d – OPP %d", period, sikat_s, opp_s)
                on_quarter_end(QuarterEndEvent(
                    wall_time=now,
                    quarter=period,
                    sikat_score=sikat_s,
                    opp_score=opp_s,
                ))

            # ── Team scoring play ─────────────────────────────────────────────
            elif (tno == config.TEAM_TNO
                  and success == 1
                  and scoring == 1
                  and a_type in ("2pt", "3pt", "freethrow")):

                pts = _points_for(a_type)
                evt = ScoringEvent(
                    wall_time=now,
                    quarter=period,
                    game_clock=gt,
                    player=entry.get("player", ""),
                    shirt_number=entry.get("shirtNumber", ""),
                    action_type=a_type,
                    sub_type=sub,
                    qualifier=entry.get("qualifier") or [],
                    sikat_score=sikat_s,
                    opp_score=opp_s,
                    points=pts,
                    action_number=an,
                )
                tags = ", ".join(evt.qualifier) if evt.qualifier else ""
                log.info(
                    "SCORE  %s #%s | %s %s [%s] | Q%d %s | SiKAT %d – OPP %d",
                    evt.player, evt.shirt_number,
                    evt.action_type, evt.sub_type, tags,
                    evt.quarter, evt.game_clock,
                    evt.sikat_score, evt.opp_score,
                )
                on_score(evt)

            # Update high-water mark
            if an > max_action_seen:
                max_action_seen = an

        time.sleep(config.POLL_INTERVAL)

    log.info("Poller stopped.")
