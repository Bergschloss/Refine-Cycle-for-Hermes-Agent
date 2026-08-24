"""Sanitized trace for plugin LLM invocations.

No API keys, bearer tokens, endpoints, trajectory, or evidence.
Only uses available PluginLlm fields: session_id, provider, model, usage.
Trace emission never touches mutation journal (budget/rollback/ledger).
Standard library only."""

import hashlib
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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
    """Emit trace to log only. Never to mutation journal.
    Failure is visible via warning log, never silently swallowed."""
    try:
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