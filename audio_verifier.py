"""
audio_verifier.py
Second-layer authentication for SiKAT scoring clips.

Pipeline position:
    cut_clip() → audio_verifier.verify() → confirmed → compile
                                         → rejected  → review/

How it works:
  1. ffmpeg extracts a mono 16kHz WAV from the clip (Whisper's preferred input)
  2. Whisper transcribes the commentary audio
  3. The transcript is scanned for AUDIO_CONFIRM_KEYWORDS and AUDIO_REJECT_KEYWORDS
  4. A VerificationResult is returned with the decision, transcript, and matched words

Requires:  pip3 install openai-whisper
           ffmpeg in PATH
"""

import logging
import os
import shutil
import tempfile
import subprocess
from dataclasses import dataclass, field
from typing import Optional

import config

log = logging.getLogger(__name__)

# Lazy-load Whisper so the rest of the pipeline works even if it's not installed
_whisper_model = None


def _get_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper
            log.info("Loading Whisper model '%s'…", config.WHISPER_MODEL)
            _whisper_model = whisper.load_model(config.WHISPER_MODEL)
            log.info("Whisper model loaded.")
        except ImportError:
            log.error(
                "openai-whisper is not installed. "
                "Run: pip3 install openai-whisper"
            )
            raise
    return _whisper_model


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    passed: bool
    transcript: str
    matched_confirm: list = field(default_factory=list)
    matched_reject:  list = field(default_factory=list)
    reason: str = ""


# ── Core verification ─────────────────────────────────────────────────────────

def verify(clip_path: str, player_name: str = "") -> VerificationResult:
    """
    Transcribe the audio of a clip and decide whether it sounds like a
    SiKAT basket.

    Args:
        clip_path:   Path to the MP4 clip to verify.
        player_name: Optional scorer name from the FIBA feed — prepended to
                     AUDIO_CONFIRM_KEYWORDS for this specific check, making
                     matching more precise.

    Returns:
        VerificationResult
    """
    if not config.AUDIO_VERIFY:
        return VerificationResult(passed=True, transcript="", reason="verification disabled")

    if not os.path.isfile(clip_path):
        return VerificationResult(passed=False, transcript="", reason="clip file not found")

    # Build per-clip keyword list (add player's surname from feed data)
    extra_keywords = []
    if player_name:
        # Add each word of the name individually (e.g. "J. Felicilda" → ["felicilda"])
        for part in player_name.replace(".", " ").split():
            if len(part) > 2:
                extra_keywords.append(part.lower())

    confirm_keywords = config.AUDIO_CONFIRM_KEYWORDS + extra_keywords
    reject_keywords  = config.AUDIO_REJECT_KEYWORDS

    transcript = _transcribe(clip_path)
    if transcript is None:
        # Transcription failed — pass through rather than silently dropping clip
        return VerificationResult(
            passed=True,
            transcript="",
            reason="transcription failed — passing through",
        )

    transcript_lower = transcript.lower()
    log.debug("Transcript: %s", transcript)

    matched_reject  = [kw for kw in reject_keywords  if kw in transcript_lower]
    matched_confirm = [kw for kw in confirm_keywords if kw in transcript_lower]

    if matched_reject:
        return VerificationResult(
            passed=False,
            transcript=transcript,
            matched_confirm=matched_confirm,
            matched_reject=matched_reject,
            reason=f"reject keyword(s) found: {matched_reject}",
        )

    if matched_confirm:
        return VerificationResult(
            passed=True,
            transcript=transcript,
            matched_confirm=matched_confirm,
            reason=f"confirmed by: {matched_confirm}",
        )

    # No keywords matched — flag for review rather than silently drop.
    # Commentators may be speaking Filipino dialects not in the keyword list.
    return VerificationResult(
        passed=False,
        transcript=transcript,
        reason="no confirm keywords found — sent to review",
    )


def move_to_review(clip_path: str) -> str:
    """Move a failed clip to REVIEW_DIR and return the new path."""
    os.makedirs(config.REVIEW_DIR, exist_ok=True)
    dest = os.path.join(config.REVIEW_DIR, os.path.basename(clip_path))
    shutil.move(clip_path, dest)
    log.info("Moved to review: %s", dest)
    return dest


# ── Audio extraction + transcription ─────────────────────────────────────────

def _transcribe(clip_path: str) -> Optional[str]:
    """
    Extract mono 16kHz WAV from clip_path and transcribe with Whisper.
    Returns the transcript string, or None on error.
    """
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)
    try:
        # Extract audio: mono, 16kHz — Whisper's native format
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-i", clip_path,
            "-ac", "1",          # mono
            "-ar", "16000",      # 16kHz sample rate
            "-y", wav_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("ffmpeg audio extract failed: %s", result.stderr[-400:])
            return None

        model = _get_model()
        result = model.transcribe(
            wav_path,
            language=None,       # auto-detect (handles Filipino, English, mix)
            fp16=False,          # safer on CPU
            verbose=False,
        )
        return result.get("text", "").strip()

    except Exception as exc:
        log.error("Transcription error: %s", exc)
        return None
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
