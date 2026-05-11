"""
audio_verifier.py
Verifies that a scoring clip actually contains a basket moment by detecting
crowd noise energy spikes in the audio.

How it works:
  1. ffmpeg extracts a mono 16kHz WAV from the clip
  2. Audio is split into the lead window (before basket) and tail window (after basket)
  3. RMS energy is computed for each window
  4. A crowd noise spike in the tail confirms the basket moment
  5. Very quiet audio (dead ball, timeout) is flagged for review

This approach is language-agnostic — works for any broadcaster, any language.
No external model required beyond ffmpeg and numpy (already installed).

Requires: ffmpeg in PATH, numpy
"""

import logging
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

import config

log = logging.getLogger(__name__)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    passed:          bool
    reason:          str = ""
    lead_rms:        float = 0.0
    tail_rms:        float = 0.0
    overall_rms:     float = 0.0
    # Legacy fields kept for compatibility with any code that reads these
    transcript:      str = ""
    matched_confirm: list = field(default_factory=list)
    matched_reject:  list = field(default_factory=list)


# ── Core verification ─────────────────────────────────────────────────────────

def verify(clip_path: str, player_name: str = "") -> VerificationResult:
    """
    Verify a clip by detecting crowd noise energy around the basket moment.

    The basket is expected at config.CLIP_LEAD_SECONDS into the clip.
    A spike in crowd noise after that point confirms the play.

    Args:
        clip_path:   Path to the MP4 clip.
        player_name: Unused — kept for interface compatibility.

    Returns:
        VerificationResult
    """
    if not config.AUDIO_VERIFY:
        return VerificationResult(passed=True, reason="verification disabled")

    if not os.path.isfile(clip_path):
        return VerificationResult(passed=False, reason="clip file not found")

    audio = _extract_audio(clip_path)
    if audio is None:
        # Extraction failed — pass through rather than silently drop the clip
        return VerificationResult(passed=True, reason="audio extraction failed — passing through")

    samples, sample_rate = audio
    basket_frame = int(config.CLIP_LEAD_SECONDS * sample_rate)

    lead_samples = samples[:basket_frame]
    tail_samples = samples[basket_frame:]

    lead_rms    = _rms(lead_samples)
    tail_rms    = _rms(tail_samples)
    overall_rms = _rms(samples)

    # Thresholds (16-bit PCM scale: 0–32767)
    # Below this the clip is essentially silent — dead ball or wrong moment
    SILENCE_THRESHOLD = config.CROWD_NOISE_SILENCE_THRESHOLD
    # Tail must be at least this much louder than lead to confirm crowd reaction
    SPIKE_RATIO       = config.CROWD_NOISE_SPIKE_RATIO

    log.debug(
        "Crowd noise — overall: %.0f  lead: %.0f  tail: %.0f  ratio: %.2f",
        overall_rms, lead_rms, tail_rms,
        tail_rms / lead_rms if lead_rms > 0 else 0,
    )

    if overall_rms < SILENCE_THRESHOLD:
        return VerificationResult(
            passed=False,
            reason=f"silent clip — likely dead ball or wrong moment (RMS {overall_rms:.0f})",
            lead_rms=lead_rms, tail_rms=tail_rms, overall_rms=overall_rms,
        )

    if lead_rms > 0 and tail_rms >= lead_rms * SPIKE_RATIO:
        return VerificationResult(
            passed=True,
            reason=f"crowd noise spike after basket (lead {lead_rms:.0f} → tail {tail_rms:.0f})",
            lead_rms=lead_rms, tail_rms=tail_rms, overall_rms=overall_rms,
        )

    # Consistently loud throughout — fast break, crowd already excited
    if overall_rms >= SILENCE_THRESHOLD * config.CROWD_NOISE_CONSISTENT_FACTOR:
        return VerificationResult(
            passed=True,
            reason=f"consistent crowd noise throughout (RMS {overall_rms:.0f})",
            lead_rms=lead_rms, tail_rms=tail_rms, overall_rms=overall_rms,
        )

    return VerificationResult(
        passed=False,
        reason=f"no crowd noise spike detected (lead {lead_rms:.0f}, tail {tail_rms:.0f})",
        lead_rms=lead_rms, tail_rms=tail_rms, overall_rms=overall_rms,
    )


def move_to_review(clip_path: str) -> str:
    """Move a failed clip to REVIEW_DIR and return the new path."""
    os.makedirs(config.REVIEW_DIR, exist_ok=True)
    dest = os.path.join(config.REVIEW_DIR, os.path.basename(clip_path))
    shutil.move(clip_path, dest)
    log.info("Moved to review: %s", dest)
    return dest


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _extract_audio(clip_path: str) -> Optional[tuple]:
    """
    Extract mono 16kHz PCM audio from clip_path using ffmpeg.
    Returns (numpy_array, sample_rate) or None on error.
    """
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)
    try:
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-i", clip_path,
            "-ac", "1",        # mono
            "-ar", "16000",    # 16kHz
            "-y", wav_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("ffmpeg audio extract failed: %s", result.stderr[-400:])
            return None

        with wave.open(wav_path, "rb") as wf:
            sample_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        return samples, sample_rate

    except Exception as exc:
        log.error("Audio extraction error: %s", exc)
        return None
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def _rms(samples: np.ndarray) -> float:
    """Root mean square energy of a sample array."""
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))
