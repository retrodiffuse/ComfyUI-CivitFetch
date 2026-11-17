"""Stream a resolved download to disk, yielding progress as it goes.

``download`` is a generator: each yield is a dict describing progress or a terminal state,
which api.py forwards to the browser as Server-Sent Events. Downloads write to a ``.part``
file first and are atomically renamed on success, so an interrupted download never leaves a
half-written model in place.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Iterator, Optional

import requests

_CHUNK = 1 << 16  # 64 KiB
_DOWNLOAD_TIMEOUT = (30, 300)  # (connect, read) seconds


class Control:
    """Cooperative pause/stop signal shared between an HTTP handler and a download loop.

    The download loop checks these flags once per chunk. ``pause`` ends the current stream
    but keeps the ``.part`` file so it can be resumed later via an HTTP Range request;
    ``stop`` ends the stream and deletes the ``.part`` file.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._pause = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        self._pause.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()


def _filename_from_headers(resp: requests.Response) -> Optional[str]:
    """Extract a filename from a Content-Disposition header, if present."""
    cd = resp.headers.get("Content-Disposition", "")
    if not cd:
        return None
    # RFC 5987 extended form: filename*=UTF-8''name.safetensors
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.IGNORECASE)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1).strip())
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def sanitize_filename(name: str) -> str:
    """Strip path separators and unsafe characters from a filename."""
    name = os.path.basename(name.strip().replace("\\", "/").split("/")[-1])
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name or "download.bin"


def _unique_path(dest_dir: str, filename: str) -> str:
    """Return a path in dest_dir, adding ' (n)' before the extension to avoid clobbering."""
    candidate = os.path.join(dest_dir, filename)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    i = 1
    while True:
        candidate = os.path.join(dest_dir, f"{stem} ({i}){ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _open_stream(url: str, offset: int):
    """GET ``url`` as a stream, optionally resuming from ``offset`` bytes via a Range header.

    Returns (response, resume_from). resume_from is ``offset`` if the server honoured the
    range (HTTP 206), else 0 (server ignored it / fresh download).
    """
    headers = {"User-Agent": "ComfyUI-CivitFetch"}
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
    resp = requests.get(
        url, headers=headers, stream=True, allow_redirects=True, timeout=_DOWNLOAD_TIMEOUT
    )
    resume_from = offset if (offset > 0 and resp.status_code == 206) else 0
    return resp, resume_from


def download(
    url: str,
    dest_dir: str,
    suggested_name: Optional[str] = None,
    control: "Optional[Control]" = None,
) -> Iterator[dict]:
    """Stream ``url`` into ``dest_dir``. Yields progress dicts, then a terminal dict.

    Terminal dict is one of ``{"event": "done"|"paused"|"stopped"|"error", ...}``.

    The ``.part`` file is named deterministically from the resolved filename, so a paused
    download can be resumed on a later call: if a matching ``.part`` already exists we send a
    Range request and append to it (control.pause keeps it, control.stop deletes it).
    """
    os.makedirs(dest_dir, exist_ok=True)

    # If we can predict the filename (CivitAI usually tells us), we can resume an existing
    # .part before even opening the stream.
    resume_from = 0
    part_path = None
    if suggested_name:
        part_path = os.path.join(dest_dir, sanitize_filename(suggested_name) + ".part")
        if os.path.exists(part_path):
            resume_from = os.path.getsize(part_path)

    try:
        resp, resume_from = _open_stream(url, resume_from)
    except requests.RequestException as exc:
        yield {"event": "error", "message": f"Connection failed: {exc}"}
        return

    with resp:
        if resp.status_code in (401, 403):
            yield {
                "event": "error",
                "message": "Download refused (auth). This model likely needs an API "
                "key — set one in Settings → CivitFetch.",
            }
            return
        if resp.status_code >= 400:
            yield {"event": "error", "message": f"Download failed (HTTP {resp.status_code})."}
            return

        # Now we know the real filename. Recompute the .part path from it.
        filename = sanitize_filename(
            _filename_from_headers(resp) or suggested_name or "download.bin"
        )
        part_path = os.path.join(dest_dir, filename + ".part")

        # If we couldn't guess earlier but a .part exists for this name, resume it now by
        # re-requesting with a Range (only when the server supports it).
        if (
            resume_from == 0
            and os.path.exists(part_path)
            and resp.headers.get("Accept-Ranges") == "bytes"
        ):
            resp.close()
            try:
                resp, resume_from = _open_stream(url, os.path.getsize(part_path))
            except requests.RequestException as exc:
                yield {"event": "error", "message": f"Connection failed: {exc}"}
                return

        remaining = int(resp.headers.get("Content-Length") or 0)
        total = resume_from + remaining
        downloaded = resume_from
        mode = "ab" if resume_from else "wb"
        yield {
            "event": "start",
            "filename": filename,
            "total": total,
            "downloaded": downloaded,
        }

        outcome = "completed"
        try:
            with open(part_path, mode) as fh:
                for chunk in resp.iter_content(chunk_size=_CHUNK):
                    if control is not None and control.stopped:
                        outcome = "stopped"
                        break
                    if control is not None and control.paused:
                        outcome = "paused"
                        break
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    pct = round(downloaded / total * 100, 1) if total else None
                    yield {
                        "event": "progress",
                        "downloaded": downloaded,
                        "total": total,
                        "pct": pct,
                    }
        except (requests.RequestException, OSError) as exc:
            try:
                os.remove(part_path)
            except OSError:
                pass
            yield {"event": "error", "message": f"Download interrupted: {exc}"}
            return

        if outcome == "stopped":
            try:
                os.remove(part_path)
            except OSError:
                pass
            yield {"event": "stopped"}
            return
        if outcome == "paused":
            yield {"event": "paused", "downloaded": downloaded, "total": total}
            return

        # Completed — pick a non-clobbering final name and atomically move into place.
        final_path = _unique_path(dest_dir, filename)
        os.replace(part_path, final_path)
        yield {
            "event": "done",
            "filename": os.path.basename(final_path),
            "path": final_path,
            "total": downloaded,
        }
