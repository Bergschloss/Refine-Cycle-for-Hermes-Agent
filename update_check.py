"""Tell the user when a newer Refine Cycle release exists, and update on request.

Nothing here installs code on its own. ``update_available`` only reads the tag
of the latest release. ``run_update`` installs it, and runs only when the user
sends ``/refine update``: a plugin that pulls and runs its own updates would
bypass the review a pinned catalog entry exists for, and a compromised release
would reach every install without anyone choosing it.

The check is one anonymous request to api.github.com, at most once a day per
process. Nothing about the host, its sessions or its config is sent.
``update_check: false`` turns the check off; the explicit command still works.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from . import config
except ImportError:
    import config  # type: ignore

_REPO = "Bergschloss/Refine-Cycle-for-Hermes-Agent"
_API = f"https://api.github.com/repos/{_REPO}"
RELEASES_URL = f"{_API}/releases/latest"
_TARBALL_URL = f"https://codeload.github.com/{_REPO}/tar.gz/{{sha}}"
_USER_AGENT = "refine-cycle-update-check"

_CHECK_INTERVAL_SECONDS = 24 * 3600
_CHECK_TIMEOUT_SECONDS = 3.0
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_INSTALL_TIMEOUT_SECONDS = 300
# A release archive is about 18 MB, most of it demo animations and evidence.
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_OUTPUT_TAIL_CHARS = 1200

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_MANIFEST_VERSION_RE = re.compile(r"^version:\s*['\"]?([^'\"\s]+)", re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CATALOG_SIDECAR = ".hermes-catalog.json"

# Process memory only. A file would make /refine status, which is read-only,
# write into the runtime directory. A failed check is remembered too, so an
# offline host is not asked again on every call.
_memory: Dict[str, Any] = {}
_lock = threading.Lock()


# -- Versions -------------------------------------------------------------------


def parse_version(value: Any) -> Optional[Tuple[int, int, int]]:
    match = _VERSION_RE.match(str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _manifest_version(directory: Path) -> str:
    try:
        text = (directory / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _MANIFEST_VERSION_RE.search(text)
    return match.group(1) if match else ""


def _plugin_dir() -> Path:
    return Path(__file__).resolve().parent


def installed_version() -> str:
    """The version in this install's plugin.yaml, or "" when it cannot be read."""
    return _manifest_version(_plugin_dir())


# -- The daily check --------------------------------------------------------------


def _get_json(url: str, timeout: float = _CHECK_TIMEOUT_SECONDS) -> Dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _fetch_latest_release() -> Optional[Dict[str, str]]:
    data = _get_json(RELEASES_URL)
    tag = str(data.get("tag_name") or "")
    if data.get("draft") or data.get("prerelease") or parse_version(tag) is None:
        return None
    return {"tag": tag, "url": str(data.get("html_url") or "")}


def latest_release(now: Optional[float] = None, *, fetch: bool = True) -> Optional[Dict[str, str]]:
    """The latest release, asked for at most once a day. Never raises.

    ``fetch=False`` answers from what this process already knows and never
    touches the network, for callers that hold a lock.
    """
    if not config.update_check_enabled():
        return None
    now = time.time() if now is None else now
    with _lock:
        if not fetch:
            return _memory.get("release")
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


def _newer(release: Optional[Dict[str, str]], installed: str) -> bool:
    if not release:
        return False
    latest = parse_version(release.get("tag"))
    current = parse_version(installed)
    return latest is not None and current is not None and latest > current


def update_available(*, fetch: bool = True) -> Optional[Dict[str, str]]:
    """``{installed, latest, url}`` when a newer release exists, otherwise None."""
    try:
        release = latest_release(fetch=fetch)
        installed = installed_version()
        if not _newer(release, installed):
            return None
        return {"installed": installed, "latest": release["tag"], "url": release.get("url", "")}
    except Exception:
        return None


# -- Download ---------------------------------------------------------------------


def _resolve_tag_commit(tag: str) -> str:
    """The full commit a release tag points at, following an annotated tag once."""
    target = _get_json(f"{_API}/git/ref/tags/{tag}").get("object") or {}
    if target.get("type") == "tag":
        target = _get_json(f"{_API}/git/tags/{target.get('sha')}").get("object") or {}
    sha = str(target.get("sha") or "")
    if target.get("type") != "commit" or not _SHA_RE.match(sha):
        raise ValueError(f"release {tag} does not resolve to a commit")
    return sha


def _safe_extract(archive: Path, destination: Path) -> Path:
    """Extract a release archive, refusing any member that could land outside it."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"release archive contains a link or device: {member.name}")
            if member.name.startswith(("/", "\\")) or ".." in Path(member.name).parts:
                raise ValueError(f"release archive member escapes its folder: {member.name}")
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"release archive member escapes its folder: {member.name}")
        extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(root, members=members, **extra)
    folders = [path for path in root.iterdir() if path.is_dir()]
    if len(folders) != 1:
        raise ValueError("release archive does not hold exactly one top-level folder")
    return folders[0]


def _verify_tree(tree: Path, tag: str) -> None:
    if not (tree / "install.py").is_file():
        raise ValueError(f"release {tag} has no install.py")
    if parse_version(_manifest_version(tree)) != parse_version(tag):
        raise ValueError(
            f"release {tag} contains plugin version {_manifest_version(tree) or 'unknown'}"
        )


def _download_release(tag: str, workdir: Path) -> Path:
    """Download the commit a release tag names, not a branch tip, and unpack it."""
    sha = _resolve_tag_commit(tag)
    archive = workdir / "release.tar.gz"
    request = urllib.request.Request(_TARBALL_URL.format(sha=sha), headers={"User-Agent": _USER_AGENT})
    received = 0
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response, \
            open(archive, "wb") as out:
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            received += len(chunk)
            if received > _MAX_ARCHIVE_BYTES:
                raise ValueError("release archive is larger than any release has been")
            out.write(chunk)
    tree = _safe_extract(archive, workdir / "tree")
    _verify_tree(tree, tag)
    return tree


# -- Install ----------------------------------------------------------------------


def _host_checkout() -> Optional[Path]:
    """The Hermes checkout this process runs from."""
    try:
        import hermes_cli  # type: ignore
    except Exception:
        return None
    return Path(hermes_cli.__file__).resolve().parent.parent


def _snapshot(plugin_dir: Path, into: Path) -> None:
    shutil.copytree(plugin_dir, into, ignore=shutil.ignore_patterns("__pycache__"))


def _restore(plugin_dir: Path, snapshot: Path) -> None:
    """Put the plugin directory back exactly as it was before the update."""
    kept = {path.relative_to(snapshot) for path in snapshot.rglob("*")}
    for path in sorted(plugin_dir.rglob("*"), reverse=True):
        relative = path.relative_to(plugin_dir)
        if "__pycache__" in relative.parts or relative in kept:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    shutil.copytree(snapshot, plugin_dir, dirs_exist_ok=True)


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-_OUTPUT_TAIL_CHARS:] if len(text) > _OUTPUT_TAIL_CHARS else text


def _host_patch_note(runner: Callable[..., Any], tree: Path, host: Path) -> str:
    """One sentence when the host route patch is not in place, otherwise ""."""
    try:
        done = runner(
            [sys.executable, str(tree / "install.py"), "--status", "--hermes-src", str(host)],
            capture_output=True, text=True, timeout=60, cwd=str(tree),
        )
        state = next(
            (line for line in (done.stdout or "").splitlines() if line.strip().startswith("State")),
            "",
        )
    except Exception:
        return ""
    if not state or "patched" in state:
        return ""
    return (
        " The host route patch is not in place, so proposals will fail until you run "
        f"install.py --patch-only from the plugin directory ({state.split(':', 1)[-1].strip()})."
    )


def run_update(*, runner: Callable[..., Any] = subprocess.run) -> Dict[str, str]:
    """Install the latest release over this plugin, when the user asks for it.

    Uses the installer shipped inside that release, the same one a manual
    install runs, and puts the previous files back if it fails. The running
    process keeps the old code until Hermes restarts.
    """
    plugin_dir = _plugin_dir()
    if (plugin_dir / _CATALOG_SIDECAR).is_file():
        return {
            "outcome": "catalog_install",
            "message": (
                "This install came from the Hermes plugin catalog, which pins a reviewed "
                "commit. Update it with: hermes plugins update refine-cycle"
            ),
        }

    installed = installed_version()
    try:
        release = _fetch_latest_release()
    except Exception as exc:
        return {"outcome": "check_failed",
                "message": f"Could not reach GitHub to look for a release ({type(exc).__name__})."}
    if not release:
        return {"outcome": "check_failed", "message": "GitHub returned no usable release."}
    with _lock:
        _memory.update({"checked_ts": time.time(), "release": release})
    tag = release["tag"]
    if not _newer(release, installed):
        return {"outcome": "already_latest",
                "message": f"Already on the latest release ({installed or tag})."}

    host = _host_checkout()
    if host is None:
        return {"outcome": "failed",
                "message": "Could not locate the Hermes checkout this plugin runs in."}

    workdir = Path(tempfile.mkdtemp(prefix="refine-update-"))
    try:
        try:
            tree = _download_release(tag, workdir)
        except Exception as exc:
            return {"outcome": "failed", "message": f"Could not download {tag}: {exc}"}

        snapshot = workdir / "previous"
        _snapshot(plugin_dir, snapshot)

        def undo(reason: str) -> Dict[str, str]:
            try:
                _restore(plugin_dir, snapshot)
                kept = f"{installed} is still installed."
            except Exception as exc:
                kept = f"Restoring {installed} also failed ({exc}); reinstall it with install.py."
            return {"outcome": "failed", "message": f"{reason} {kept}"}

        argv = [sys.executable, str(tree / "install.py"), "--plugin-only", "--hermes-src", str(host)]
        try:
            done = runner(argv, capture_output=True, text=True,
                          timeout=_INSTALL_TIMEOUT_SECONDS, cwd=str(tree))
        except Exception as exc:
            return undo(f"The {tag} installer could not run ({type(exc).__name__}).")
        if done.returncode != 0:
            output = _tail((done.stdout or "") + "\n" + (done.stderr or ""))
            return undo(f"The {tag} installer stopped with exit code {done.returncode}:\n{output}\n")

        now_installed = installed_version()
        if parse_version(now_installed) != parse_version(tag):
            return undo(
                f"The {tag} installer finished, but this plugin directory still reports "
                f"{now_installed or 'no version'}."
            )

        return {
            "outcome": "updated",
            "message": (
                f"Updated Refine Cycle {installed} to {tag}. Restart Hermes to load it "
                "(in chat: /restart)." + _host_patch_note(runner, tree, host)
            ),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
