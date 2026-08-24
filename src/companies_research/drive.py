"""Google Drive, read-only: list files, download one, render it as Markdown.

Auth follows the rest of this codebase: the operator's own Google account via
the installed-app OAuth flow, consented once in a browser, cached in a token
file. Drive gets its *own* token (``credentials/token-drive.json``) rather than
a scope added to the Gmail token, so turning Drive on never forces the mail
consent to be redone — the same isolation reasoning as the delivery mailbox.

A service-account file (``GOOGLE_SERVICE_ACCOUNT_FILE``) is the headless
alternative for deployments with no browser: share a folder with the account's
address and it sees exactly that folder. Setting the variable is the opt-in,
which is why it wins over OAuth when both exist.

File content goes through MarkItDown, so PDF, DOCX, XLSX, PPTX and the Google
Docs formats (exported to their Office equivalents first) all come back as
Markdown a model can read. What that Markdown *is* — stranger-writable input —
is handled at the tool boundary: ``tools.builtin.read_drive_file`` fences it
with ``render_untrusted`` before any model sees it.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import Any

from .config import SETTINGS

log = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# What a Google-native file becomes on the way down. Everything else downloads
# as itself.
_EXPORT_MAP = {
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}

MAX_CONTENT_CHARS = 15000


class DriveUnavailable(RuntimeError):
    """No way to reach Drive — carries what to set up."""


def _build_service() -> Any:
    from googleapiclient.discovery import build

    sa_file = SETTINGS.google_service_account_file
    if sa_file.exists():
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            str(sa_file), scopes=DRIVE_SCOPES
        )
        log.debug("Drive: using service account %s", sa_file.name)
        return build("drive", "v3", credentials=creds)

    if SETTINGS.google_credentials_file.exists():
        from .google_auth import get_credentials

        creds = get_credentials(
            token_file=SETTINGS.credentials_dir / "token-drive.json",
            scopes=DRIVE_SCOPES,
        )
        log.debug("Drive: using installed-app OAuth")
        return build("drive", "v3", credentials=creds)

    raise DriveUnavailable(
        "no Google credential for Drive — set GOOGLE_SERVICE_ACCOUNT_FILE to a "
        "service-account JSON (and share the folder with its email address), or "
        "put an OAuth client at credentials/client_secret.json"
    )


# Built per call, not cached: SETTINGS is a live view the web UI can change,
# and a cached client would keep answering with the credential it was born
# with. Loading a credential file per listing is cheap next to the API call.
def _get_service() -> Any:
    return _build_service()


def list_files(folder_id: str = "", page_size: int = 50) -> dict:
    """Files visible to the credential, newest first."""
    service = _get_service()

    query_parts = ["trashed = false"]
    folder = folder_id or SETTINGS.drive_folder_id
    if folder:
        query_parts.append(f"'{folder}' in parents")

    results = service.files().list(
        q=" and ".join(query_parts),
        pageSize=max(1, min(page_size, 200)),
        fields="files(id, name, mimeType, size, modifiedTime)",
        orderBy="modifiedTime desc",
    ).execute()

    files = [
        {
            "id": f["id"],
            "name": f["name"],
            "mimeType": f.get("mimeType", ""),
            "size": f.get("size", "unknown"),
            "modifiedTime": f.get("modifiedTime", ""),
        }
        for f in results.get("files", [])
    ]
    return {"total_files": len(files), "files": files}


def _download(file_id: str) -> dict:
    """Fetch one file to a temp path, exporting Google-native formats."""
    from googleapiclient.http import MediaIoBaseDownload

    service = _get_service()
    meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()
    mime_type = meta.get("mimeType", "")
    file_name = meta.get("name", "")

    if mime_type in _EXPORT_MAP:
        export_mime, ext = _EXPORT_MAP[mime_type]
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        request = service.files().get_media(fileId=file_id)
        ext = os.path.splitext(file_name)[1] or ".bin"

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(buffer.getvalue())
        temp_path = tmp.name

    return {"file_name": file_name, "mime_type": mime_type, "temp_path": temp_path}


def read_file_markdown(file_id: str, *, max_chars: int = MAX_CONTENT_CHARS) -> dict:
    """Download a Drive file and return its content as Markdown."""
    from markitdown import MarkItDown

    download = _download(file_id)
    try:
        converted = MarkItDown().convert(download["temp_path"])
        content = converted.text_content or ""
    finally:
        try:
            os.unlink(download["temp_path"])
        except OSError:
            log.warning("Could not remove temp file for %s", file_id)

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"

    return {
        "file_id": file_id,
        "file_name": download["file_name"],
        "mime_type": download["mime_type"],
        "content": content,
        "truncated": truncated,
    }
