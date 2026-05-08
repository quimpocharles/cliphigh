"""
clipper.py
Cuts individual scoring clips from the ongoing recording and compiles
per-quarter highlight reels.
"""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

import config
from fiba_poller import ScoringEvent, QuarterEndEvent
from stream_recorder import StreamRecorder
from audio_verifier import verify as audio_verify, move_to_review

log = logging.getLogger(__name__)


def _safe_filename(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)


class Clipper:
    def __init__(self, recorder: StreamRecorder):
        self._recorder = recorder
        self._lock = threading.Lock()
        # quarter → list of clip file paths produced
        self._quarter_clips: dict[int, List[str]] = {}

    # ── Individual scoring clips ──────────────────────────────────────────────

    def cut_scoring_clip(self, evt: ScoringEvent) -> None:
        """
        Cut a clip around a scoring event and store it for later compilation.
        Runs synchronously but is typically called from a thread.
        """
        if not self._recorder.is_alive() and self._recorder.recording_start_time is None:
            log.warning("No recording active — cannot cut clip.")
            return

        # Calculate position in the recording file
        video_offset = self._recorder.video_offset(evt.wall_time)
        start_sec = max(0.0, video_offset - config.CLIP_LEAD_SECONDS)
        duration  = config.CLIP_LEAD_SECONDS + config.CLIP_TAIL_SECONDS

        # Wait until enough footage has been recorded for the tail
        tail_needed = video_offset + config.CLIP_TAIL_SECONDS
        while self._recorder.seconds_recorded() < tail_needed:
            log.debug("Waiting for recording to reach %.1fs…", tail_needed)
            time.sleep(2)

        player_tag = _safe_filename(evt.player or "FT")
        timestamp  = time.strftime("%H%M%S", time.localtime(evt.wall_time))
        clip_name  = f"Q{evt.quarter}_{timestamp}_{player_tag}_{evt.action_type}.mp4"
        clip_dir   = os.path.join(config.CLIPS_DIR, config.LEAGUE, config.TEAM, config.OPPONENT)
        clip_path  = os.path.join(clip_dir, clip_name)

        Path(clip_dir).mkdir(parents=True, exist_ok=True)
        success = self._run_ffmpeg_clip(
            input_file=config.RECORDING_FILE,
            start=start_sec,
            duration=duration,
            output=clip_path,
        )

        if not success:
            log.error("Failed to cut clip for %s at offset %.1fs", evt.player, start_sec)
            return

        # ── Audio verification ────────────────────────────────────────────────
        result = audio_verify(clip_path, player_name=evt.player)
        if result.passed:
            log.info("Clip VERIFIED  %s  (%s)", clip_path, result.reason)
            with self._lock:
                self._quarter_clips.setdefault(evt.quarter, []).append(clip_path)
        else:
            log.warning("Clip REVIEW    %s  (%s)", clip_path, result.reason)
            if result.transcript:
                log.warning("  Transcript: %s", result.transcript)
            move_to_review(clip_path)

    # ── Quarter highlight compilation ─────────────────────────────────────────

    def compile_quarter(self, qevt: QuarterEndEvent) -> Optional[str]:
        """
        Wait QUARTER_COMPILE_DELAY seconds, then concatenate all clips from
        the quarter into a single highlight reel.  Returns output path or None.
        """
        quarter = qevt.quarter
        log.info(
            "Q%d ended. Waiting %ds before compiling highlight reel…",
            quarter, config.QUARTER_COMPILE_DELAY
        )
        time.sleep(config.QUARTER_COMPILE_DELAY)

        with self._lock:
            clips = list(self._quarter_clips.get(quarter, []))

        if not clips:
            log.warning("Q%d: no clips to compile.", quarter)
            return None

        date_str    = time.strftime("%Y%m%d")
        quarter_dir = os.path.join(config.HIGHLIGHTS_DIR, config.LEAGUE, config.TEAM, config.OPPONENT, str(quarter))
        Path(quarter_dir).mkdir(parents=True, exist_ok=True)
        output     = os.path.join(quarter_dir, f"SiKAT_Q{quarter}_{date_str}.mp4")
        concat_txt = os.path.join(quarter_dir, f"concat_Q{quarter}.txt")

        # Write ffmpeg concat demuxer input file
        with open(concat_txt, "w") as f:
            for clip in clips:
                abs_clip = os.path.abspath(clip)
                f.write(f"file '{abs_clip}'\n")

        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_txt,
            "-c", "copy",
            "-y",
            output,
        ]
        log.info("Compiling Q%d highlight: %d clips → %s", quarter, len(clips), output)
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.error("ffmpeg concat failed:\n%s", result.stderr[-1000:])
            return None

        os.remove(concat_txt)
        log.info("Q%d highlight ready: %s", quarter, output)
        return output

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _run_ffmpeg_clip(input_file: str, start: float, duration: float, output: str) -> bool:
        """Cut a segment from input_file using ffmpeg stream-copy."""
        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-ss", f"{start:.3f}",
            "-i", input_file,
            "-t", f"{duration:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-y",
            output,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("ffmpeg clip error:\n%s", result.stderr[-800:])
            return False
        return True
