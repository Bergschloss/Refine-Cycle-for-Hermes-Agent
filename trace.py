"""Sanitized trace for plugin LLM invocations.

No API keys, bearer tokens, endpoints, trajectory, or evidence.
Only uses available PluginLlm fields: session_id, provider, model, usage.
Trace emission never touches mutation journal (budget/rollback/ledger).
Standard library only."""

import hashlib
import logging
import logging.handlers
import pathlib
import threading
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Plugin-owned trace log. The host's agent.log handler runs at INFO (the
# config default), so a logger.debug() record is dropped at the handler and
# every trace is silently lost. A dedicated handler on THIS logger — with
# propagate disabled — captures traces in ~/.hermes/logs/refine-trace.log
# without touching the host's log levels, handlers, or agent.log.
_TRACE_LOGGER_NAME = __name__
_trace_handler = None
_trace_handler_lock = threading.Lock()


def _trace_file() -> Optional[pathlib.Path]:
    """Trace log location under the plugin's own hermes_home resolution."""
    try:
        from config import hermes_home
        return hermes_home() / "logs" / "refine-trace.log"
    except Exception:
        return None


def _ensure_trace_handler() -> None:
    """Attach a DEBUG-level rotating file handler to this module's logger.
    Idempotent; never raises (a missing log dir falls back to no handler and
    emit_trace's existing warning path)."""
    global _trace_handler
    with _trace_handler_lock:
        if _trace_handler is not None:
            return
        path = _trace_file()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=512 * 1024, backupCount=2,
                encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            logger.propagate = False  # keep DEBUG out of the host's INFO handler
            _trace_handler = handler
        except Exception:
            # No trace file this process; emit_trace still logs via its own
            # warning path on failure. Never raise from logging setup.
            _trace_handler = None


def _safe_client_hash(provider: Optional[str], model: Optional[str]) -> str:
    """Hash provider/model for tracing only when provided by host response.
    Never fabricate from arbitrary strings."""
    if provider and model:
        raw = f"{provider}:{model}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return "none"


def build_trace(
    *,
    session_id: str,
    source: str,
    operation: str,
    route_state: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    output_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a sanitized trace dict using only verified PluginLlm fields."""
    return {
        "trace_id": str(uuid.uuid4()),
        "session_id": session_id,
        "source": source,
        "operation": operation,
        "route_state": route_state,
        "provider": provider or "none",
        "model": model or "none",
        "output_tokens": output_tokens,
        "thread_id": threading.get_ident(),
        "start_ts": time.monotonic(),
        "end_ts": None,
        "duration_ms": None,
        "result_code": None,
    }


def finalize_trace(trace: Dict[str, Any], *, result_code: str, output_tokens: Optional[int] = None) -> Dict[str, Any]:
    """Finalize trace with terminal event."""
    end = time.monotonic()
    trace["end_ts"] = end
    trace["duration_ms"] = int((end - trace["start_ts"]) * 1000)
    trace["result_code"] = result_code
    trace["output_tokens"] = output_tokens or trace.get("output_tokens")
    return trace


def emit_trace(trace: Dict[str, Any], *, journal_append=None) -> None:
    """Emit trace to the plugin-owned trace log (never the host's agent.log,
    never the mutation journal). Failure is visible via warning log, never
    silently swallowed."""
    try:
        _ensure_trace_handler()
        logger.debug(
            "refine_trace trace_id=%s route_state=%s result=%s session=%s",
            (trace.get("trace_id") or "?")[:8],
            trace.get("route_state"),
            trace.get("result_code"),
            trace.get("session_id", "?")[:8],
        )
        # Trace emissions never write to mutation journal (invariant protection)
        if journal_append is not None:
            logger.warning("Trace emission: journal_append ignored (trace stays out of mutation journal)")
    except Exception as exc:
        logger.warning("Trace emission failure: %s", str(exc)[:200])


def validate_trace_invariants(events: list) -> str:
    """Oracle validation of trace event sequence.
    Returns 'valid' or invariant violation string (no credentials)."""
    if not events:
        return "INVARIANT_VIOLATION: no events"
    seq = [e.get("sequence") for e in events if isinstance(e.get("sequence"), int)]
    if len(seq) != len(set(seq)):
        return "INVARIANT_VIOLATION: duplicate sequence numbers"
    if seq != sorted(seq):
        return "INVARIANT_VIOLATION: sequence not increasing"
    starts = sum(1 for e in events if e.get("event_type") == "invocation_started")
    if starts != 1:
        return f"INVARIANT_VIOLATION: expected 1 start, found {starts}"
    terminals = sum(1 for e in events if e.get("event_type") == "invocation_finished")
    if terminals != 1:
        return f"INVARIANT_VIOLATION: expected 1 finish, found {terminals}"
    for e in events:
        v = e.get("output_tokens")
        if isinstance(v, str) and v.startswith(("sk-", "Bearer ", "token=")):
            return f"INVARIANT_VIOLATION: credential in output_tokens"
    return "valid"