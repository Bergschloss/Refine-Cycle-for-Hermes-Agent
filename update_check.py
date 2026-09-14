"""Tell the user when a newer Refine Cycle release exists. Never install it.

A plugin that downloads and runs its own updates bypasses the review a pinned
catalog entry exists for, and a compromised release would reach every install
on its own. So this only reads the tag of the latest release and reports it.

At most one anonymous request to api.github.com per process per day. Nothing
about the host, its sessions or its config is sent. ``update_check: false``
turns it off.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from . import config
except ImportError:
    import config  # type: ignore

RELEASES_URL = (
    "https://api.github.com/repos/Bergschloss/Refine-Cycle-for-Hermes-Agent/releases/latest"
)
_CHECK_INTERVAL_SECONDS = 24 * 3600
_TIMEOUT_SECONDS = 3.0
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# Process memory only. A file would make /refine status, which is read-only,
# write into the runtime directory. A failed check is remembered too, so an
# offline host is not asked again on every status call.
_memory: Dict[str, Any] = {}
_lock = threading.Lock()


def installed_version() -> str:
    """The version in this install's plugin.yaml, or "" when it cannot be read."""
    try:
        text = (Path(__file__).resolve().parent / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^version:\s*['\"]?([^'\"\s]+)", text, re.MULTILINE)
    return match.group(1) if match else ""


def parse_version(value: Any) -> Optional[Tuple[int, int, int]]:
    match = _VERSION_RE.match(str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _fetch_latest_release() -> Optional[Dict[str, str]]:
    request = urllib.request.Request(
        RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "refine-cycle-update-check",
        },
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    tag = str(data.get("tag_name") or "")
    if data.get("draft") or data.get("prerelease") or parse_version(tag) is None:
        return None
    return {"tag": tag, "url": str(data.get("html_url") or "")}


def latest_release(now: Optional[float] = None) -> Optional[Dict[str, str]]:
    """The latest release, asked for at most once a day. Never raises."""
    if not config.update_check_enabled():
        return None
    now = time.time() if now is None else now
    with _lock:
        checked = _memory.get("checked_ts")
        if isinstance(checked, (int, float)) and now - checked < _CHECK_INTERVAL_SECONDS:
            return _memory.get("release")
        try:
            release = _fetch_latest_release()
        except Exception:
            # Keep the last good answer through an outage rather than forgetting it.
            release = _memory.get("release")
        _memory["checked_ts"] = now
        _memory["release"] = release
        return release


def update_available() -> Optional[Dict[str, str]]:
    """``{installed, latest, url}`` when a newer release exists, otherwise None."""
    try:
        release = latest_release()
        if not release:
            return None
        installed = installed_version()
        latest_parsed = parse_version(release.get("tag"))
        installed_parsed = parse_version(installed)
        if latest_parsed is None or installed_parsed is None:
            return None
        if latest_parsed <= installed_parsed:
            return None
        return {"installed": installed, "latest": release["tag"], "url": release.get("url", "")}
    except Exception:
        return None
