"""YouTube Data API v3 publisher — uploads a rendered video to a user's channel.

Uses the installed-app OAuth flow (not a service account): a YouTube channel
belongs to a Google *user*, so uploading on someone's behalf requires their
consent, not machine-to-machine credentials. First call opens a browser for
one-time consent; the refresh token is cached at ``YOUTUBE_TOKEN_FILE`` so
later uploads don't re-prompt.

Setup (see .env.example):
  1. Google Cloud Console -> enable "YouTube Data API v3" on a project.
  2. OAuth consent screen -> External -> add scope
     https://www.googleapis.com/auth/youtube.upload -> add yourself as a test user.
  3. Credentials -> Create Credentials -> OAuth client ID -> Desktop app -> download JSON.
  4. Set YOUTUBE_CLIENT_SECRET_FILE to that JSON's path.

Safety: ``privacy_status`` defaults to "private". Anything that ships as
"public" is a real, irreversible publish action to a live channel — the
calling agent must treat that the same as any other publish-with-approval
step, not a default.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YoutubeUpload(BaseTool):
    name = "youtube_upload"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "youtube"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = ["env:YOUTUBE_CLIENT_SECRET_FILE", "python:googleapiclient"]
    install_instructions = (
        "Set up YouTube upload:\n"
        "  1. https://console.cloud.google.com/ -> create/select a project\n"
        "  2. APIs & Services > Library -> enable 'YouTube Data API v3'\n"
        "  3. APIs & Services > OAuth consent screen -> External -> add scope\n"
        "     https://www.googleapis.com/auth/youtube.upload -> add yourself as a test user\n"
        "  4. APIs & Services > Credentials -> Create Credentials > OAuth client ID\n"
        "     -> Application type: Desktop app -> download the JSON\n"
        "  5. Set YOUTUBE_CLIENT_SECRET_FILE to the downloaded file's path in .env\n"
        "     (tip: name it gcp-oauth-client.json in the project root - already gitignored)\n"
        "First upload opens a browser for one-time consent; the token is then\n"
        "cached at YOUTUBE_TOKEN_FILE (default: .youtube-token.json in the project root)."
    )
    agent_skills = []

    capabilities = ["upload_video", "publish_youtube"]
    supports = {
        "resumable_upload": True,
        "scheduled_publish": True,
        "playlists": True,
        "free": True,
    }
    best_for = [
        "publishing a finished render directly to a YouTube channel",
        "scheduled or immediate publish with SEO metadata",
    ]
    not_good_for = [
        "channels not owned/managed by the authenticated Google account",
        "bulk/mass uploading (YouTube enforces daily upload quotas)",
    ]

    input_schema = {
        "type": "object",
        "required": ["video_path", "title"],
        "properties": {
            "video_path": {"type": "string", "description": "Path to the final rendered video."},
            "title": {"type": "string", "description": "Video title (max 100 chars)."},
            "description": {"type": "string", "default": ""},
            "tags": {"type": "array", "items": {"type": "string"}},
            "category_id": {
                "type": "string",
                "default": "27",
                "description": "YouTube category id. 27 = Education, 28 = Science & Technology, 22 = People & Blogs.",
            },
            "privacy_status": {
                "type": "string",
                "enum": ["private", "unlisted", "public"],
                "default": "private",
                "description": "Defaults to private. 'public' is an irreversible live publish and needs explicit user approval before this tool is called with it.",
            },
            "made_for_kids": {"type": "boolean", "default": False},
            "publish_at": {
                "type": "string",
                "description": "ISO-8601 UTC timestamp for a scheduled public release (implies privacy_status='private' until then).",
            },
            "thumbnail_path": {"type": "string", "description": "Optional custom thumbnail image (JPEG/PNG)."},
            "playlist_id": {"type": "string", "description": "Optional playlist to add the uploaded video to."},
            "client_secret_file": {"type": "string", "description": "Override YOUTUBE_CLIENT_SECRET_FILE."},
            "token_file": {"type": "string", "description": "Override YOUTUBE_TOKEN_FILE."},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "video_id": {"type": "string"},
            "video_url": {"type": "string"},
            "privacy_status": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=0, network_required=True)
    retry_policy = RetryPolicy(max_retries=2, backoff_seconds=5.0, retryable_errors=["quotaExceeded", "backendError", "internalError"])
    idempotency_key_fields = ["video_path", "title"]
    side_effects = [
        "Uploads a real video to the authenticated YouTube channel",
        "If privacy_status='public', the video becomes publicly visible immediately",
        "Opens a browser window for one-time OAuth consent on first use",
    ]
    user_visible_verification = [
        "Open the returned video_url and confirm title, description, and visibility are correct",
    ]

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    # ---- Auth ----

    def _get_credentials(self, client_secret_file: str, token_file: str):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        token_path = Path(token_file)
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(client_secret_file):
                    raise RuntimeError(
                        f"YOUTUBE_CLIENT_SECRET_FILE not found: {client_secret_file}. "
                        + self.install_instructions
                    )
                flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")

        return creds

    # ---- Execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        video_path = Path(inputs["video_path"]).expanduser()
        if not video_path.is_file():
            return ToolResult(success=False, error=f"video_path not found: {video_path}")

        thumbnail_path = inputs.get("thumbnail_path")
        if thumbnail_path and not Path(thumbnail_path).expanduser().is_file():
            return ToolResult(success=False, error=f"thumbnail_path provided but not found: {thumbnail_path}")

        client_secret_file = inputs.get("client_secret_file") or os.environ.get("YOUTUBE_CLIENT_SECRET_FILE")
        if not client_secret_file:
            return ToolResult(success=False, error="YOUTUBE_CLIENT_SECRET_FILE not set. " + self.install_instructions)
        token_file = inputs.get("token_file") or os.environ.get("YOUTUBE_TOKEN_FILE") or ".youtube-token.json"

        try:
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            return ToolResult(
                success=False,
                error=(
                    "google-api-python-client / google-auth-oauthlib not installed. "
                    "Run: pip install google-api-python-client google-auth-oauthlib"
                ),
            )

        try:
            creds = self._get_credentials(client_secret_file, token_file)
        except Exception as exc:  # noqa: BLE001 - surfaced as an actionable error
            return ToolResult(success=False, error=f"YouTube OAuth failed: {exc}")

        status: dict[str, Any] = {
            "privacyStatus": inputs.get("privacy_status", "private"),
            "selfDeclaredMadeForKids": bool(inputs.get("made_for_kids", False)),
        }
        if inputs.get("publish_at"):
            status["privacyStatus"] = "private"
            status["publishAt"] = inputs["publish_at"]

        body = {
            "snippet": {
                "title": inputs["title"][:100],
                "description": inputs.get("description", ""),
                "tags": inputs.get("tags", []) or [],
                "categoryId": inputs.get("category_id", "27"),
            },
            "status": status,
        }

        try:
            youtube = build("youtube", "v3", credentials=creds)
            media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/*")
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

            response = None
            while response is None:
                _, response = request.next_chunk()

            video_id = response["id"]

            if thumbnail_path:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(str(Path(thumbnail_path).expanduser())),
                ).execute()

            playlist_id = inputs.get("playlist_id")
            if playlist_id:
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id},
                        }
                    },
                ).execute()

        except HttpError as exc:
            return ToolResult(success=False, error=f"YouTube API error ({exc.resp.status}): {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"YouTube upload failed: {exc}")

        video_url = f"https://youtu.be/{video_id}"
        return ToolResult(
            success=True,
            data={
                "video_id": video_id,
                "video_url": video_url,
                "privacy_status": status["privacyStatus"],
                "playlist_id": inputs.get("playlist_id"),
            },
            artifacts=[video_url],
        )
