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
import shutil
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    from . import config, journal, update_check
    from . import notify as _notify
    from .sanitization import scrub_text
except ImportError:  # bare-module import
    import config  # type: ignore  # noqa: F811
    import journal  # type: ignore  # noqa: F811
    import update_check  # type: ignore  # noqa: F811
    import notify as _notify  # type: ignore  # noqa: F811
    from sanitization import scrub_text  # type: ignore  # noqa: F811

logger = logging.getLogger(__name__)

BRAND = "♾️ Refine Cycle"
UPDATE_COMMAND = "refine-update"
FIX_COMMAND = "refine-fix"
_STATE_FILE = "notices.json"
_CHECK_INTERVAL_SECONDS = 24 * 3600
# A check that started and never answered (no network, a short-lived CLI process
# killed mid-fetch) holds the slot for an hour, not for the whole day: otherwise
# one dead process costs every process on the host a day of release checks.
_CHECK_RETRY_SECONDS = 3600
# How many attempt stamps to keep. Each names a version, so only the newest can
# still be inside the retry window; the rest are history nobody reads.
_ATTEMPTS_KEPT = 8
_RESTART_DELAY_SECONDS = 3.0
# Every Hermes process on the host writes this one file, so the write is
# serialized by the cross-process lock prompt notes and the model override
# already use -- one lock for threads and processes both, rather than a second
# one here that only threads of this process would respect. Both waits are short
# by design: writing the file takes microseconds, so a longer wait means refine
# itself is mid-mutation, and a message nobody asked for must never be the reason
# anything else waits. ``remember_chat`` runs on the agent's own turn and so
# waits not at all.
_STATE_LOCK_TIMEOUT = 0.5
_TURN_LOCK_TIMEOUT = 0.0


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
    """Write the state file the way every other store in this plugin writes.

    Its own ``mkstemp`` + ``os.replace`` was the only write here without the
    bounded retry the others have: on Windows a concurrent read denies a replace
    for a moment, and its temp files used a prefix the interrupted-artifact
    cleanup does not recognise, so a process killed mid-write left one behind
    forever.
    """
    journal._atomic_write_text(_state_path(), json.dumps(state))


@contextmanager
def _mutation(timeout: float = _STATE_LOCK_TIMEOUT) -> Iterator[Optional[Dict[str, Any]]]:
    """Read, change and write the state file as one transaction, or yield None.

    ``_save`` is atomic on its own, but load-change-save is not, and the writers
    are separate processes: the gateway, a CLI, cron. Without a cross-process
    lock one process's ``chat`` or ``update_notified`` is overwritten by another's
    older snapshot, which either sends the next message to a stale chat or repeats
    a message this module promises to send once.

    Only the read and the write happen in here. A GitHub fetch, the installer
    subprocess and every ``notify`` run outside it, against a snapshot taken
    before them, with the result latched afterwards -- the lock used to be held
    across a 60-second subprocess while the agent's turn waited on it.

    ``None`` means the lock was busy and nothing was written. Callers must treat
    that as "not recorded" -- at worst a message is said again later -- because
    the alternative is making refine wait on a cosmetic notice.
    """
    stack = ExitStack()
    try:
        stack.enter_context(journal.mutation_lock(timeout=timeout))
    except TimeoutError:
        stack.close()
        logger.debug("refine notices: state file busy, nothing recorded")
        yield None
        return
    with stack:
        state = _load()
        yield state
        try:
            _save(state)
        except Exception:
            # Swallowed by every caller, so this is the only place it can be seen,
            # and an unwritable state file makes every message here repeat.
            logger.warning("refine notices: cannot write %s", _STATE_FILE, exc_info=True)
            raise


def remember_chat(chat: Optional[Tuple[str, str, str]]) -> None:
    """Keep the last chat the user talked from, for messages nobody asked for.

    Called from the post-LLM hook, on the agent's own turn: it reads first and
    only takes a lock when the chat actually changed.
    """
    if not chat or not chat[0] or not chat[1]:
        return
    try:
        if _load().get("chat") == list(chat):
            return
        with _mutation(_TURN_LOCK_TIMEOUT) as state:
            if state is not None:
                state["chat"] = list(chat)
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


def _check_due(state: Dict[str, Any], now: float) -> bool:
    """Whether this process should ask GitHub for a release now.

    Two stamps, because a check that answered and a check that only started are
    not the same fact: the first is good for a day, the second holds the slot for
    an hour so a failed or killed process does not silence the host until
    tomorrow.
    """
    checked = state.get("update_checked_ts")
    if isinstance(checked, (int, float)) and now - checked < _CHECK_INTERVAL_SECONDS:
        return False
    attempted = state.get("update_check_attempt_ts")
    if isinstance(attempted, (int, float)) and now - attempted < _CHECK_RETRY_SECONDS:
        return False
    return True


def _claim(event: str, now: float) -> bool:
    """Claim the one attempt at a once-per-event message, or return False.

    Neither obvious order works on its own. Send first and latch what was
    delivered, and the message repeats: ``notify`` returns False both when there
    is nothing to send to (a desktop-only host has no chat on record) and when its
    five-second wait ran out on a slow platform, and two processes starting
    together -- gateway, CLI and cron right after a restart -- both read "not said
    yet" and both send. Latch first and a send that really failed is lost for good.

    So the attempt is what is recorded, before the send and under the lock: the
    process that wins it sends, the others stay quiet, and a send that failed is
    retried on the same clock a failed release check uses. The delivery latch
    (``broken``, ``update_notified``, ``memory_full``) still decides whether the
    event is finished; this only decides who may try, and how often.

    False also when the state file could not be written -- unrecorded attempts
    would be exactly the repetition this prevents.
    """
    try:
        with _mutation() as state:
            if state is None:
                return False
            attempts = state.get("attempts")
            attempts = dict(attempts) if isinstance(attempts, dict) else {}
            when = attempts.get(event)
            if isinstance(when, (int, float)) and now - when < _CHECK_RETRY_SECONDS:
                return False
            attempts[event] = now
            # Bounded: an event name carries a version, so upgrades leave old keys
            # behind. The newest few are all that can still be inside a window.
            state["attempts"] = dict(
                sorted(attempts.items(), key=lambda item: item[1])[-_ATTEMPTS_KEPT:]
            )
            return True
    except Exception:
        logger.debug("refine notices: cannot claim %s", event, exc_info=True)
        return False


def check_update(now: Optional[float] = None) -> None:
    """Once a day across every process: look for a release, and say so once per release."""
    if not config.update_check_enabled():
        return
    now = time.time() if now is None else now
    try:
        state = _load()
        if _check_due(state, now):
            claimed = False
            with _mutation() as fresh:
                # Claimed under the lock so two processes starting together do not
                # both call GitHub; re-checked inside because the first one may
                # have claimed it while this one waited. No claim, no fetch.
                if fresh is not None and _check_due(fresh, now):
                    fresh["update_check_attempt_ts"] = now
                    claimed = True
            if claimed:
                release = None
                answered = True
                try:
                    release = update_check._fetch_latest_release()
                except Exception:
                    # Not silent: the attempt stamp above is what a later process
                    # reads, and it must not look like a check that answered.
                    answered = False
                    logger.debug("refine notices: release lookup failed", exc_info=True)
                if answered:
                    with _mutation() as fresh:
                        if fresh is not None:
                            # An answer of "nothing usable" -- a draft, a
                            # prerelease, a tag this version cannot parse -- is
                            # still an answer, and holds the slot for the day.
                            # Only a check that failed retries within the hour.
                            fresh["update_checked_ts"] = now
                            if release:
                                fresh["latest_tag"] = release["tag"]
            state = _load()
        latest = latest_known(state)
        if latest and state.get("update_notified") != latest and _claim(f"update:{latest}", now):
            if _send(state, update_available_text(latest)):
                with _mutation() as fresh:
                    if fresh is not None:
                        fresh["update_notified"] = latest
    except Exception:
        logger.debug("refine notices: update check failed", exc_info=True)


def startup_check(now: Optional[float] = None) -> None:
    """At process start: say whether the plugin works, once per change.

    Every message is claimed before it is sent and latched only after it was
    delivered. Two processes start together often -- gateway, CLI and cron right
    after ``hermes gateway restart`` -- and a process with no way to reach the
    user must neither repeat the message nor consume the confirmation the gateway
    would have sent.
    """
    now = time.time() if now is None else now
    try:
        state = _load()
        pending = state.get("pending")
        version = update_check.installed_version()
        if plugin_working():
            if isinstance(pending, dict) and pending.get("kind") == "update":
                text = running_text(version)
            elif state.get("broken") or isinstance(pending, dict):
                text = working_again_text()
            else:
                text = ""
            if text and _claim(f"working:{version}", now) and _send(state, text):
                with _mutation() as fresh:
                    if fresh is not None:
                        fresh.pop("broken", None)
                        fresh.pop("pending", None)
        else:
            # Claimed before ``_host_supported``, not after: that call runs the
            # installer as a subprocess with a 60-second timeout, and on a broken
            # host every ``hermes`` invocation used to pay for it even when the
            # message had been delivered days earlier. Keyed on the Hermes version
            # alone -- deciding paused vs stopped is what the subprocess is for.
            host_version = hermes_version()
            if _claim(f"broken:{host_version}", now):
                supported = _host_supported()
                kind = "paused" if supported is False else "stopped"
                key = [kind, host_version]
                text = "" if state.get("broken") == key else (
                    paused_text(host_version) if kind == "paused" else stopped_text()
                )
                if text and _send(state, text):
                    with _mutation() as fresh:
                        if fresh is not None:
                            fresh["broken"] = key
            if isinstance(pending, dict):
                with _mutation() as fresh:
                    if fresh is not None:
                        # A confirmation still pending on a plugin that does not
                        # work is stale: the update landed and did not fix it.
                        fresh.pop("pending", None)
        check_update(now)
    except Exception:
        logger.debug("refine notices: startup check failed", exc_info=True)


def start_background_checks() -> None:
    threading.Thread(target=startup_check, name="refine-notices", daemon=True).start()


def memory_full(used: Optional[int], limit: Optional[int], now: Optional[float] = None) -> None:
    """Once per store state: a lesson could not be saved because memory is full."""
    if used is None or limit is None:
        return
    now = time.time() if now is None else now
    try:
        state = _load()
        key = [int(used), int(limit)]
        if state.get("memory_full") == key:
            return
        if not _claim(f"memory_full:{used}/{limit}", now):
            return
        if _send(state, memory_full_text(int(used), int(limit))):
            with _mutation() as fresh:
                if fresh is not None:
                    fresh["memory_full"] = key
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


def _hermes_argv() -> List[str]:
    """How to run the ``hermes`` CLI on this host, or ``[]``.

    The host's own resolver knows the cases nothing else does -- a frozen build, a
    venv wrapper, a service install -- so it is asked first. It is asked by name at
    call time rather than imported: this release was disabled outright on a live
    host for statically importing one host symbol Hermes had deleted, and a
    private one is the likelier next casualty. The fallbacks keep the restart
    working when it goes, instead of taking the plugin down with it.
    """
    try:
        import importlib
        resolve = getattr(importlib.import_module("gateway.run"), "_resolve_hermes_bin", None)
        if callable(resolve):
            argv = [str(part) for part in (resolve() or [])]
            if argv:
                return argv
    except Exception:
        logger.debug("refine: the host could not resolve the hermes binary", exc_info=True)
    found = shutil.which("hermes")
    if found:
        return [found]
    for name in ("hermes.exe", "hermes"):
        candidate = Path(config.hermes_home()) / "bin" / name
        if candidate.is_file():
            return [str(candidate)]
    return []


def restart_hermes(loop: Any = None) -> bool:
    """Restart Hermes so the new code loads. True when a restart was started.

    Inside the gateway: the gateway's own restart, a few seconds from now so this
    command's reply is delivered first. Elsewhere: ``hermes gateway restart`` when a
    gateway is running; a CLI process itself loads the new code on its next start.
    """
    # The loop check first: without one the runner cannot be used at all, and
    # finding it walks the whole heap. Every desktop and CLI call passes None.
    runner = _gateway_runner() if loop is not None else None
    if runner is not None:
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
        # get_running_pid, not is_gateway_running: Hermes removed the latter on
        # 2026-09-14 and refuses to load a plugin that still imports it.
        from gateway.status import get_running_pid
        if get_running_pid() is None:
            return False
        argv = _hermes_argv()
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
    # The installer's own stdout and stderr are quoted in this message, so it can
    # carry whatever the environment that ran it had in it. Scrubbed here, once,
    # where the message is turned into words: the chat reply and the desktop app's
    # JSON both read it, and the desktop one used to send it raw.
    message = scrub_text(str(result.get("message") or "")).strip()
    if outcome in ("updated", "repaired"):
        new_version = str(result.get("tag") or update_check.installed_version())
        try:
            with _mutation() as state:
                if state is not None:
                    state["pending"] = {"kind": "update" if outcome == "updated" else "fix",
                                        "version": new_version}
        except Exception:
            # The update is already on disk. A state file that could not be
            # written costs the confirmation after the restart, and nothing else;
            # reporting a failure here would be a lie about what happened.
            logger.warning("refine notices: cannot record the pending confirmation",
                           exc_info=True)
        head = (f"{BRAND} updated to {plain_version(new_version)}." if outcome == "updated"
                else f"{BRAND} fixed.")
        # What the installer said about the host route patch belongs in the head:
        # the caller rebuilds the reply from the head when it restarts, so a
        # release that installed while the patch could not be restored said
        # nothing about it and only turned up later as "stopped working".
        host_note = scrub_text(str(result.get("host_note") or "")).strip()
        if host_note:
            head = f"{head} {host_note}"
        return head, head
    if outcome == "already_latest":
        if was_working:
            return f"{BRAND} {plain_version(update_check.installed_version())} is up to date.", ""
        if _host_supported() is False:
            return paused_text(hermes_version()), ""
        return f"{BRAND} could not fix itself. {message}".strip(), ""
    if outcome == "catalog_install":
        return f"{BRAND}: {message}", ""
    return f"{BRAND} update failed. {message}".strip(), ""


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
# Which backend process answered. New code only runs in a new process, so this
# changing is the one honest proof a restart happened; the button waits for it
# instead of announcing the new version while the old code is still answering.
_BACKEND_ID = f"{os.getpid()}-{time.time():.6f}"


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
        "backend": _BACKEND_ID,
        # The desktop notification is said once per event, like the chat one, and
        # "the plugin stopped working" is an event about a Hermes version, not
        # about the plugin's. Keyed on the plugin version alone, a second Hermes
        # update that broke it again said nothing.
        "hermes": hermes_version(),
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
                        # backend is restarted by the button once it sees this. The
                        # sentence about restarting is left to the button as well:
                        # it is the only side that knows whether it can recycle the
                        # backend, and this side must not claim a restart that only
                        # the other side can perform.
                        restart_hermes(None)
                        reply = restart_head
                    result = {"status": "done", "reply": reply, "restart": bool(restart_head)}
                except Exception as exc:
                    logger.exception("refine desktop update failed")
                    result = {"status": "done", "restart": False,
                              "reply": f"{BRAND} update failed. {type(exc).__name__}"}
                with _job_lock:
                    _job.update(result)

            threading.Thread(target=work, name="refine-desktop-update", daemon=True).start()
    return desktop_state()
