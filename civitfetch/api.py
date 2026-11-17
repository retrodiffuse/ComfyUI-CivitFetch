"""HTTP routes for CivitFetch, registered on ComfyUI's aiohttp server (PromptServer).

Endpoints (all under /civitfetch):
  GET  /folders    -> {"folders": [...]}  subfolders of the ComfyUI models/ directory
  POST /download   -> Server-Sent Events stream of download progress

Blocking work (network + disk via the `requests`-based downloader) runs in a background
thread; progress items are handed back to the aiohttp event loop through an asyncio.Queue
so the SSE response stays responsive.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import List

from aiohttp import web

from server import PromptServer  # provided by ComfyUI

from . import civitai, downloader

# Depth cap so we don't walk enormous/symlinked model trees forever.
_MAX_DEPTH = 3

routes = PromptServer.instance.routes


def _models_dir() -> str:
    """Absolute path to ComfyUI's models/ directory."""
    import folder_paths  # imported lazily so this file is importable outside ComfyUI

    base = getattr(folder_paths, "models_dir", None)
    if base:
        return base
    return os.path.join(folder_paths.base_path, "models")


def _list_folders() -> List[str]:
    """Relative POSIX paths of every non-hidden subfolder under models/, up to _MAX_DEPTH."""
    root = _models_dir()
    if not os.path.isdir(root):
        return []
    found = set()
    root_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, _ in os.walk(root):
        depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
        # Skip hidden folders and common junk.
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for d in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, d), root)
            found.add(rel.replace(os.sep, "/"))
    return sorted(found)


def _safe_dest(folder: str) -> str:
    """Resolve `folder` under models/ and reject anything that escapes it (path traversal)."""
    root = os.path.realpath(_models_dir())
    # Treat the input as relative regardless of leading slashes/backslashes.
    rel = (folder or "").strip().replace("\\", "/").lstrip("/")
    dest = os.path.realpath(os.path.join(root, rel))
    if dest != root and not dest.startswith(root + os.sep):
        raise ValueError("Target folder is outside the models directory.")
    return dest


@routes.get("/civitfetch/folders")
async def get_folders(request: web.Request) -> web.Response:
    try:
        folders = await asyncio.get_event_loop().run_in_executor(None, _list_folders)
    except Exception as exc:  # never 500 the panel; report as empty + error
        return web.json_response({"folders": [], "error": str(exc)}, status=200)
    return web.json_response({"folders": folders})


def _fmt_bytes(n: int) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(n)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}" if i else f"{int(size)} B"


def _scrub(msg: str) -> str:
    """Mask API keys (?token=...) so they never reach the console or the UI.

    requests puts the full URL, token included, into its exception text.
    """
    return re.sub(r"(token=)[^&\s'\"]+", r"\1***", str(msg))


def _log(msg: str) -> None:
    print(f"[CivitFetch] {_scrub(msg)}", flush=True)


def _bar(pct: float, width: int = 24) -> str:
    filled = int(round(width * max(0.0, min(100.0, pct)) / 100))
    return "#" * filled + "-" * (width - filled)


def _fmt_speed(bytes_per_sec) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "--.- /s"
    return f"{_fmt_bytes(bytes_per_sec)}/s"


def _fmt_eta(seconds) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:  # None / negative / NaN
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# Minimum seconds between terminal redraws, so we refresh live without flooding the console.
_REDRAW_INTERVAL = 0.1


# Control object for the currently-running download, so the /control endpoint can
# pause/stop it. The UI runs one download at a time.
_current_control = None


def _run_download(url, folder, token, queue, loop, control) -> None:
    """Blocking worker (runs in a thread): resolve + download, pushing dicts onto `queue`.

    Renders a live, in-place progress bar to the ComfyUI console (redrawn on one line via
    carriage return) so downloads are visible in the terminal even when no browser tab is
    watching the SSE stream.
    """

    def emit(item: dict) -> None:
        if item and "message" in item:
            item = {**item, "message": _scrub(item["message"])}
        loop.call_soon_threadsafe(queue.put_nowait, item)

    last_draw = 0.0
    bar_open = False  # a \r progress line is currently on-screen without a trailing newline
    speed = None  # smoothed bytes/sec (exponential moving average)
    prev_t = None  # timestamp of the previous speed sample
    prev_b = 0  # downloaded bytes at the previous speed sample

    def draw(line: str) -> None:
        # Pad to clear any leftover characters from a previous, longer line.
        print(f"\r[CivitFetch] {line}".ljust(100), end="", flush=True)

    def close_bar() -> None:
        nonlocal bar_open
        if bar_open:
            print(flush=True)  # finish the in-place line with a newline
            bar_open = False

    try:
        dest = _safe_dest(folder)
        _log(f"Resolving {url}")
        resolved = civitai.resolve(url, token or None)
        for item in downloader.download(resolved.url, dest, resolved.filename, control):
            ev = item.get("event")
            if ev == "start":
                total = item.get("total") or 0
                size = _fmt_bytes(total) if total else "unknown size"
                _log(f"Downloading {item.get('filename')} ({size}) -> {dest}")
                prev_t = time.monotonic()
                prev_b = item.get("downloaded") or 0
            elif ev == "progress":
                now = time.monotonic()
                pct = item.get("pct")
                is_final = pct is not None and pct >= 100
                if now - last_draw >= _REDRAW_INTERVAL or is_final:
                    last_draw = now
                    done_bytes = item["downloaded"]
                    # Smooth the instantaneous rate so the readout doesn't jitter.
                    if prev_t is not None and now > prev_t:
                        inst = (done_bytes - prev_b) / (now - prev_t)
                        speed = inst if speed is None else 0.3 * inst + 0.7 * speed
                    prev_t, prev_b = now, done_bytes
                    if pct is not None:
                        total = item["total"]
                        eta = (total - done_bytes) / speed if speed and total else None
                        draw(
                            f"[{_bar(pct)}] {pct:5.1f}%  "
                            f"{_fmt_bytes(done_bytes)} / {_fmt_bytes(total)}  "
                            f"{_fmt_speed(speed)}  ETA {_fmt_eta(eta)}"
                        )
                    else:  # unknown total size — no percentage / ETA available
                        draw(f"{_fmt_bytes(done_bytes)}  {_fmt_speed(speed)}")
                    bar_open = True
            elif ev == "done":
                close_bar()
                _log(f"✓ Saved {item.get('path')}")
            elif ev == "paused":
                close_bar()
                _log(f"⏸ Paused at {_fmt_bytes(item.get('downloaded') or 0)} (resume to continue)")
            elif ev == "stopped":
                close_bar()
                _log("⏹ Stopped (partial file discarded)")
            elif ev == "error":
                close_bar()
                _log(f"✗ {item.get('message')}")
            emit(item)
    except civitai.CivitaiError as exc:
        close_bar()
        _log(f"✗ {exc}")
        emit({"event": "error", "message": str(exc)})
    except ValueError as exc:  # path traversal / bad folder
        close_bar()
        _log(f"✗ {exc}")
        emit({"event": "error", "message": str(exc)})
    except Exception as exc:  # pragma: no cover - defensive
        close_bar()
        _log(f"✗ Unexpected error: {exc}")
        emit({"event": "error", "message": f"Unexpected error: {exc}"})
    finally:
        close_bar()
        emit(None)  # sentinel: stream complete


@routes.post("/civitfetch/download")
async def post_download(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    url = (body.get("url") or "").strip()
    folder = body.get("folder") or ""
    token = (body.get("token") or "").strip()
    if not url:
        return web.json_response({"error": "No link provided."}, status=400)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    global _current_control
    control = downloader.Control()
    _current_control = control

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    worker = threading.Thread(
        target=_run_download,
        args=(url, folder, token, queue, loop, control),
        daemon=True,
    )
    worker.start()

    try:
        while True:
            item = await queue.get()
            if item is None:  # worker finished
                break
            try:
                await response.write(f"data: {json.dumps(item)}\n\n".encode("utf-8"))
            except (ConnectionResetError, asyncio.CancelledError):
                # Client navigated away — let the worker finish writing the file in the bg.
                break
    finally:
        if _current_control is control:
            _current_control = None

    try:
        await response.write_eof()
    except Exception:
        pass
    return response


@routes.post("/civitfetch/control")
async def post_control(request: web.Request) -> web.Response:
    """Pause or stop the currently-running download."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON body."}, status=400)

    action = (body.get("action") or "").strip().lower()
    control = _current_control
    if control is None:
        return web.json_response({"ok": False, "error": "No active download."}, status=409)
    if action == "pause":
        control.pause()
    elif action == "stop":
        control.stop()
    else:
        return web.json_response({"ok": False, "error": "Unknown action."}, status=400)
    return web.json_response({"ok": True})
