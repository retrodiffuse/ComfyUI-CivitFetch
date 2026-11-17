"""Resolve CivitAI / Civitai Red links into a concrete download URL + suggested filename.

Handles the two shapes users typically paste:

  1. Direct download endpoint
     https://civitai.com/api/download/models/<versionId>[?type=&format=&size=]
  2. Model page
     https://civitai.com/models/<modelId>[?modelVersionId=<versionId>]

The host is preserved end-to-end, so a ``civitai.red`` link uses the ``.red`` API and
download host (Civitai Red mirrors the same REST API + URL scheme as civitai.com).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse, urlencode, urlunparse

import requests

# Hosts we recognise. Anything else is rejected up front with a clear message.
_ALLOWED_HOSTS = {"civitai.com", "civitai.red"}

_REQUEST_TIMEOUT = 30  # seconds, for the metadata API lookups (not the download itself)


class CivitaiError(Exception):
    """Raised when a link can't be understood or the CivitAI API lookup fails."""


@dataclass
class ResolvedDownload:
    url: str                       # final URL to stream from (token appended if provided)
    filename: Optional[str] = None  # suggested filename from API metadata, may be None


def _normalise_host(host: str) -> str:
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _api_base(host: str) -> str:
    """Base URL for the v1 REST API on the same host the user pasted."""
    return f"https://{host}/api/v1"


def _append_token(url: str, token: Optional[str]) -> str:
    """Append ?token=... to a URL, replacing any existing token param."""
    if not token:
        return url
    parts = urlparse(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["token"] = [token]
    new_query = urlencode({k: v[-1] for k, v in query.items()})
    return urlunparse(parts._replace(query=new_query))


def _get_json(url: str, token: Optional[str]) -> dict:
    headers = {"User-Agent": "ComfyUI-CivitFetch"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
    except requests.RequestException as exc:  # network/DNS/timeout
        raise CivitaiError(f"Could not reach CivitAI API: {exc}") from exc
    if resp.status_code in (401, 403):
        raise CivitaiError(
            "CivitAI rejected the request (auth). Set a valid API key in "
            "Settings → CivitFetch."
        )
    if resp.status_code == 404:
        raise CivitaiError("CivitAI could not find that model/version (404).")
    if resp.status_code >= 400:
        raise CivitaiError(f"CivitAI API error {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise CivitaiError("CivitAI API returned an unexpected (non-JSON) response.") from exc


def _primary_file_name(version: dict) -> Optional[str]:
    """Pick the model file name from a model-version payload (prefer the primary file)."""
    files = version.get("files") or []
    if not files:
        return None
    for f in files:
        if f.get("primary"):
            return f.get("name")
    return files[0].get("name")


def resolve(url: str, token: Optional[str] = None) -> ResolvedDownload:
    """Turn a user-pasted CivitAI/Red link into a streamable download URL + filename.

    Raises CivitaiError with a user-facing message on any problem.
    """
    url = (url or "").strip()
    if not url:
        raise CivitaiError("No link provided.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    parts = urlparse(url)
    host = _normalise_host(parts.hostname or "")
    if host not in _ALLOWED_HOSTS:
        raise CivitaiError(
            "Link must be a civitai.com or civitai.red URL "
            f"(got host '{parts.hostname}')."
        )
    path = parts.path.rstrip("/")

    # Case 1: already a direct download endpoint — pass through, just add the token.
    # Best-effort: look up the version's filename so the UI + resume have a stable name.
    m = re.match(r"^/api/download/models/(\d+)", path)
    if m:
        filename = None
        try:
            version = _get_json(f"{_api_base(host)}/model-versions/{m.group(1)}", token)
            filename = _primary_file_name(version)
        except CivitaiError:
            pass  # fall back to Content-Disposition at download time
        return ResolvedDownload(url=_append_token(url, token), filename=filename)

    # Case 2: a model page. Figure out the version id, then ask the API for downloadUrl.
    m = re.match(r"^/models/(\d+)", path)
    if m:
        model_id = m.group(1)
        query = parse_qs(parts.query)
        version_id = (query.get("modelVersionId") or [None])[0]

        if version_id is None:
            # No explicit version — fetch the model and take its latest version.
            model = _get_json(f"{_api_base(host)}/models/{model_id}", token)
            versions = model.get("modelVersions") or []
            if not versions:
                raise CivitaiError("That model has no downloadable versions.")
            version = versions[0]  # API returns newest first
        else:
            version = _get_json(
                f"{_api_base(host)}/model-versions/{version_id}", token
            )

        download_url = version.get("downloadUrl")
        if not download_url:
            raise CivitaiError("CivitAI did not return a download URL for that version.")
        return ResolvedDownload(
            url=_append_token(download_url, token),
            filename=_primary_file_name(version),
        )

    raise CivitaiError(
        "Unrecognised CivitAI link. Paste a model page "
        "(…/models/1234?modelVersionId=5678) or a direct "
        "…/api/download/models/5678 link."
    )
