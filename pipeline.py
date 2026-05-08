#!/usr/bin/env python3
"""
pipeline.py — SiKAT Zamboanga Automated Highlight Pipeline
===========================================================
Usage:
    # Set YOUTUBE_STREAM_URL and GAME_ID in config.py first, then:
    python pipeline.py

    # Dry-run (poll only, no recording/clipping):
    python pipeline.py --dry-run

    # Override game ID at runtime:
    python pipeline.py --game-id 2835583
"""

import argparse
import logging
import signal
import sys
import threading
import time

import config
from fiba_poller import ScoringEvent, QuarterEndEvent, run_poller
from stream_recorder import StreamRecorder
from clipper import Clipper
from publisher import Publisher

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline")


# ── Globals ───────────────────────────────────────────────────────────────────
_stop = threading.Event()


def _handle_signal(sig, frame):
    log.info("Signal %s received — shutting down…", sig)
    _stop.set()


signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Pipeline logic ────────────────────────────────────────────────────────────

def build_on_score(clipper: Clipper, dry_run: bool):
    def on_score(evt: ScoringEvent):
        label = f"{evt.points}pt"
        if evt.sub_type:
            label += f" {evt.sub_type}"
        log.info(
            "🏀 SCORE  %s #%s | %s | Q%d %s | SiKAT %d – OPP %d",
            evt.player, evt.shirt_number,
            label, evt.quarter, evt.game_clock,
            evt.sikat_score, evt.opp_score,
        )
        if dry_run:
            log.info("  [dry-run] Skipping clip cut.")
            return
        # Run clip in a background thread so polling isn't delayed
        t = threading.Thread(
            target=clipper.cut_scoring_clip,
            args=(evt,),
            daemon=True,
            name=f"clip-Q{evt.quarter}-{evt.game_clock}",
        )
        t.start()
    return on_score


def build_on_quarter_end(clipper: Clipper, publisher: Publisher, dry_run: bool):
    def on_quarter_end(qevt: QuarterEndEvent):
        log.info(
            "⏱ QUARTER %d ended | SiKAT %d – OPP %d",
            qevt.quarter, qevt.sikat_score, qevt.opp_score,
        )
        if dry_run:
            log.info("  [dry-run] Skipping highlight compilation.")
            return
        def compile_and_publish():
            output_path = clipper.compile_quarter(qevt)
            if output_path:
                publisher.publish(output_path, qevt.quarter)
        t = threading.Thread(
            target=compile_and_publish,
            daemon=True,
            name=f"compile-Q{qevt.quarter}",
        )
        t.start()
    return on_quarter_end


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SiKAT Highlight Pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="Poll feed and log events without recording or clipping")
    parser.add_argument("--game-id", default=None,
                        help="Override GAME_ID from config.py")
    args = parser.parse_args()

    if args.game_id:
        config.GAME_ID = args.game_id
        log.info("Game ID overridden to: %s", config.GAME_ID)

    log.info("═" * 60)
    log.info("  SiKAT Zamboanga Highlight Pipeline")
    log.info("  Game ID  : %s", config.GAME_ID)
    log.info("  Stream   : %s", config.YOUTUBE_STREAM_URL or "(none — dry-run only)")
    log.info("  Dry-run  : %s", args.dry_run)
    log.info("═" * 60)

    # ── Components ────────────────────────────────────────────────────────────
    recorder  = StreamRecorder()
    clipper   = Clipper(recorder)
    publisher = Publisher()

    # ── Start recording (unless dry-run) ─────────────────────────────────────
    if not args.dry_run:
        if not config.YOUTUBE_STREAM_URL:
            log.error("YOUTUBE_STREAM_URL is not set in config.py. Use --dry-run or set the URL.")
            sys.exit(1)
        ok = recorder.start()
        if not ok:
            log.error("Failed to start recording. Exiting.")
            sys.exit(1)

    # ── Wire up callbacks ─────────────────────────────────────────────────────
    on_score       = build_on_score(clipper, args.dry_run)
    on_quarter_end = build_on_quarter_end(clipper, publisher, args.dry_run)

    # ── Run poller (blocks until _stop is set) ────────────────────────────────
    try:
        run_poller(
            on_score=on_score,
            on_quarter_end=on_quarter_end,
            stop_flag=_stop.is_set,
        )
    finally:
        if not args.dry_run:
            recorder.stop()
        log.info("Pipeline shut down cleanly.")


if __name__ == "__main__":
    main()
