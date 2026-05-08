"""
publisher.py
Outputs finished highlight reels either locally or to a Facebook Page.
"""

import logging
import os
import time
from datetime import date

import requests

import config

log = logging.getLogger(__name__)


class Publisher:
    def publish(self, video_path: str, quarter: int) -> bool:
        """
        Publish a quarter highlight video.
        Uses Facebook if credentials are configured, otherwise saves locally.
        """
        if not os.path.isfile(video_path):
            log.error("Video file not found: %s", video_path)
            return False

        if config.FB_ACCESS_TOKEN and config.FB_PAGE_ID:
            return self._post_to_facebook(video_path, quarter)
        else:
            return self._save_locally(video_path, quarter)

    # ── Facebook ──────────────────────────────────────────────────────────────

    def _post_to_facebook(self, video_path: str, quarter: int) -> bool:
        """
        Upload video to a Facebook Page using the Graph API video endpoint.
        Requires pages_manage_posts + publish_video permissions on the token.
        """
        message = config.FB_POST_MESSAGE_TEMPLATE.format(
            quarter=quarter,
            date=date.today().strftime("%B %d, %Y"),
        )
        url = f"https://graph-video.facebook.com/v19.0/{config.FB_PAGE_ID}/videos"

        log.info("Uploading Q%d highlight to Facebook…", quarter)
        try:
            with open(video_path, "rb") as f:
                resp = requests.post(
                    url,
                    data={"description": message, "access_token": config.FB_ACCESS_TOKEN},
                    files={"source": (os.path.basename(video_path), f, "video/mp4")},
                    timeout=300,
                )
            resp.raise_for_status()
            video_id = resp.json().get("id", "unknown")
            log.info("Facebook upload success — video ID: %s", video_id)
            return True
        except requests.RequestException as exc:
            log.error("Facebook upload failed: %s", exc)
            return False

    # ── Local fallback ────────────────────────────────────────────────────────

    @staticmethod
    def _save_locally(video_path: str, quarter: int) -> bool:
        """
        No FB credentials — just confirm the file exists and log its location.
        """
        size_mb = os.path.getsize(video_path) / 1_048_576
        log.info(
            "Q%d highlight saved locally: %s (%.1f MB)",
            quarter, os.path.abspath(video_path), size_mb
        )
        print(f"\n✓ Q{quarter} highlight ready → {os.path.abspath(video_path)} ({size_mb:.1f} MB)\n")
        return True
