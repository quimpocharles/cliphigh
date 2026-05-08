"""
stream_recorder.py
Starts a background recording of the MPBL YouTube live stream using yt-dlp
(to resolve the direct stream URL) then ffmpeg (to write a seekable TS file).

The recording start wall-clock time is stored so that video timestamps can be
calculated as:  offset = detection_wall_time - recording_start_time
"""

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)


class StreamRecorder:
    def __init__(self):
        self.recording_start_time: Optional[float] = None
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._stream_url: Optional[str] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Resolve the YouTube stream URL and start ffmpeg recording.
        Returns True on success.
        """
        if not config.YOUTUBE_STREAM_URL:
            log.error("YOUTUBE_STREAM_URL is not set in config.py. Recording skipped.")
            return False

        Path(config.RECORDING_DIR).mkdir(exist_ok=True)

        log.info("Resolving stream URL via yt-dlp…")
        self._stream_url = self._resolve_stream_url(config.YOUTUBE_STREAM_URL)
        if not self._stream_url:
            log.error("Could not resolve YouTube stream URL.")
            return False

        log.info("Stream URL resolved. Starting ffmpeg recording → %s", config.RECORDING_FILE)
        self._ffmpeg_proc = self._start_ffmpeg(self._stream_url, config.RECORDING_FILE)
        self.recording_start_time = time.time()

        # Give ffmpeg a moment to begin writing
        time.sleep(3)
        if self._ffmpeg_proc.poll() is not None:
            log.error("ffmpeg exited immediately (return code %d).", self._ffmpeg_proc.returncode)
            return False

        log.info("Recording started at wall time %.2f", self.recording_start_time)
        return True

    def stop(self) -> None:
        """Gracefully terminate the ffmpeg recording."""
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
            log.info("Stopping ffmpeg recording…")
            self._ffmpeg_proc.terminate()
            try:
                self._ffmpeg_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()
        log.info("Recording stopped.")

    def seconds_recorded(self) -> float:
        """Return how many seconds of footage have been captured so far."""
        if self.recording_start_time is None:
            return 0.0
        return time.time() - self.recording_start_time

    def video_offset(self, wall_time: float) -> float:
        """
        Convert a wall-clock timestamp to the corresponding byte-offset time
        (in seconds) within the recording file.
        """
        if self.recording_start_time is None:
            return 0.0
        return max(0.0, wall_time - self.recording_start_time)

    def is_alive(self) -> bool:
        return self._ffmpeg_proc is not None and self._ffmpeg_proc.poll() is None

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_stream_url(youtube_url: str) -> Optional[str]:
        """Use yt-dlp to get the best direct stream URL (m3u8 or similar)."""
        try:
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                    "--get-url",
                    youtube_url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            urls = result.stdout.strip().splitlines()
            if not urls or not urls[0]:
                log.error("yt-dlp returned no URL. stderr: %s", result.stderr[:500])
                return None
            # If DASH (video+audio separate), use the first (video) URL;
            # ffmpeg's -i flag will handle HLS m3u8 playlists natively.
            return urls[0]
        except FileNotFoundError:
            log.error("yt-dlp not found. Install it: pip install yt-dlp")
            return None
        except subprocess.TimeoutExpired:
            log.error("yt-dlp timed out resolving stream URL.")
            return None

    @staticmethod
    def _start_ffmpeg(stream_url: str, output_file: str) -> subprocess.Popen:
        """Launch ffmpeg to record the stream to a seekable MPEG-TS file."""
        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-i", stream_url,
            "-c", "copy",          # copy streams without re-encoding
            "-y",                  # overwrite if exists
            output_file,
        ]
        log.debug("ffmpeg cmd: %s", " ".join(cmd))
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(config.RECORDING_DIR, "ffmpeg.log"), "w"),
        )
