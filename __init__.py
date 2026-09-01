"""Refine plugin registration and command handlers."""

import difflib
import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from agent.plugin_llm import PluginLlm

try:
    from . import config, core, journal, ledger
except ImportError:
    import config, core, journal, ledger  # noqa: F811

logger = logging.getLogger(__name__)
_ROLLBACK_COMMAND = re.compile(r"^rollback\s+([0-9a-fA-F]{12})$")


_AUTO_THREAD_GUARD = threading.Lock()
# A deferred session-end must retain the invocation-bound facade AND the active
# chat captured by its host callback. Bare worker threads do not inherit either
# ContextVar, so both travel by value: {session_id: (llm, active_chat)}.
_AUTO_PENDING_SESSION_ENDS: dict = {}
_AUTO_PENDING_LOCK = threading.Lock()
_REGISTERED_CONTEXT: Optional[Any] = None
_BOUND_LLM_UNSET = object()

# Assistant-message count observed when each session last started an attempt.
# One host turn can append several assistant messages, so the trigger compares a
# delta instead of an exact multiple; an exact multiple is silently skipped
# whenever a tool-using turn steps over it.
_AUTO_TURN_MARKS: dict[str, int] = {}
_AUTO_TURN_MARKS_LOCK = threading.Lock()
_AUTO_TURN_MARKS_MAX = 64
_HOST_PATH_LOCK_TIMEOUT = 2.0


def _defer_or_claim_session_end(
    session_id: str,
    llm: Optional[PluginLlm],
    active_chat: Optional[Tuple[str, str, str]] = None,
) -> bool:
    """Atomically claim the worker slot or publish one deferred fallback."""
    with _AUTO_PENDING_LOCK:
        if _AUTO_THREAD_GUARD.acquire(blocking=False):
            return True
        _AUTO_PENDING_SESSION_ENDS[session_id] = (llm, active_chat)
        return False


def _claim_auto_worker() -> bool:
    """Claim the worker slot under the same lock used by the pending queue."""
    with _AUTO_PENDING_LOCK:
        return _AUTO_THREAD_GUARD.acquire(blocking=False)


def _finish_auto_worker() -> None:
    """Atomically hand the worker slot to pending session-end work or release it."""
    while True:
        with _AUTO_PENDING_LOCK:
            pending = next(iter(_AUTO_PENDING_SESSION_ENDS.items()), None)
            if pending is None:
                _AUTO_THREAD_GUARD.release()
                return
            session_id, (llm, pending_chat) = pending
            del _AUTO_PENDING_SESSION_ENDS[session_id]
        if not config.auto_enabled():
            # Auto is disabled — clean up prompt notes and continue draining
            # while retaining the claimed worker slot.
            _clear_session_prompt_notes(session_id, timeout=_HOST_PATH_LOCK_TIMEOUT)
            continue
        try:
            _on_session_end(
                session_id=session_id,
                _bound_llm=llm,
                _worker_claimed=True,
                _active_chat=pending_chat,
            )
        except Exception:
            logger.exception("deferred refine session-end hook failed")
            _clear_session_prompt_notes(session_id, timeout=_HOST_PATH_LOCK_TIMEOUT)
            continue
        return


def _session_llm() -> Optional[PluginLlm]:
    """Resolve the LLM facade for the current host invocation only.

    ``PluginContext.llm`` is route-bound by Hermes through a ContextVar. It must
    therefore be read while the command or tool handler is executing; retaining
    the registration-time facade or constructing a fallback client would send
    private trajectory evidence through a different route.
    """
    if _REGISTERED_CONTEXT is None:
        return None
    try:
        llm = _REGISTERED_CONTEXT.llm
    except Exception as exc:
        logger.warning("Cannot resolve the active refine LLM: %s", core.scrub_text(str(exc)))
        return None
    return llm if getattr(llm, "invocation_bound", False) else None


def _assistant_turn_count(conversation_history: Any) -> int:
    """Count assistant messages in host callback history without assuming its shape.

    One host turn can contribute several assistant messages, so this is a
    monotonic progress measure, not a count of user-visible turns.
    """
    if not isinstance(conversation_history, (list, tuple)):
        return 0
    count = 0
    for message in conversation_history:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role == "assistant":
            count += 1
    return count


_ACTIVE_CHAT_UNSET = object()


def _capture_active_chat() -> Optional[Tuple[str, str, str]]:
    """Read the active conversation. MUST be called from a hook callback.

    Returns ``(platform, chat_id, thread_id)`` for a live chat turn, or None when
    the turn is not a messaging surface (CLI, local, tui, tool, ...) or the host
    does not expose session context at all.

    **Why the call site matters.** These values live in a ContextVar the gateway
    sets per asyncio task. A raw ``threading.Thread`` inheriting that context is
    an accident of where it was started, and the host confirmed there is no
    contract behind it. Refine notifies from worker threads, so the capture
    happens here, on the turn's own thread, and the triple is passed down by
    value — exactly as the LLM facade already is. Never call this from a worker:
    it would read an empty context and address the wrong chat, or none.

    Any failure means "no chat": this reaches into host internals
    (``gateway.session_context``) a future version may move, and a notification
    address is never worth raising over.
    """
    try:
        from gateway.session_context import (  # type: ignore
            get_session_env,
            session_is_messaging_surface,
        )

        if not session_is_messaging_surface():
            return None
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            return None
        return (platform, chat_id, get_session_env("HERMES_SESSION_THREAD_ID", ""))
    except Exception:
        logger.debug("refine: active chat unavailable", exc_info=True)
        return None


def _cooldown_elapsed() -> bool:
    # One owner for the arithmetic, so the gate and /refine status cannot drift.
    return core.auto_cooldown_remaining_minutes() <= 0


def _turn_interval_reached(session_id: str, assistant_turns: int) -> bool:
    """Compare assistant messages added since this session's last attempt."""
    interval = config.auto_turn_interval()
    if interval <= 0:
        return False
    with _AUTO_TURN_MARKS_LOCK:
        mark = _AUTO_TURN_MARKS.get(session_id)
        if mark is None:
            # A missing mark means "unknown", never "zero turns so far". The mark
            # table is an LRU capped at _AUTO_TURN_MARKS_MAX, so a busy host
            # evicts sessions that are still live; defaulting to 0 made the very
            # next turn of an evicted session look like a full interval of
            # unrefined progress and fired a pass immediately, on every
            # eviction. Baseline the session here and let the interval elapse
            # from this point instead. Being at most one interval late costs
            # nothing; a spurious pass spends the daily edit budget.
            _set_turn_mark_locked(session_id, assistant_turns)
            return False
        return assistant_turns - mark >= interval


def _set_turn_mark_locked(session_id: str, assistant_turns: int) -> None:
    """Write one mark with _AUTO_TURN_MARKS_LOCK already held.

    Split out so the baseline write above shares the LRU bookkeeping instead of
    reaching into the dict directly. ``_AUTO_TURN_MARKS_LOCK`` is a plain
    non-reentrant ``threading.Lock``, so callers must hold it and must not call
    ``_mark_turn_attempt``, which acquires it.
    """
    if session_id in _AUTO_TURN_MARKS:
        # Re-insert at end so insertion order tracks recency (LRU).
        del _AUTO_TURN_MARKS[session_id]
    elif len(_AUTO_TURN_MARKS) >= _AUTO_TURN_MARKS_MAX:
        _AUTO_TURN_MARKS.pop(next(iter(_AUTO_TURN_MARKS)), None)
    _AUTO_TURN_MARKS[session_id] = assistant_turns


def _mark_turn_attempt(session_id: str, assistant_turns: int) -> None:
    """Record the attempt point, keeping the per-session marks bounded."""
    with _AUTO_TURN_MARKS_LOCK:
        _set_turn_mark_locked(session_id, assistant_turns)


def _forget_turn_marks(session_id: str) -> None:
    with _AUTO_TURN_MARKS_LOCK:
        _AUTO_TURN_MARKS.pop(session_id, None)


def _auto_refine_allowed() -> bool:
    """Return whether an automatic attempt may start without mutating state."""
    return config.auto_enabled() and _cooldown_elapsed()


def _run_auto_refine(
    session_id: str,
    llm: Optional[PluginLlm] = None,
    *,
    cleanup_session_notes: bool = False,
    active_chat: Optional[Tuple[str, str, str]] = None,
) -> None:
    """Run one guarded automatic pass with the callback-captured LLM facade.

    ``active_chat`` travels by value for the same reason ``llm`` does: this runs
    on a worker thread that cannot be trusted to have inherited the turn context.
    """
    try:
        if not _auto_refine_allowed():
            if cleanup_session_notes:
                _clear_session_prompt_notes(session_id)
            return
        with journal.try_mutation_lock() as acquired:
            try:
                if not acquired:
                    message = "Automatic refine skipped because the mutation lock is busy"
                    logger.warning(message)
                    core.note_auto_event("mutation_lock_busy", message)
                elif _cooldown_elapsed():
                    core.refine_run(
                        llm=llm,
                        session_id=session_id,
                        auto=True,
                        # The same worker clears this session's notes below, so a
                        # session-scoped note written here would not survive the call.
                        session_ending=cleanup_session_notes,
                        active_chat=active_chat,
                    )
            finally:
                # Cleanup must always run, even if refine_run raised above.
                # Kept inside the ``with`` block deliberately: the mutation lock
                # is reentrant per thread, so when ``acquired`` is True this is a
                # free nested acquisition, not a second wait. When ``acquired``
                # is False, timeout=0.0 makes the attempt non-blocking instead of
                # queuing behind whoever holds it.
                if cleanup_session_notes:
                    _clear_session_prompt_notes(
                        session_id, timeout=None if acquired else 0.0
                    )
    except Exception as exc:
        safe_error = core.scrub_text(str(exc))
        message = f"Automatic refine failed: {safe_error or 'unknown error'}"
        logger.error("%s", message)
        core.note_auto_event("auto_refine_failed", message)
    finally:
        _finish_auto_worker()


def _start_auto_refine(
    session_id: str,
    assistant_turns: int,
    llm: Optional[PluginLlm],
    active_chat: Optional[Tuple[str, str, str]] = None,
) -> None:
    """Start one pass with the bound facade captured in the host callback."""
    if (
        not _auto_refine_allowed()
        or not _turn_interval_reached(session_id, assistant_turns)
        or not _claim_auto_worker()
    ):
        return
    # Charge the attempt to this turn point before the worker starts, so a
    # skipped or failed attempt cannot retry on every following turn.
    _mark_turn_attempt(session_id, assistant_turns)
    try:
        threading.Thread(
            target=_run_auto_refine,
            args=(session_id, llm),
            # kwargs, not args: a positional here would land on
            # cleanup_session_notes and both break notes and mis-route the chat.
            kwargs={"active_chat": active_chat},
            daemon=True,
            name="refine-auto",
        ).start()
    except Exception:
        _finish_auto_worker()
        logger.exception("refine auto thread could not start")


_BLOCK_RULES: list = []  # list of dicts: {type, target, action, ...}

# Child sessions of subagents this plugin launched as proposers. Populated by
# the subagent_start hook (matched by subagent id) and drained on
# subagent_stop; the pre_tool_call hook refuses skill_manage for these.
_PROPOSER_CHILD_SESSIONS: set = set()

def _update_block_rules(notes):
    """Parse prompt-notes into structured block rules.

    A rule inherits its note's ``scope``/``session_id`` unchanged. Without
    this, a note scoped to one session (``scope == "session"``) built a rule
    that _on_pre_tool_call enforced against every session on the host: advice
    given for one task actively blocked an unrelated tool call in someone
    else's conversation. The prompt-injection path a few lines below this one
    (``_on_pre_llm_call``) already filters by scope before injecting text;
    the block path must apply the identical filter before enforcing.
    """
    global _BLOCK_RULES
    rules = []
    for note in (notes or []):
        content = note.get("content", "")
        rule = _parse_prompt_note_rule(content)
        if rule:
            rule["scope"] = note.get("scope", "global")
            rule["session_id"] = note.get("session_id", "")
            rules.append(rule)
    _BLOCK_RULES = rules


def _parse_prompt_note_rule(content):
    """Extract a structured block rule from a prompt-note string."""
    parts = content.split(", ", 1)
    if len(parts) < 2:
        return None
    action_text = parts[1].rstrip(".")
    import re

    # --- Reroute directive: "use X instead of Y" / "prefer X over Y" ---
    m = re.search(
        r"\b(?:use|try|run|execute|call)\s+(.+?)\s+"
        r"(?:instead\s+of|rather\s+than|over)\s+(.+?)\s*$",
        action_text, re.I
    )
    if m:
        alternative = m.group(1).strip()
        raw_target = m.group(2).strip()
        target = raw_target
        # Normalize target: strip "the", trailing dots, parentheticals
        target = re.sub(r"\s*\(.*?\)", "", target).strip(" .")
        target = re.sub(r"^(?:the|a|an)\s+", "", target, flags=re.I)
        target = re.sub(
            r"\s+(?:cli|command|tool|binary|utility|mcp|api|sdk)$", "",
            target, flags=re.I,
        ).lower()
        # If the condition or raw target mentions tool/MCP/API, force block_tool.
        is_tool_context = bool(re.search(
            r"\b(?:tool|mcp|api|sdk)\b",
            raw_target + " " + parts[0], re.I,
        ))
        rule_type = "block_tool" if is_tool_context else (
            "block_binary" if _looks_like_cli(target) else "block_tool"
        )
        return {
            "type": rule_type,
            "target": target,
            "action": action_text,
        }

    # --- "use X instead" (no "of"): implied target would have to come from
    # the CONDITION prose. A condition describes when advice applies, not what
    # to block — synthesizing a target from its nouns once produced
    # block_binary target='code' from 'exit code 127', which blocks the real
    # `code` CLI. Notes without an explicit reroute target are advice only.
    m = re.search(r"\buse\s+(.+?)\s+instead\s*\.?\s*$", action_text, re.I)
    if m:
        return None

    # --- Param rule: "always include both 'A' and 'B' fields" ---
    m = re.search(
        r"(?:always\s+)?include\s+(?:both\s+)?['\u2018]([^'\u2018\u2019]+)['\u2019]"
        r"\s+and\s+['\u2018]([^'\u2018\u2019]+)['\u2019]\s+fields?",
        action_text, re.I
    )
    if m:
        cond = parts[0].lower()
        if cond.startswith("when "):
            cond = cond[5:]
        # Extract tool name from condition
        tool = cond.split("calling ")[-1].split(",")[0].strip()
        return {
            "type": "require_fields",
            "tool": tool,
            "fields": [m.group(1), m.group(2)],
            "action": action_text,
        }

    # --- Fallback: no structured rule. The old fallback treated condition
    # nouns as block targets (block_binary target='identical'/'characters'/
    # 'code' from prose) — a false block stops the agent, so a note that does
    # not name its target explicitly must stay advice, never a rule.
    return None


def _looks_like_cli(word):
    """Whether a word looks like a CLI command name.

    Dots are allowed: real binaries carry version and extension dots
    (``python3.11``, ``node.js``). Without them such a name was misclassified as
    a tool rather than a binary, so the load-bearing protection (B1), which keys
    on binaries reached through ``terminal``, never saw it.
    """
    return bool(re.match(r"^[a-z][a-z0-9_.-]*$", word)) and len(word) <= 20


def _looks_like_tool(word):
    """Whether a word looks like a tool/function name."""
    return bool(re.match(r"^[a-z_][a-z0-9_]+$", word))


def _tool_matches(tool_name: str, target: str) -> bool:
    """Match a tool name against a target, handling mcp__ and namespace prefixes."""
    if not tool_name or not target:
        return False
    tn, tgt = tool_name.lower(), target.lower()
    if tn == tgt or tgt in tn:
        return True
    # Handle mcp__server__tool and namespace:tool patterns
    return tn.endswith("__" + tgt) or tn.endswith(":" + tgt) or tn.endswith("." + tgt)


def _binary_matches(binary: str, target: str) -> bool:
    """Check if binary matches target; cmake won't match make."""
    if binary == target:
        return True
    if target in binary:
        prefix = binary[:binary.index(target)]
        suffix = binary[binary.index(target) + len(target):]
        if prefix and (prefix[-1].isalpha() or prefix[-1] == "_"):
            return False
        if suffix and (suffix[0].isalpha() or suffix[0] == "_"):
            return False
        return True
    return False


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Block tool calls that match a persisted prompt-note rule.

    Also enforces the proposer subagent's read-only contract: a child the
    plugin launched as a proposer may read skills but never write them.
    """
    # This hook runs inside the host's tool dispatch. An exception here — a
    # malformed rule, a helper raising, an unreadable config — would propagate
    # into that dispatch and could block the agent's tool calls entirely. A
    # broken hook must fail OPEN: log and return None so the agent keeps working
    # rather than losing tool access to a refine bug. The block decisions on the
    # happy path are unchanged.
    global _BLOCK_RULES
    try:
        import re, shlex

        session_id = str(kwargs.get("session_id", "") or "")
        if session_id and session_id in _PROPOSER_CHILD_SESSIONS:
            if tool_name == "skill_manage":
                return {
                    "action": "block",
                    "message": (
                        "You are the refine proposer and have no write access. "
                        "Use skills_list and skill_view to verify, then return "
                        "your JSON proposal."
                    ),
                }

        # The block rules come only from prompt notes. When the operator turns
        # the feature off, _BLOCK_RULES can still hold rules parsed on an earlier
        # turn (the list lives for the process), so tool calls would keep being
        # refused by a feature that is disabled. Clear the stale rules and stop
        # enforcing them. The proposer read-only contract above is a separate
        # concern and is intentionally left in force.
        if not config.prompt_notes_enabled():
            _BLOCK_RULES = []
            return None

        # Same normalization _on_pre_llm_call uses before comparing against a
        # note's stored session_id, so a rule built from a session-scoped note
        # only ever fires inside that same session.
        current_session = journal.normalize_prompt_note_session_id(session_id)

        for rule in _BLOCK_RULES:
            if rule.get("scope") == "session" and rule.get("session_id", "") != current_session:
                continue
            rt = rule.get("type", "")
            target = rule.get("target", "")

            # --- Block a specific CLI binary ---
            if rt == "block_binary" and tool_name == "terminal":
                cmd = str(args.get("command", "")) if isinstance(args, dict) else ""
                # check all binaries in the pipeline/chain
                for binary in _extract_binaries(cmd):
                    if _binary_matches(binary, target):
                        return {"action": "block", "message": rule["action"]}

            # --- Block a specific tool name ---
            if rt == "block_tool":
                if _tool_matches(tool_name, target):
                    return {"action": "block", "message": rule["action"]}

            # --- Require specific fields ---
            if rt == "require_fields":
                if _tool_matches(tool_name, rule.get("tool", "")):
                    if isinstance(args, dict):
                        missing = [f for f in rule.get("fields", []) if f not in args]
                        if missing:
                            return {
                                "action": "block",
                                "message": f"Missing required fields: {', '.join(missing)}. "
                                           f"{rule['action']}",
                            }

        return None
    except Exception:
        logger.debug("refine _on_pre_tool_call failed; failing open", exc_info=True)
        return None


def _extract_binaries(cmd: str) -> list:
    """Extract all executable names from a pipeline/chain command."""
    import re, shlex
    binaries = []
    for segment in re.split(r"[;&|\n]{1,2}", cmd):
        segment = segment.strip()
        if not segment or segment.startswith("$"):
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        idx = 0
        # Skip leading VAR=value assignments rather than discarding the whole
        # segment on the first one. `FOO=1 git ...` is `git` run with FOO set,
        # not a segment with no binary -- treating the assignment as the whole
        # command (the old `"=" in toks[0]: continue`) let `FOO=1 git status`
        # and `PYTHONPATH=. pytest` run past a block rule untouched (audit 04-02).
        # Match a real assignment head, `NAME=...`, so an operand that merely
        # contains `=` (a flag value) does not skip a token.
        while idx < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[idx]):
            idx += 1
        while idx < len(tokens) and tokens[idx].lower() in ("sudo", "env", "nohup", "time"):
            idx += 1
        if idx >= len(tokens):
            continue
        binary = tokens[idx].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if binary.lower().endswith(".exe"):
            binary = binary[:-4]
        binaries.append(binary.lower())
    return binaries




def _on_pre_llm_call(**kwargs) -> Optional[dict]:
    """Inject bounded plugin-owned notes without reading or changing the base prompt."""
    try:
        # Record the session id before anything else — this must not be lost
        # because a later error in prompt-note loading skips the rest.
        core.note_session_id(kwargs.get("session_id", ""))
        if not config.prompt_notes_enabled():
            return None
        session_id = journal.normalize_prompt_note_session_id(kwargs.get("session_id", ""))
        # With no store there is nothing to inject, and taking the lock would
        # create journal_dir on a path that may simply be mistyped. This hook
        # runs every turn, so it must stay strictly read-only.
        if not journal.prompt_notes_read_path().is_file():
            return None
        # Prefer reading under the lock, but never drop notes for a whole turn
        # just because a refine pass owns it: the store is only ever replaced
        # atomically, so a lock-free read still sees one complete generation.
        with journal.try_mutation_lock():
            notes = journal.load_prompt_notes()
        if not notes:
            _update_block_rules([])
            return None
        _update_block_rules(notes)
        selected = []
        for note in notes:
            scope = note.get("scope", "global")
            if scope == "session" and note.get("session_id") != session_id:
                continue
            content = core.scrub_text(note["content"]).strip()
            content_error = core._stored_prompt_note_content_error(content)
            if content_error:
                core.note_auto_event(
                    "prompt_note_not_injected",
                    f"Prompt note {note['id']} was not injected: {content_error}",
                )
                continue
            selected.append({"id": note["id"], "content": content})
        if not selected:
            return None
        selected = selected[-config.prompt_notes_max_count():]
        while selected:
            rendered = "Refine notes:\n" + "\n".join(
                "- " + note["content"].replace("\n", "\n  ")
                for note in selected
            )
            safe_rendered = core.scrub_text(rendered)
            if len(safe_rendered) <= config.prompt_notes_max_chars():
                return {"context": safe_rendered}
            selected = selected[1:]
    except Exception:
        logger.warning("refine prompt-note hook failed", exc_info=True)
    return None


def _clear_session_prompt_notes(
    session_id: str, *, timeout: Optional[float] = None
) -> bool:
    """Clear session notes without stalling, and expose failed cleanup."""
    if not isinstance(session_id, str) or not session_id.strip():
        return True
    try:
        # Mirroring is delegated so it happens under the cleanup's own lock.
        # ``ledger.record_edit`` waits up to its own 30s default for the mutation
        # lock, which on this callback thread would defeat ``timeout`` entirely.
        if timeout is None:
            cleanup = journal.clear_session_prompt_notes(
                session_id, mirror=ledger.record_journal_state
            )
        else:
            cleanup = journal.clear_session_prompt_notes(
                session_id, timeout=timeout, mirror=ledger.record_journal_state
            )
        if cleanup is None:
            message = "Session prompt-note cleanup did not complete"
            logger.warning(message)
            core.note_auto_event("prompt_note_cleanup_failed", message)
            return False
        conflicts = cleanup.get("conflicts", [])
        cleanup_error = core.scrub_text(str(cleanup.get("error", "")))
        if conflicts or cleanup_error or cleanup.get("complete") is False:
            # Name the exact notes. Refine will not remove a note it cannot prove
            # it owns, so this state does not clear itself; without the ids the
            # operator has nothing to inspect in the note store. A failure and a
            # retained note happen together, so neither may hide the other.
            retained = ""
            if conflicts:
                named = ", ".join(
                    core.scrub_text(str(note_id)) for note_id in conflicts[:5]
                )
                if len(conflicts) > 5:
                    named += f" (+{len(conflicts) - 5} more)"
                retained = (
                    f"retained {len(conflicts)} note(s) with ownership conflicts"
                    f": {named}"
                )
            if cleanup_error and retained:
                message = (
                    "Session prompt-note cleanup did not complete: "
                    f"{cleanup_error}; {retained}"
                )
            elif cleanup_error:
                message = f"Session prompt-note cleanup did not complete: {cleanup_error}"
            elif retained:
                message = f"Session prompt-note cleanup {retained}"
            else:
                message = "Session prompt-note cleanup did not complete"
            logger.warning(message)
            core.note_auto_event("prompt_note_cleanup_failed", message)
            return False
        return True
    except Exception as exc:
        safe_error = core.scrub_text(str(exc))
        message = f"Session prompt-note cleanup failed: {safe_error}"
        logger.warning(message)
        core.note_auto_event("prompt_note_cleanup_failed", message)
        return False


def _on_session_reset(session_id: str = "", **kwargs) -> None:
    """Expire only notes owned by the session Hermes reset."""
    _forget_turn_marks(session_id)
    _clear_session_prompt_notes(session_id, timeout=_HOST_PATH_LOCK_TIMEOUT)


def _on_post_llm_call(
    session_id: str = "", conversation_history: Any = None, **kwargs
) -> None:
    """Record the session and schedule auto-refine without breaking the host hook."""
    try:
        core.note_session_id(session_id)
        # Resolve while Hermes still has this callback's invocation ContextVar.
        # The worker is a bare thread and cannot resolve the same route later.
        llm = _session_llm()
        # Same reason, same moment: the active chat lives in a per-task
        # ContextVar the worker may not inherit. Capture here, pass by value.
        _start_auto_refine(
            session_id,
            _assistant_turn_count(conversation_history),
            llm,
            _capture_active_chat(),
        )
    except Exception as exc:
        safe_error = core.scrub_text(str(exc))
        message = f"Post-LLM refine hook failed: {safe_error or 'unknown error'}"
        logger.warning("%s", message)
        core.note_auto_event("post_llm_hook_failed", message)


_MODEL_SUBCOMMAND = "model"
_SESSION_SUBCOMMAND = "session"

# Every subcommand _handle_refine_command actually implements. Kept beside the
# two names above so the list cannot drift from the branches that consume them.
_KNOWN_SUBCOMMANDS = ("audit", "dry-run", _MODEL_SUBCOMMAND, "rollback",
                      _SESSION_SUBCOMMAND, "status")


def _explicit_session_status(value: Any) -> tuple[str, str]:
    """Validate one historical-session selector before any model call.

    Returns ``(normalized_id, lookup_status)``. ``lookup_status`` is
    ``invalid`` when the value is not one safe token; otherwise it is the
    status returned by the read-only sessions-table lookup.
    """
    if not isinstance(value, str):
        return "", "invalid"
    selector = value.strip()
    if not selector or " " in selector:
        return "", "invalid"
    session_id = journal.normalize_prompt_note_session_id(selector)
    if not session_id:
        return "", "invalid"
    _, lookup_status = core._get_session_source_status(session_id)
    return session_id, lookup_status


def _is_model_target(remainder: str) -> bool:
    """Whether text is exactly ``<model>`` or ``<provider>/<model>``.

    Only the first slash separates provider from model; any further ones belong to
    the model id, which is commonly namespaced (``openrouter/deepseek/deepseek-chat``).
    Rejecting those made the command start a real refine pass instead — spending a
    daily edit on a mistyped setting.

    Anything else — prose, an empty half such as ``a/`` or ``/b`` — is a free-form
    reason and must reach the proposal path untouched. The rule itself lives in
    ``journal``, which owns the store.
    """
    head, separator, tail = remainder.partition("/")
    if not journal.valid_model_identifier(head):
        return False
    if not separator:
        return True
    return bool(tail) and journal.valid_model_id(tail)


def _handle_model_subcommand(remainder: str) -> str:
    """Handle /refine model [auto | <provider/model> | <model>]."""
    if _session_llm() is not None:
        if not remainder:
            return "model: active host invocation (bound route; stored overrides are ignored)"
        return (
            "❌ Refine uses the active host invocation model; "
            f"{_command_display_name()} model cannot change a bound route."
        )
    trust_model = config.llm_allow_model_override()
    trust_prov = config.llm_allow_provider_override()

    if not remainder:
        # Show current effective target
        effective = config.effective_llm_target()
        source = effective["source"]
        set_model = effective.get("model", "")
        set_provider = effective.get("provider", "")
        lines = [
            f"model: {set_model or '(host default)'}",
            f"provider: {set_provider or '(host default)'}",
            f"source: {source}",
            f"trust: model={'allowed' if trust_model else 'denied'}, "
            f"provider={'allowed' if trust_prov else 'denied'}",
        ]
        for issue in effective.get("issues", ()):
            lines.append(f"⚠ {issue}")
        # Only warn where trust actually changes the outcome. On ``live`` the
        # user set nothing and the host uses that model regardless, so warning
        # there would report a problem that does not exist.
        if source in ("command", "config"):
            if set_model and not trust_model:
                lines.append(
                    "⚠ Model is set but host trust denies overrides. "
                    "Enable plugins.entries.refine.llm.allow_model_override to apply it."
                )
            if set_provider and not trust_prov:
                lines.append(
                    "⚠ Provider is set but host trust denies overrides. Enable "
                    "plugins.entries.refine.llm.allow_provider_override to apply it."
                )
        return core.scrub_text("\n".join(lines))

    if remainder == "auto":
        outcome = journal.clear_model_override()
        effective = config.effective_llm_target()
        prefix = {
            "removed": "Override removed.",
            "absent": "No override was set.",
            # Does not claim the override is "still in force": the file surviving
            # and the file being usable are different things, and the effective
            # target printed next is the accurate answer either way.
            "failed": "⚠ Could not remove the override file.",
        }[outcome]
        return core.scrub_text(
            f"{prefix} Effective model: {effective.get('model') or '(host default)'} "
            f"(source: {effective['source']})"
        )

    # Parse provider/model or bare model. The store validates and refuses; doing
    # it again here would put the same rule in two places that could drift.
    provider = ""
    model = remainder
    if "/" in remainder:
        provider, model = remainder.split("/", 1)

    journal.write_model_override(provider, model)
    lines = [f"Override set: model={model}" + (f" provider={provider}" if provider else "")]
    if not trust_model:
        lines.append(
            "⚠ Host trust denies model overrides. The value is saved but will not "
            "be sent until plugins.entries.refine.llm.allow_model_override is true."
        )
    if provider and not trust_prov:
        lines.append(
            "⚠ Host trust denies provider overrides. The provider value is saved but "
            "will not be sent until plugins.entries.refine.llm.allow_provider_override is true."
        )
    return core.scrub_text("\n".join(lines))


def _mistyped_subcommand_error(args: str) -> Optional[str]:
    """Usage text when a lone token is a near-miss for a real subcommand.

    Falling through to the proposal path meant ``/refine auditt`` started a
    *mutation* run with "auditt" as its reason instead of reporting a typo.

    Only a single leading token is judged, because arbitrary prose is a
    legitimate reason (``/refine the tests keep failing``); anything containing
    whitespace goes straight through. A lone token is refused only when it is
    close enough to a real subcommand to be a typo of it, so an unrelated
    one-word reason such as ``timeouts`` still runs. ``sessions`` and ``models``
    do match, which is intended: they are subcommand attempts, not reasons, and
    a usage line costs nothing next to spending an edit from the daily budget.
    """
    if not args or any(char.isspace() for char in args):
        return None
    if args in _KNOWN_SUBCOMMANDS:
        # Each exact name is handled by its own branch above. Reaching here
        # would be a routing bug, and reporting it as a typo would mislead.
        return None
    close = difflib.get_close_matches(args, _KNOWN_SUBCOMMANDS, n=1, cutoff=0.8)
    if not close:
        return None
    name = _command_display_name()
    return (
        f"❌ Unknown subcommand '{core.scrub_text(args)}'. Did you mean '{close[0]}'?\n"
        f"Usage: {name} [audit | status | dry-run | model | "
        f"rollback <journal_id> | session <session_id>]\n"
        f"Any other text is a reason, e.g. {name} the tests keep failing"
    )


def _handle_refine_command(raw_args: str) -> Optional[str]:
    """Handle exact audit/rollback subcommands; all other text is a reason."""
    args = raw_args.strip()
    if args == "audit":
        try:
            return core.refine_audit().get("report", "No data.")
        except Exception as exc:
            logger.exception("refine audit failed")
            return f"❌ Audit failed: {core.scrub_text(str(exc))}"

    if args == "status":
        try:
            status = core.refine_status()
        except Exception as exc:
            logger.exception("refine status failed")
            return f"❌ Status failed: {core.scrub_text(str(exc))}"
        command_blockers = list(status["blockers"])
        if _session_llm() is None:
            command_blockers.append({
                "code": "llm_invocation_unavailable",
                "message": (
                    "No invocation-bound host LLM is available in this command "
                    "context; proposal-producing /refine commands cannot run here."
                ),
            })
        lines = [
            f"auto: {'on' if status['auto_enabled'] else 'off'}",
            f"turn interval: {status['auto_turn_interval']}"
            + ("" if status["turn_trigger_enabled"] else " (turn trigger off)"),
            f"min messages: {status['auto_min_messages']}",
            f"cooldown: {status['auto_cooldown_minutes']} min",
            f"edits today: {status['edits_today']}/{status['max_edits_per_day']}",
            f"session: {status['session_id'] or '(unknown)'}"
            + f" (source: {status['session_id_source']}"
            + f", messages: {status['session_message_count']})",
            f"session db source: {status['session_source'] or '(empty/unknown)'}",
            "skipped session sources: "
            + (", ".join(status["skip_session_sources"]) or "(none)"),
            f"model: {status['llm_model'] or '(host default)'}"
            + (f" @ {status['llm_provider']}" if status["llm_provider"] else "")
            + f" (source: {status['llm_target_source']})",
            # The DoD line: which proposer serves a proposal run and why.
            (
                "proposer: subagent (host lifecycle bound, config enabled)"
                if status["proposer"]["effective"] == "subagent"
                else (
                    "proposer: structured (subagent arm unavailable: "
                    + (
                        "disabled in config"
                        if not status["proposer"]["subagent_config_enabled"]
                        else "host lifecycle not bound"
                    )
                    + ")"
                )
            ),
            # Route presence: without the core patch every proposal run stops
            # at llm_invocation_unavailable. Say it here, with the fix, instead
            # of leaving the user to find the registration warning.
            (
                "route: present (invocation-bound LLM available)"
                if status.get("route_present") is True
                else (
                    "route: MISSING — Hermes core lacks the invocation-route "
                    "patch; refine_run will stop with llm_invocation_unavailable. "
                    "Run install.sh from the plugin directory."
                    if status.get("route_present") is False
                    else "route: unknown (host plugin module not importable here)"
                )
            ),
            (
                "last model substitution: yes (reviewer verdict not trustworthy)"
                if status.get("last_model_substituted")
                else "last model substitution: no"
            ),
            f"journal: {status['journal_dir']} ({status['journal_dir_state_text']})",
            (
                (
                    "storage: "
                    if status['persistence']['total_bytes_complete']
                    else "storage: at least "
                )
                + f"{status['persistence']['total_bytes']} bytes total; "
                f"journal={status['persistence']['journal'].get('bytes')} bytes/"
                f"{status['persistence']['journal'].get('physical_lines')} lines; "
                f"backups={status['persistence']['backups'].get('count')}/"
                f"{status['persistence']['backups'].get('bytes')} bytes; "
                f"ledger={status['persistence']['ledger'].get('bytes')} bytes"
            ),
            f"migration: {status['migration_outcome']}"
            + (
                f" (active: {status['migration'].get('active_dir')})"
                if status["migration"].get("active_dir")
                else ""
            ),
        ]
        if status["cooldown_remaining_minutes"] > 0:
            lines.append(
                f"cooldown remaining: {status['cooldown_remaining_minutes']} min"
            )
        if status["recent_auto_events"]:
            lines.append("recent auto events:")
            lines.extend(
                f"  • {event.get('code', 'unknown')} — {event.get('message', '')}"
                for event in status["recent_auto_events"][-3:]
            )
        if command_blockers:
            lines.append("blockers:")
            lines.extend(f"  • {item['message']}" for item in command_blockers)
        else:
            lines.append("blockers: none — automatic refinement is active")
        if status["warnings"]:
            lines.append("warnings:")
            lines.extend(f"  ⚠ {item['message']}" for item in status["warnings"])
        return core.scrub_text("\n".join(lines))

    if args == "dry-run" or args.startswith("dry-run "):
        dry_reason = args[7:].strip()  # len("dry-run") == 7
        dry_session = ""
        if dry_reason == _SESSION_SUBCOMMAND:
            return (
                f"Usage: {_command_display_name()} dry-run session <session_id>\n"
                "Previews that exact session without applying an edit.\n"
                f"Find ids in the sessions table of {config.state_db_path()}"
            )
        if dry_reason.startswith(_SESSION_SUBCOMMAND + " "):
            selector = dry_reason[len(_SESSION_SUBCOMMAND):].strip()
            # A single token after ``dry-run session`` is an explicit selector.
            # Prose remains a reason, matching the ordinary session subcommand.
            if " " not in selector:
                dry_session, lookup_status = _explicit_session_status(selector)
                if lookup_status == "invalid":
                    return (
                        "❌ That is not a usable session id.\n"
                        f"Usage: {_command_display_name()} dry-run session <session_id>\n"
                        f"Find ids in the sessions table of {config.state_db_path()}"
                    )
                if lookup_status == "error":
                    return (
                        "❌ Cannot read the sessions table to confirm that session.\n"
                        f"Checked: {config.state_db_path()}"
                    )
                if lookup_status != "ok":
                    return (
                        f"❌ No session '{core.scrub_text(dry_session)}' exists.\n"
                        f"Usage: {_command_display_name()} dry-run session <session_id>\n"
                        f"Find ids in the sessions table of {config.state_db_path()}"
                    )
                dry_reason = ""
        try:
            result = core.refine_run(
                llm=_session_llm(),
                reason=dry_reason,
                session_id=dry_session or None,
                auto=False,
                dry_run=True,
                explicit_session=bool(dry_session),
                # This handler runs inside the turn, so the chat context is valid.
                active_chat=_capture_active_chat(),
            )
        except Exception as exc:
            logger.exception("refine dry-run failed")
            return f"❌ Dry-run failed: {core.scrub_text(str(exc))}"
        if result.get("outcome") == "dry_run":
            proposal = result.get("proposal", {})
            lines = ["🔍 Dry run — nothing applied."]
            evidence = result.get("evidence", {})
            if evidence.get("session_id"):
                lines.append(
                    f"session: {evidence['session_id']} "
                    f"(source: {evidence.get('session_id_source', 'unknown')})"
                )
            if proposal.get("action") and proposal["action"] != "no_op":
                lines.append(
                    f"action: {proposal.get('action')} | kind: {proposal.get('kind', '')} "
                    f"| name: {proposal.get('name', '')}"
                )
                if proposal.get("summary"):
                    lines.append(f"summary: {proposal['summary']}")
                if proposal.get("expected_outcome"):
                    lines.append(f"expected: {proposal['expected_outcome']}")
            else:
                lines.append(f"action: no_op | reason: {proposal.get('reason', '')}")
            diff = result.get("diff", "")
            if diff:
                lines.append("\n```diff")
                lines.append(diff)
                lines.append("```")
                if result.get("diff_truncated"):
                    lines.append("(diff truncated)")
            return core.scrub_text("\n".join(lines))
        # Non-dry-run outcome (session_unknown, skipped, etc.)
        if not result.get("success"):
            return f"❌ {result.get('message', 'Unknown error')}"
        return result.get("message", "No proposal.")

    if args == _MODEL_SUBCOMMAND or args.startswith(_MODEL_SUBCOMMAND + " "):
        remainder = args[len(_MODEL_SUBCOMMAND):].strip()
        # Only treat as a subcommand when:
        # - no remainder (show current)
        # - remainder is "auto"
        # - remainder is exactly a model or provider/model token
        # Anything else ("model of gmail failures") is a free-form reason.
        if not remainder or remainder == "auto" or _is_model_target(remainder):
            try:
                return _handle_model_subcommand(remainder)
            except Exception as exc:
                # The other subcommands report failures; this one used to escape
                # as a traceback on exactly the unwritable journal_dir that
                # /refine status exists to diagnose.
                logger.exception("refine model command failed")
                return f"❌ Model command failed: {core.scrub_text(str(exc))}"
        if remainder and ("/" in remainder or " " not in remainder):
            return (
                "❌ Invalid model target.\n"
                f"Usage: {_command_display_name()} model [auto | <model> | <provider>/<model>]"
            )
        # Prose such as "model of gmail failures" remains a refine reason.

    if args == _SESSION_SUBCOMMAND or args.startswith(_SESSION_SUBCOMMAND + " "):
        remainder = args[len(_SESSION_SUBCOMMAND):].strip()
        if not remainder:
            return (
                f"Usage: {_command_display_name()} session <session_id>\n"
                "Analyses that exact session instead of the current one.\n"
                f"Find ids in the sessions table of {config.state_db_path()}"
            )
        # Only a single token is a selector; prose such as "session handling keeps
        # failing" stays a free-form reason. A single token is still confirmed
        # against the sessions table, so a one-word reason reports "not found"
        # instead of silently analysing the wrong session or spending a pass.
        if " " not in remainder:
            explicit_session, lookup_status = _explicit_session_status(remainder)
            if lookup_status == "invalid":
                # A lone token that cannot be an id is refused rather than run as
                # a reason: falling through would analyse the *current* session.
                return (
                    "❌ That is not a usable session id.\n"
                    f"Usage: {_command_display_name()} session <session_id>\n"
                    f"Find ids in the sessions table of {config.state_db_path()}"
                )
            if lookup_status == "error":
                return (
                    "❌ Cannot read the sessions table to confirm that session.\n"
                    f"Checked: {config.state_db_path()}"
                )
            if lookup_status != "ok":
                return (
                    f"❌ No session '{core.scrub_text(explicit_session)}' exists.\n"
                    f"Usage: {_command_display_name()} session <session_id>\n"
                    f"Find ids in the sessions table of {config.state_db_path()}"
                )
            try:
                result = core.refine_run(
                    llm=_session_llm(),
                    reason="",
                    session_id=explicit_session,
                    auto=False,
                    explicit_session=True,
                    active_chat=_capture_active_chat(),
                )
            except Exception as exc:
                logger.exception("refine session command failed")
                return f"❌ Refine failed: {core.scrub_text(str(exc))}"
            return _format_run_result(result)
        # Anything else is prose and reaches the proposal path untouched.

    if args == "rollback":
        return (
            f"Usage: {_command_display_name()} rollback <12-character journal_id>\n"
            f"Find ids in {journal.journal_read_path()}"
        )
    rollback_match = _ROLLBACK_COMMAND.fullmatch(args)
    if rollback_match:
        entry_id = rollback_match.group(1).lower()
        try:
            result = core.refine_rollback(entry_id)
        except Exception as exc:
            logger.exception("refine rollback failed")
            return f"❌ Rollback failed: {core.scrub_text(str(exc))}"
        if result.get("success"):
            return f"✅ Rollback {entry_id}: {result.get('message', 'done')}"
        return f"❌ Rollback failed: {result.get('error', 'unknown error')}"
    if args.startswith("rollback "):
        return (
            "❌ Invalid rollback format. Expected a 12-character hex journal ID.\n"
            f"Usage: {_command_display_name()} rollback <12-character journal_id>\n"
            f"Find ids in {journal.journal_read_path()}"
        )

    # Last gate before the reason path: a lone mistyped subcommand must not be
    # spent as a refine run. Checked here, after every real subcommand branch,
    # so it can only see text that was already going to be treated as prose.
    mistyped = _mistyped_subcommand_error(args)
    if mistyped:
        return mistyped

    try:
        result = core.refine_run(
            llm=_session_llm(), reason=args, auto=False,
            active_chat=_capture_active_chat(),
        )
    except Exception as exc:
        logger.exception("refine command failed")
        return f"❌ Refine failed: {core.scrub_text(str(exc))}"

    return _format_run_result(result)


def _format_run_result(result: Dict[str, Any]) -> str:
    """Render one refine run for the command, shared by every run entry point."""
    if not result.get("success") and result.get("outcome") != "partial_success":
        return f"❌ {result.get('message', 'Unknown error')}"

    summary = str(result.get("message") or "done")
    evidence = result.get("evidence", {})
    if evidence.get("session_id"):
        summary += (
            f"\nsession: {evidence['session_id']} "
            f"(source: {evidence.get('session_id_source', 'unknown')})"
        )
    if result.get("outcome") == "partial_success":
        summary = "⚠️ " + summary
    proposal = result.get("proposal", {})
    edits = proposal.get("edits")
    if isinstance(edits, list) and edits:
        if proposal.get("summary"):
            summary += f"\n📝 {proposal['summary']}"
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            summary += (
                f"\n   • {edit.get('action', '?')} {edit.get('kind', '?')} "
                f"\"{edit.get('name', '?')}\""
            )
    elif proposal.get("action") not in (None, "no_op"):
        summary += (
            f"\n📝 {proposal.get('action', '?')} {proposal.get('kind', '?')} "
            f"\"{proposal.get('name', '?')}\""
        )
    recoveries = result.get("recoveries", [])
    if recoveries:
        summary += "\nRecovery / rollback IDs:"
        for recovery in recoveries:
            journal_id = recovery.get("journal_id", "?")
            line = f"\n🔖 {journal_id} ({recovery.get('outcome', 'unknown')})"
            if recovery.get("rollback_command"):
                line += f" — {recovery['rollback_command']}"
            summary += line
    else:
        journal_id = result.get("journal_id")
        if journal_id:
            summary += f"\n🔖 {journal_id}"
            if result.get("reversible"):
                summary += f" (rollback: {_command_display_name()} rollback {journal_id})"
    return summary


def _handle_refine_run(args: dict, **kw) -> str:
    payload = args if isinstance(args, dict) else {}
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        return json.dumps({
            "success": False,
            "error": "reason must be a string.",
        })
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return json.dumps({
            "success": False,
            "error": "dry_run must be a boolean.",
        })

    session_id = ""
    supplied_session = payload.get("session_id")
    if supplied_session not in (None, ""):
        session_id, lookup_status = _explicit_session_status(supplied_session)
        if lookup_status == "invalid":
            return json.dumps({
                "success": False,
                "error": "session_id must be one usable session token.",
            })
        if lookup_status == "error":
            return json.dumps({
                "success": False,
                "error": "Cannot read the sessions table to confirm session_id.",
            })
        if lookup_status != "ok":
            return json.dumps({
                "success": False,
                "error": f"No session '{core.scrub_text(session_id)}' exists.",
            })

    try:
        result = core.refine_run(
            llm=_session_llm(),
            reason=reason,
            session_id=session_id or None,
            auto=False,
            dry_run=dry_run,
            explicit_session=bool(session_id),
            # The tool handler runs inside the turn, like the command handler.
            active_chat=_capture_active_chat(),
        )
    except Exception as exc:
        logger.exception("refine_run tool failed")
        return json.dumps({"success": False, "error": core.scrub_text(str(exc))})
    return json.dumps(result, ensure_ascii=False)


def _on_session_end(
    session_id: str = "",
    turn_id: str = "",
    completed: bool = False,
    interrupted: bool = False,
    _bound_llm: Any = _BOUND_LLM_UNSET,
    _worker_claimed: bool = False,
    _active_chat: Any = _ACTIVE_CHAT_UNSET,
    **kwargs,
) -> None:
    """Run the session-end fallback without blocking or losing its bound route."""
    # A deferred drain calls this function from a worker and supplies the facade
    # captured by the original callback. Only a real host callback resolves it.
    bound_llm = _session_llm() if _bound_llm is _BOUND_LLM_UNSET else _bound_llm
    # Same contract for the active chat: resolve it here only when this really is
    # a host callback. A drain runs on a worker whose context is not the turn's,
    # so it hands back the triple its original callback captured — reading it
    # again would address the wrong chat, or none.
    active_chat = (
        _capture_active_chat() if _active_chat is _ACTIVE_CHAT_UNSET else _active_chat
    )
    core.note_session_id(session_id)
    _forget_turn_marks(session_id)
    if not config.auto_enabled() or interrupted:
        _clear_session_prompt_notes(session_id, timeout=_HOST_PATH_LOCK_TIMEOUT)
        if _worker_claimed:
            _finish_auto_worker()
        return
    if not _worker_claimed and not _defer_or_claim_session_end(
        session_id, bound_llm, active_chat
    ):
        return

    def _collect_and_run() -> None:
        handed_off = False
        try:
            session_source, _ = core._get_session_source_status(session_id)
            if (
                session_source
                and session_source.lower() in config.skip_session_sources()
            ):
                # Let the normal source gate create the non-budget journal record,
                # but do not fetch even one trajectory row in this preflight.
                handed_off = True
                _run_auto_refine(
                    session_id, bound_llm, cleanup_session_notes=True,
                    active_chat=active_chat,
                )
                return
            # Session-end only gates on ``auto_min_messages``. Count at most that
            # many active rows without selecting role/content/tool payloads; the
            # actual refine pass is the single owner of full evidence collection
            # and independently sizes its reviewer window when that path is on.
            message_threshold = config.auto_min_messages()
            preflight = core.count_session_messages(
                session_id=session_id, limit=message_threshold
            )
            collection_status = str(preflight.get("collection_status", "ok"))
            if collection_status != "ok":
                logger.warning(
                    "refine auto: evidence unavailable (%s); recording durable failure",
                    core.scrub_text(collection_status),
                )
                entry_id = core.record_evidence_failure(
                    session_id,
                    collection_status,
                    str(preflight.get("collection_error", "")),
                    trigger="auto",
                )
                if not entry_id:
                    core.note_auto_event(
                        "evidence_failure_not_persisted",
                        f"Evidence failure {collection_status} could not be journaled",
                    )
                return
            message_count = int(preflight.get("count", 0) or 0)
            if message_count < message_threshold:
                logger.debug("refine auto: not enough messages (%d)", message_count)
                return
            handed_off = True
            _run_auto_refine(
                session_id, bound_llm, cleanup_session_notes=True,
                active_chat=active_chat,
            )
        except Exception:
            logger.exception("refine auto session-end hook failed")
        finally:
            if not handed_off:
                _clear_session_prompt_notes(session_id)
                _finish_auto_worker()

    try:
        threading.Thread(
            target=_collect_and_run, daemon=True, name="refine-auto"
        ).start()
    except Exception:
        _finish_auto_worker()
        logger.exception("refine auto session-end thread could not start")


REFINE_RUN_SCHEMA = {
    "name": "refine_run",
    "description": (
        "Trigger a self-improvement pass over recent repeated failures or explicit corrections "
        "using the active host-provided LLM. Mutations are serialized, journaled, and reversible "
        "when applied. Use dry_run with an explicit historical session before autorun apply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Optional issue or area to focus on; passed to the proposal model.",
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional exact historical session id to analyze. It must exist in the "
                    "read-only Hermes sessions table."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Preview and journal the proposal without applying edits or consuming the "
                    "daily edit budget."
                ),
            },
        },
        "required": [],
    },
}


def _on_subagent_start(
    child_subagent_id: str = "",
    child_session_id: str = "",
    **_: Any,
) -> None:
    """Mark a child this plugin launched as a proposer (read-only session)."""
    if not child_subagent_id or not child_session_id:
        return
    if str(child_subagent_id) in core._PROPOSER_SUBAGENT_IDS:
        _PROPOSER_CHILD_SESSIONS.add(str(child_session_id))


def _on_subagent_stop(
    child_subagent_id: str = "",
    child_session_id: str = "",
    **_: Any,
) -> None:
    """Drop the read-only marker once the proposer child has finished."""
    if child_session_id:
        _PROPOSER_CHILD_SESSIONS.discard(str(child_session_id))
    if child_subagent_id:
        core._PROPOSER_SUBAGENT_IDS.discard(str(child_subagent_id))


def register(ctx) -> None:
    global _REGISTERED_CONTEXT
    _REGISTERED_CONTEXT = ctx
    # Hand the host's plugin-safe subagent lifecycle service to the proposer
    # path. Accessor, not the service itself: the service resolves the active
    # parent lazily, which is only ever correct at proposal time.
    core._set_subagent_lifecycle_provider(lambda: ctx.subagent_lifecycle)
    # One-time migration of runtime data out of the plugin install directory.
    # Must not fail registration — a broken migration just leaves data in place.
    try:
        journal.migrate_legacy_journal_dir()
    except Exception:
        logger.debug("refine journal migration failed", exc_info=True)
    # The command name is decided at registration time: newer Hermes cores ship
    # their own built-in /refine (a background review fork), and register_command
    # silently drops plugin commands that collide with it. When the built-in
    # exists, the plugin registers as /refine-cycle instead so its subcommands
    # (audit/status/dry-run/session/model/rollback) stay reachable, and every
    # usage hint renders the name that actually answers.
    command_name = _resolve_command_name()
    ctx.register_command(
        command_name,
        _handle_refine_command,
        description=(
            "Self-improve skills/memory. "
            f"Usage: /{command_name} [reason|audit|status|dry-run [session <session_id>|reason]|"
            "model [target|auto]|session <session_id>|rollback <id>]"
        ),
        args_hint=(
            "[reason | audit | status | dry-run [session <session_id> | reason] | "
            "model [target|auto] | session <session_id> | rollback <id>]"
        ),
    )
    ctx.register_tool(
        "refine_run",
        "refine",
        REFINE_RUN_SCHEMA,
        _handle_refine_run,
        description="Run one self-improvement pass over trajectory",
        emoji="🧠",
    )
    # Refine's whole point is improving the agent without anyone clicking approve.
    # The host's write-approval gate queues every memory and skill write, the
    # agent's own included, and a queue nobody drains looks exactly like an agent
    # that quietly stopped learning. Turned off here rather than documented.
    try:
        disabled = config.disable_host_write_approval()
        if disabled:
            logger.warning(
                "Refine turned off host write approval for %s: it queues every "
                "memory and skill write until someone approves them by hand. "
                "A backup of the previous config is at config.yaml.refine-bak.",
                ", ".join(disabled),
            )
    except Exception:
        logger.debug("write approval check failed", exc_info=True)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
    _warn_if_core_patch_missing()
    _warn_on_register()


_REGISTER_WARNED = False

# Set by register(): the slash-command name that actually answers in this host
# ("refine" normally, "refine-cycle" when the core's own built-in /refine won).
_COMMAND_NAME = "refine"
_FALLBACK_COMMAND_NAME = "refine-cycle"


def _built_in_command_exists(name: str) -> bool:
    """True when this Hermes core already owns a built-in command with that name."""
    try:
        from hermes_cli.commands import resolve_command
    except Exception:
        return False
    try:
        return resolve_command(name) is not None
    except Exception:
        return False


def _resolve_command_name() -> str:
    """Pick the plugin's slash-command name for this host.

    Prefers ``refine``; falls back to ``refine-cycle`` only when the core
    already ships a built-in command with that exact name (register_command
    would otherwise drop the registration silently).
    """
    global _COMMAND_NAME
    if _built_in_command_exists("refine"):
        _COMMAND_NAME = _FALLBACK_COMMAND_NAME
        logger.warning(
            "Refine plugin: built-in /%s detected; registering as /%s instead.",
            "refine",
            _FALLBACK_COMMAND_NAME,
        )
    else:
        _COMMAND_NAME = "refine"
    return _COMMAND_NAME


def _command_display_name() -> str:
    """The slash name to render in usage hints, e.g. '/refine' or '/refine-cycle'."""
    return "/" + _COMMAND_NAME



def _core_patch_present() -> bool:
    """True when the installed Hermes core carries the invocation-route patch.

    Checked by marker symbol; import failure means "cannot know" -> False so
    the registration warning still fires and the user can verify manually.
    """
    try:
        from hermes_cli import plugins as host_plugins
        return hasattr(host_plugins, "plugin_invocation_scope")
    except Exception:
        return False


def _warn_if_core_patch_missing() -> None:
    """One-line warning with the exact fix command when the core is unpatched.

    Without the invocation-route patch refine_run cannot reach a bound LLM
    route: every run ends llm_invocation_unavailable. Silent absence looks
    identical to "no signal", so this is warned at registration.
    """
    if _core_patch_present():
        return
    logger.warning(
        "Refine: Hermes core lacks the invocation-route patch; refine_run "
        "will stop with llm_invocation_unavailable. Run install.sh from the "
        "plugin directory (or set plugins.entries.refine.auto_apply_core_patch=true)."
    )


def _warn_on_register() -> None:
    """Warn once per process that runtime data sits in the plugin directory."""
    global _REGISTER_WARNED
    if _REGISTER_WARNED:
        return
    try:
        jdir = config.journal_dir()
        if not (jdir / "plugin.yaml").is_file():
            return
    except Exception:
        return
    # Set only after the condition held, so a warning is never skipped because
    # an earlier call returned before there was anything to report.
    _REGISTER_WARNED = True
    logger.warning(
        "Refine journal_dir (%s) contains plugin source code. "
        "A forced reinstall may delete runtime data (journal, backups, ledger). "
        "Set plugins.entries.refine.journal_dir to a separate path.",
        jdir,
    )
