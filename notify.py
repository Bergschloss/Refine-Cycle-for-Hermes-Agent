"""User-facing notifications.

Hermes exposes no plugin notification API (checked on v2026.8.31: PluginContext has
no send_message/notify; inject_message forges a user turn; platform_actions cannot
send). The only in-process path is hermes_cli.send_cmd.cmd_send, which takes an
argparse.Namespace because it is a CLI entry point.

That is an undocumented coupling and it is expected to break eventually, so every
call is wrapped: a notification failure must never change a refine outcome. Refine
writes into the agent's own future context; a cosmetic message is not allowed to
put that at risk.
"""

import argparse
import logging
import threading
import time
from typing import Optional, Tuple

try:
    from . import config
    from .sanitization import scrub_text
except ImportError:  # bare-module import on the server
    import config  # type: ignore  # noqa: F811
    from sanitization import scrub_text  # type: ignore  # noqa: F811

logger = logging.getLogger(__name__)

# The join timeout: cmd_send talks to a remote messaging platform, so it can
# block on the network. Refine runs this from a hook thread and must not inherit
# that latency, let alone hang on it. On timeout the send is abandoned and the
# thread is left to finish on its own (it is a daemon), and notify() reports
# failure rather than waiting.
_SEND_TIMEOUT_SECONDS = 5.0

# The sentinel target used when there is no active chat and nothing configured:
# sending is impossible, not merely failing. A constant rather than a bare
# literal so the call site and the branch that reads it cannot drift apart.
_NO_TARGET = "(none)"

# Failure reporting is throttled per CAUSE, not once per process.
#
# A single process-wide latch was the first shape of this, and it recreated the
# failure mode the WARNING exists to prevent: the first undeliverable
# notification silenced every LATER one -- including a different target failing
# for a different reason -- for the life of a gateway that runs for weeks. That
# is the silent-forever mode AGENTS.md names as this plugin's default way of
# failing, and this module already shipped one invisible failure of exactly that
# shape.
#
# Keyed by (target, detail) so a new cause always speaks, and re-armed after a
# window so a persistent misconfiguration resurfaces instead of scrolling away on
# day one. Identical failures stay at debug in between, which is what keeps an
# operator reading the log rather than filtering it.
_FAILURE_REPEAT_SECONDS = 3600.0
_FAILURE_KEYS_MAX = 64
_SEND_FAILURE_REPORTED: "dict[tuple, float]" = {}
_SEND_FAILURE_LOCK = threading.Lock()


def _reset_failure_reports() -> None:
    """Clear the throttle state. For tests that assert on WARNING emission."""
    with _SEND_FAILURE_LOCK:
        _SEND_FAILURE_REPORTED.clear()


def _report_send_failure(target: str, detail: object) -> None:
    """Say, at WARNING, that a notification could not be delivered.

    This failure used to be entirely invisible: notify() returned False and
    logged at debug, and cmd_send's own reason is suppressed because the
    Namespace sets quiet=True. Measured on the reference host, every notification
    had been failing -- ``notify_target`` defaulted to the bare platform name
    ``telegram``, which Hermes can only route when that platform has a home
    channel, and none was set.

    The remedy is chosen from the actual cause: telling an operator who
    configured nothing to go check home channels sends them after the wrong
    thing. Refine outcomes are untouched either way; only the message was lost.
    """
    key = (target, str(detail))
    now = time.monotonic()
    with _SEND_FAILURE_LOCK:
        last = _SEND_FAILURE_REPORTED.get(key)
        if last is not None and (now - last) < _FAILURE_REPEAT_SECONDS:
            logger.debug("refine notify: send to %r failed again (%s)", target, detail)
            return
        # Purge expired entries before inserting, and hard-cap the dict so a
        # rotating target cannot grow it without bound. detail is a short string
        # (e.g. "exit 1"), so this stays tiny in practice.
        stale = [k for k, ts in _SEND_FAILURE_REPORTED.items()
                 if (now - ts) >= _FAILURE_REPEAT_SECONDS]
        for k in stale:
            del _SEND_FAILURE_REPORTED[k]
        # The dict guard is not redundant: min() over an empty mapping raises,
        # and `len({}) >= 0` is true for any non-positive cap. This function is
        # the one that has to stay quiet when everything else is broken, so it
        # does not get to raise from its own bookkeeping.
        if _SEND_FAILURE_REPORTED and len(_SEND_FAILURE_REPORTED) >= _FAILURE_KEYS_MAX:
            oldest = min(_SEND_FAILURE_REPORTED, key=lambda k: _SEND_FAILURE_REPORTED[k])
            del _SEND_FAILURE_REPORTED[oldest]
        _SEND_FAILURE_REPORTED[key] = now

    if target == _NO_TARGET:
        remedy = (
            "There is no active chat to reply to and no address configured, so "
            "nothing was sent. Set plugins.entries.refine.notify_target to an "
            "explicit channel (for example 'telegram:123456789'); "
            "`hermes send --list` shows the available targets."
        )
    else:
        remedy = (
            "Delivery to that address failed. Check it is still reachable and "
            "spelled as `hermes send --list` reports it; a bare platform name "
            "only routes when that platform has a home channel configured."
        )
    logger.warning(
        "refine notify: could not deliver to %r (%s). %s Applied edits are "
        "unaffected; only the notification was lost. Identical failures are "
        "logged at debug for the next %.0f minutes.",
        target,
        detail,
        remedy,
        _FAILURE_REPEAT_SECONDS / 60.0,
    )


def target_for_chat(chat: Optional[Tuple[str, str, str]]) -> Optional[str]:
    """Decide where a notification goes. Pure: no host imports, no side effects.

    ``chat`` is the ``(platform, chat_id, thread_id)`` triple captured in a hook
    callback. It is a parameter, not something this module reads, on purpose: the
    active chat lives in a per-asyncio-task ContextVar that a worker thread is
    not guaranteed to inherit, so ``__init__._capture_active_chat`` reads it on
    the turn's own thread and hands the value down. Reading it here would run on
    the worker and address the wrong chat, or none.

    Order:

    1. The active chat. A numeric ``chat_id`` is a valid target on its own
       (``tools.send_message_tool._TELEGRAM_TOPIC_TARGET_RE`` is
       ``^\\s*(-?\\d+)(?::(\\d+))?\\s*$``, verified against real ids), and a
       ``thread_id`` addresses a forum topic, so the message lands in the
       conversation it came from.
    2. An explicitly configured ``notify_target``. Not optional: only 13 of 187
       sessions on the reference host carry a chat id -- the rest are CLI/local
       -- so without this tier the notification is silent for the bulk of that
       operator's work.
    3. ``None`` -- nothing to send to. The caller sends nothing and reports once,
       rather than failing a delivery to a bare platform name that cannot route.
    """
    if chat:
        platform, chat_id, thread_id = chat
        if platform and chat_id:
            target = f"{platform}:{chat_id}"
            return f"{target}:{thread_id}" if thread_id else target
    return config.notify_target_configured()


def _build_send_namespace(target: str, text: str) -> argparse.Namespace:
    """Every attribute hermes_cli.send_cmd.cmd_send reads, set explicitly.

    cmd_send is a CLI entry point, so it expects the full argparse.Namespace the
    ``hermes send`` parser produces. The dest names are taken from the real
    parser (register_send_subparser), not from the flag spellings: the
    ``--list`` flag's dest is ``list_targets``, not ``list``. Getting that wrong
    made cmd_send read a missing attribute path and exit non-zero, so every
    notification silently failed. Every attribute cmd_send reads is set here so no
    lookup inside it falls through to an AttributeError.
    """
    return argparse.Namespace(
        to=target,
        message=text,
        file=None,
        subject=None,
        list_targets=False,
        quiet=True,
        json=False,
    )


def _send(target: str, text: str) -> tuple:
    """Import cmd_send lazily and deliver. Runs on a worker thread.

    cmd_send is a CLI entry point: it calls sys.exit() on BOTH success and
    failure. sys.exit(0) raises SystemExit(0), so success cannot be inferred from
    "no exception" -- the exit CODE is the only signal. Return True only on
    SystemExit(0)/None; a non-zero code or a real exception is a failed send.

    Returns ``(ok, detail)``. The exit code is carried out rather than collapsed
    into the bool so the caller can name it in the failure report; cmd_send's own
    explanation never reaches us, because quiet=True suppresses it and capturing
    it would mean redirecting the process-wide sys.stdout from a worker thread
    while the gateway is writing to it.
    """
    from hermes_cli.send_cmd import cmd_send

    try:
        cmd_send(_build_send_namespace(target, text))
    except SystemExit as exc:
        return exc.code in (0, None), f"exit {exc.code}"
    return True, "exit 0"


def notify(text: str, chat: Optional[Tuple[str, str, str]] = None) -> bool:
    """Send a user-facing message. Returns True on delivery, False otherwise.

    ``chat`` is the active conversation captured in a hook callback; when it is
    present the message goes there, otherwise it falls back to a configured
    ``notify_target``. Defaulting to None keeps every existing caller working.

    Never raises: a notification failure must not touch a refine outcome. The
    coupling to cmd_send is undocumented and expected to break someday, so every
    error short of a process-control signal is swallowed and logged at debug.
    """
    try:
        if not config.notify_enabled():
            # Return before importing anything: a disabled switch must not pay
            # the cost of the fragile CLI import, and the test asserts cmd_send
            # is never reached.
            return False

        target = target_for_chat(chat)
        if not target:
            # No active chat and no configured address. Sending is impossible,
            # not merely failing, so report once and never call cmd_send: a bare
            # platform guess here is the silent-forever bug documented in
            # docs/FINDING-notify-bare-target-undeliverable.md.
            _report_send_failure(
                _NO_TARGET, "no active chat and no notify_target configured"
            )
            return False
        # Invariant 4: everything leaving for the user goes through the single
        # scrubbing choke point, even text the caller already scrubbed.
        safe_text = scrub_text(text)

        result: dict = {"ok": False}

        def worker() -> None:
            # _send translates cmd_send's SystemExit into a bool; it only raises
            # for a genuine control signal or an unexpected error. Do not re-raise
            # SystemExit here -- cmd_send raises SystemExit(0) on SUCCESS, and
            # re-raising it would drop the success on the floor (the original bug).
            try:
                ok, detail = _send(target, safe_text)
                result["ok"] = ok
                if not ok:
                    _report_send_failure(target, detail)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:  # noqa: BLE001 - a broken send must not escape
                logger.debug("refine notify: send failed", exc_info=True)
                # A broken coupling is exactly as invisible as a bad exit code,
                # so it gets the same one-shot report. The traceback stays at
                # debug; the WARNING only has to make the silence stop.
                _report_send_failure(target, type(exc).__name__)

        thread = threading.Thread(target=worker, name="refine-notify", daemon=True)
        thread.start()
        thread.join(_SEND_TIMEOUT_SECONDS)
        if thread.is_alive():
            # cmd_send is still blocked (network). Abandon it; refine will not
            # wait on a cosmetic message.
            logger.debug("refine notify: send timed out after %.1fs", _SEND_TIMEOUT_SECONDS)
            _report_send_failure(target, f"timeout after {_SEND_TIMEOUT_SECONDS:.1f}s")
            return False
        return bool(result["ok"])
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - notify() must never raise
        logger.debug("refine notify: unexpected failure", exc_info=True)
        return False
