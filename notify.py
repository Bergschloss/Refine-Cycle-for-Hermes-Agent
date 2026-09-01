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

# An undeliverable notification is reported once per process, then dropped to
# debug. A misconfigured target fails on every single applied edit, and a
# WARNING per edit teaches the operator to filter the log instead of reading it.
_SEND_FAILURE_LOGGED = False
_SEND_FAILURE_LOCK = threading.Lock()


def _report_send_failure_once(target: str, detail: object) -> None:
    """Say once, at WARNING, that a notification could not be delivered.

    This failure used to be entirely invisible: notify() returned False and
    logged at debug, and cmd_send's own reason is suppressed because the
    Namespace sets quiet=True. Measured on the reference host, every
    notification had been failing: ``notify_target`` defaults to the bare
    platform name ``telegram``, which Hermes can only route when that platform
    has a home channel configured. None was, so send_message_tool returned "No
    home channel set for telegram" and cmd_send exited 1 -- silently, forever.

    That is the silent-inertness failure mode this plugin already shipped once
    with a hardcoded home path, so the remedy is named in the message rather
    than left for someone to rediscover. Refine outcomes are untouched either
    way; only the message was lost.
    """
    global _SEND_FAILURE_LOGGED
    with _SEND_FAILURE_LOCK:
        if _SEND_FAILURE_LOGGED:
            logger.debug("refine notify: send to %r failed again (%s)", target, detail)
            return
        _SEND_FAILURE_LOGGED = True
    logger.warning(
        "refine notify: could not deliver to %r (%s). A bare platform name only "
        "routes when that platform has a home channel; either set one, or point "
        "plugins.entries.refine.notify_target at an explicit channel such as "
        "'telegram:Name' -- `hermes send --list` shows the available targets. "
        "Applied edits are unaffected; only the notification was lost. Further "
        "failures are logged at debug.",
        target,
        detail,
    )


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


def notify(text: str) -> bool:
    """Send a user-facing message. Returns True on delivery, False otherwise.

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

        target = config.notify_target()
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
                    _report_send_failure_once(target, detail)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:  # noqa: BLE001 - a broken send must not escape
                logger.debug("refine notify: send failed", exc_info=True)
                # A broken coupling is exactly as invisible as a bad exit code,
                # so it gets the same one-shot report. The traceback stays at
                # debug; the WARNING only has to make the silence stop.
                _report_send_failure_once(target, type(exc).__name__)

        thread = threading.Thread(target=worker, name="refine-notify", daemon=True)
        thread.start()
        thread.join(_SEND_TIMEOUT_SECONDS)
        if thread.is_alive():
            # cmd_send is still blocked (network). Abandon it; refine will not
            # wait on a cosmetic message.
            logger.debug("refine notify: send timed out after %.1fs", _SEND_TIMEOUT_SECONDS)
            _report_send_failure_once(target, f"timeout after {_SEND_TIMEOUT_SECONDS:.1f}s")
            return False
        return bool(result["ok"])
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - notify() must never raise
        logger.debug("refine notify: unexpected failure", exc_info=True)
        return False
