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


def _send(target: str, text: str) -> bool:
    """Import cmd_send lazily and deliver. Runs on a worker thread.

    cmd_send is a CLI entry point: it calls sys.exit() on BOTH success and
    failure. sys.exit(0) raises SystemExit(0), so success cannot be inferred from
    "no exception" -- the exit CODE is the only signal. Return True only on
    SystemExit(0)/None; a non-zero code or a real exception is a failed send.
    """
    from hermes_cli.send_cmd import cmd_send

    try:
        cmd_send(_build_send_namespace(target, text))
    except SystemExit as exc:
        return exc.code in (0, None)
    return True


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
                result["ok"] = _send(target, safe_text)
            except KeyboardInterrupt:
                raise
            except BaseException:  # noqa: BLE001 - a broken send must not escape
                logger.debug("refine notify: send failed", exc_info=True)

        thread = threading.Thread(target=worker, name="refine-notify", daemon=True)
        thread.start()
        thread.join(_SEND_TIMEOUT_SECONDS)
        if thread.is_alive():
            # cmd_send is still blocked (network). Abandon it; refine will not
            # wait on a cosmetic message.
            logger.debug("refine notify: send timed out after %.1fs", _SEND_TIMEOUT_SECONDS)
            return False
        return bool(result["ok"])
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - notify() must never raise
        logger.debug("refine notify: unexpected failure", exc_info=True)
        return False
