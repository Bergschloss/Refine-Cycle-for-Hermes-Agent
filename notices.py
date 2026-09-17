"""The messages a user gets from the plugin itself, and when.

Every message starts with the brand and is sent at most once per event: a new
release, Hermes breaking the plugin (or pausing it), the plugin running again
after an update or a fix, and a lesson lost to a full memory store. What was
already said, and the last chat to say it to, live in one small file under the
plugin's data directory, so every Hermes process on the host (gateway, CLI,
cron) agrees on it.

Users tap a command instead of a button: Hermes gives plugins no buttons, and on
Telegram a message's ``/refine_update`` is one tap that sends the command. The
gateway turns the underscore back into the registered ``refine-update``.

Nothing here may change a refine outcome. Every public function swallows its own
failures, as ``notify`` does.
"""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from . import config, update_check
    from . import notify as _notify
except ImportError:  # bare-module import
    import config  # type: ignore  # noqa: F811
    import update_check  # type: ignore  # noqa: F811
    import notify as _notify  # type: ignore  # noqa: F811

logger = logging.getLogger(__name__)

BRAND = "♾️ Refine Cycle"
UPDATE_COMMAND = "refine-update"
FIX_COMMAND = "refine-fix"
_STATE_FILE = "notices.json"
_CHECK_INTERVAL_SECONDS = 24 * 3600
_RESTART_DELAY_SECONDS = 3.0
_lock = threading.RLock()


# -- State ------------------------------------------------------------------------


def _state_path() -> Path:
    return Path(config.journal_dir()) / _STATE_FILE


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".notices-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def remember_chat(chat: Optional[Tuple[str, str, str]]) -> None:
    """Keep the last chat the user talked from, for messages nobody asked for."""
    if not chat or not chat[0] or not chat[1]:
        return
    try:
        with _lock:
            state = _load()
            if state.get("chat") == list(chat):
                return
            state["chat"] = list(chat)
            _save(state)
    except Exception:
        logger.debug("refine notices: cannot remember chat", exc_info=True)


def _send(state: Dict[str, Any], text: str) -> bool:
    chat = state.get("chat")
    return _notify.notify(text, chat=tuple(chat) if isinstance(chat, list) and len(chat) == 3 else None)


# -- Texts ------------------------------------------------------------------------


def plain_version(value: str) -> str:
    """``v1.3.12`` and ``1.3.12`` read the same to a user; show one form."""
    text = str(value or "").strip()
    return text[1:] if text[:1] in ("v", "V") else text


def tap(command: str, *, messaging: bool = True) -> str:
    """How to write a command so one tap sends it (Telegram) or it can be typed (CLI)."""
    return "/" + (command.replace("-", "_") if messaging else command)


def update_available_text(latest: str) -> str:
    return f"{BRAND} — update available: {plain_version(latest)}.\n{tap(UPDATE_COMMAND)}"


def stopped_text() -> str:
    return f"{BRAND} stopped working after the Hermes update.\n{tap(FIX_COMMAND)}"


def paused_text(hermes_version: str) -> str:
    return (
        f"{BRAND} is paused: Hermes {hermes_version} isn't supported yet. "
        "You'll get a message when it is."
    )


def running_text(version: str) -> str:
    return f"{BRAND} {plain_version(version)} is running."


def working_again_text() -> str:
    return f"{BRAND} is working again."


def memory_full_text(used: int, limit: int) -> str:
    return (
        f"{BRAND}: memory is full ({used}/{limit}). Lesson not saved. "
        "Remove old entries or raise memory_char_limit."
    )


# -- Host facts -------------------------------------------------------------------


def plugin_working() -> bool:
    """Whether the Hermes this process runs carries the route the plugin needs."""
    try:
        from hermes_cli import plugins as host_plugins
        return hasattr(host_plugins, "plugin_invocation_scope")
    except Exception:
        return False


def hermes_version() -> str:
    try:
        from hermes_cli import __version__
        return str(__version__)
    except Exception:
        return "this version"


def _host_supported() -> Optional[bool]:
    """False when no bundled route patch fits this Hermes; None when unknown."""
    host = update_check._host_checkout()
    installer = update_check._plugin_dir() / "install.py"
    if host is None or not installer.is_file():
        return None
    import subprocess
    state = update_check._host_state(subprocess.run, installer, host).get("state")
    if state == "unknown":
        return None
    return state != "incompatible"


def latest_known(state: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The newest release tag this host has seen that is newer than the installed one."""
    state = _load() if state is None else state
    tag = state.get("latest_tag")
    if isinstance(tag, str) and update_check._newer({"tag": tag}, update_check.installed_version()):
        return tag
    return None


# -- Events -----------------------------------------------------------------------


def check_update(now: Optional[float] = None) -> None:
    """Once a day across every process: look for a release, and say so once per release."""
    if not config.update_check_enabled():
        return
    now = time.time() if now is None else now
    try:
        with _lock:
            state = _load()
            checked = state.get("update_checked_ts")
            if not (isinstance(checked, (int, float)) and now - checked < _CHECK_INTERVAL_SECONDS):
                state["update_checked_ts"] = now
                _save(state)
                try:
                    release = update_check._fetch_latest_release()
                except Exception:
                    release = None
                if release:
                    state["latest_tag"] = release["tag"]
                    _save(state)
            latest = latest_known(state)
            if latest and state.get("update_notified") != latest:
                if _send(state, update_available_text(latest)):
                    state["update_notified"] = latest
                    _save(state)
    except Exception:
        logger.debug("refine notices: update check failed", exc_info=True)


def startup_check() -> None:
    """At process start: say whether the plugin works, once per change."""
    try:
        with _lock:
            state = _load()
            pending = state.pop("pending", None)
            version = update_check.installed_version()
            if plugin_working():
                if isinstance(pending, dict) and pending.get("kind") == "update":
                    _send(state, running_text(version))
                elif state.get("broken") or isinstance(pending, dict):
                    _send(state, working_again_text())
                state.pop("broken", None)
                _save(state)
            else:
                if isinstance(pending, dict):
                    _save(state)
                supported = _host_supported()
                kind = "paused" if supported is False else "stopped"
                key = [kind, hermes_version()]
                if state.get("broken") != key:
                    text = paused_text(hermes_version()) if kind == "paused" else stopped_text()
                    if _send(state, text):
                        state["broken"] = key
                        _save(state)
        check_update()
    except Exception:
        logger.debug("refine notices: startup check failed", exc_info=True)


def start_background_checks() -> None:
    threading.Thread(target=startup_check, name="refine-notices", daemon=True).start()


def memory_full(used: Optional[int], limit: Optional[int]) -> None:
    """Once per store state: a lesson could not be saved because memory is full."""
    if used is None or limit is None:
        return
    try:
        with _lock:
            state = _load()
            key = [int(used), int(limit)]
            if state.get("memory_full") == key:
                return
            if _send(state, memory_full_text(int(used), int(limit))):
                state["memory_full"] = key
                _save(state)
    except Exception:
        logger.debug("refine notices: memory-full notice failed", exc_info=True)


# -- The update / fix command ------------------------------------------------------


def _gateway_runner() -> Any:
    """The gateway running in this process, or None (a CLI, cron or TUI process).

    Hermes hands plugins no reference to it, and its own ``/restart`` is a method on
    it. Using that method, rather than a signal of our own, keeps every platform the
    gateway already knows how to restart on: a service manager, a container, a
    detached helper, Windows.
    """
    try:
        import gc
        from gateway.run import GatewayRunner
    except Exception:
        return None
    for obj in gc.get_objects():
        if isinstance(obj, GatewayRunner) and getattr(obj, "_running", False):
            return obj
    return None


def restart_hermes(loop: Any = None) -> bool:
    """Restart Hermes so the new code loads. True when a restart was started.

    Inside the gateway: the gateway's own restart, a few seconds from now so this
    command's reply is delivered first. Elsewhere: ``hermes gateway restart`` when a
    gateway is running; a CLI process itself loads the new code on its next start.
    """
    runner = _gateway_runner()
    if runner is not None and loop is not None:
        try:
            from gateway.restart import is_container_restart_context, is_gateway_supervisor_process
            via_service = bool(is_gateway_supervisor_process() or is_container_restart_context())
        except Exception:
            via_service = False

        def request() -> None:
            try:
                runner.request_restart(detached=not via_service, via_service=via_service)
            except Exception:
                logger.warning("refine: the gateway did not accept a restart", exc_info=True)

        loop.call_soon_threadsafe(loop.call_later, _RESTART_DELAY_SECONDS, request)
        return True
    try:
        from gateway.status import is_gateway_running
        from gateway.run import _resolve_hermes_bin
        if not is_gateway_running():
            return False
        argv = _resolve_hermes_bin()
        if not argv:
            return False
        import subprocess
        subprocess.Popen(
            [*argv, "gateway", "restart"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        return True
    except Exception:
        logger.warning("refine: could not restart the gateway", exc_info=True)
        return False


def run_update_command(chat: Optional[Tuple[str, str, str]] = None) -> Tuple[str, str]:
    """What ``/refine_update`` and ``/refine_fix`` do: update, or repair after a Hermes update.

    Runs on a worker thread (the caller awaits it), so the gateway keeps answering.
    Returns ``(reply, restart)``: when ``restart`` is not empty the caller restarts
    Hermes and finishes the reply with what happened. ``restart`` is the reply's
    head, so the caller can say "Restarting Hermes…" only when it really is.
    """
    remember_chat(chat)
    was_working = plugin_working()
    result = update_check.run_update()
    outcome = result.get("outcome")
    if outcome in ("updated", "repaired"):
        new_version = str(result.get("tag") or update_check.installed_version())
        with _lock:
            state = _load()
            state["pending"] = {"kind": "update" if outcome == "updated" else "fix",
                                "version": new_version}
            _save(state)
        head = (f"{BRAND} updated to {plain_version(new_version)}." if outcome == "updated"
                else f"{BRAND} fixed.")
        return head, head
    if outcome == "already_latest":
        if was_working:
            return f"{BRAND} {plain_version(update_check.installed_version())} is up to date.", ""
        if _host_supported() is False:
            return paused_text(hermes_version()), ""
        return f"{BRAND} could not fix itself. {result.get('message', '')}".strip(), ""
    if outcome == "catalog_install":
        return f"{BRAND}: {result.get('message', '')}", ""
    return f"{BRAND} update failed. {result.get('message', '')}".strip(), ""


def finish_with_restart(head: str, loop: Any = None) -> str:
    """Restart Hermes and say so; without anything to restart, say when it loads."""
    if restart_hermes(loop):
        return f"{head} Restarting Hermes…"
    return f"{head} It loads the next time Hermes starts."


# -- The desktop half --------------------------------------------------------------
#
# The Hermes desktop app runs this plugin in its own backend, not in the gateway,
# and gives a plugin command 30 seconds before it gives up on the answer. A
# download and an install take longer, so its [Update] / [Fix] button starts the
# work here and polls ``desktop_state`` until it is done; the button then restarts
# the desktop backend itself (desktop/plugin.js). Same outcome and wording as the
# one-tap chat command.

_job: Dict[str, Any] = {}
_job_lock = threading.Lock()


def desktop_state() -> Dict[str, Any]:
    """What the desktop status bar shows. May look for a release, once a day."""
    check_update()
    with _job_lock:
        job = dict(_job) or None
    latest = latest_known()
    return {
        "brand": BRAND,
        "version": plain_version(update_check.installed_version()),
        "working": plugin_working(),
        "latest": plain_version(latest) if latest else None,
        "job": job,
    }


def start_desktop_job() -> Dict[str, Any]:
    """Start one update-or-fix in the background; a second press while it runs does nothing."""
    with _job_lock:
        if _job.get("status") == "running":
            pass
        else:
            _job.clear()
            _job.update(status="running", started=time.time())

            def work() -> None:
                try:
                    reply, restart_head = run_update_command(None)
                    if restart_head:
                        # A Telegram gateway on this host restarts too; the desktop
                        # backend is restarted by the button once it sees this.
                        restart_hermes(None)
                        reply = f"{restart_head} Restarting Hermes\u2026"
                    result = {"status": "done", "reply": reply, "restart": bool(restart_head)}
                except Exception as exc:
                    logger.exception("refine desktop update failed")
                    result = {"status": "done", "restart": False,
                              "reply": f"{BRAND} update failed. {type(exc).__name__}"}
                with _job_lock:
                    _job.update(result)

            threading.Thread(target=work, name="refine-desktop-update", daemon=True).start()
    return desktop_state()
