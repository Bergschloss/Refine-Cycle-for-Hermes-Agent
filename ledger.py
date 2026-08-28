"""Timestamp-aware usefulness ledger for refine-created entries."""

import json
import logging
import math
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import journal
    from . import config as _config
    from .config import journal_dir, state_db_path
    from .sanitization import scrub_text
except ImportError:
    import journal  # type: ignore
    import config as _config  # type: ignore
    from config import journal_dir, state_db_path  # noqa: F811
    from sanitization import scrub_text  # type: ignore

logger = logging.getLogger(__name__)
_STATS_FILE_NAME = "skill_stats.json"


def stats_path() -> Path:
    path = journal_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path / _STATS_FILE_NAME


def stats_read_path() -> Path:
    """Ledger location for readers, without creating anything."""
    return journal_dir() / _STATS_FILE_NAME


def load_stats() -> Dict[str, Any]:
    """Load the skill stats ledger, distinguishing absence from corruption.

    Returns {} only when the file is genuinely absent. On read/parse error the
    function raises IOError so callers do not overwrite a corrupted or locked
    file with a single entry.
    """
    path = stats_read_path()
    if not path.is_file():
        return {}
    try:
        raw = journal._retry_on_contention(
            lambda: path.read_text(encoding="utf-8"),
            journal._READ_RETRY_BUDGET_SECONDS,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("ledger root must be an object")
        return data
    except Exception as exc:
        logger.error("Cannot read skill stats: %s", exc)
        raise IOError(f"Ledger unreadable: {scrub_text(str(exc))}") from exc


def _save_stats(stats: Dict[str, Any]) -> None:
    path = stats_path()
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(stats, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        journal._replace_with_retry(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


# Outcomes that leave nothing behind on the host, so they describe a record
# rather than an artifact the audit can measure. A resolved session cleanup did
# leave a real note temporarily; it is listed here only because no artifact
# remains now, not because the original edit failed to land.
_NO_ARTIFACT_OUTCOMES = frozenset({
    "error", "rejected", "rolled_back", "cleanup_resolved",
})
_STAGED_OUTCOMES = frozenset({
    "prepared", "pending_approval", "cleanup_prepared",
})


def _kind_is_skill(kind: Any) -> bool:
    """Treat only missing/empty legacy kinds as skills, never named unknown kinds."""
    return kind is None or (isinstance(kind, str) and not kind.strip()) or kind == "skill"


def _finite_float(value: Any) -> Optional[float]:
    """Return one persisted numeric value only when it is finite."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def record_edit(
    proposal: Dict[str, Any],
    journal_id: str,
    *,
    outcome: str = "applied",
    pending_id: str = "",
    llm_meta: Optional[Dict[str, Any]] = None,
    entry_ts: Optional[float] = None,
) -> None:
    name = str(proposal.get("name", "")).strip()
    if not name:
        return
    kind = str(proposal.get("kind", "skill") or "skill")
    # Skills keep their bare name as the key so existing statistics, the audit
    # table, and the prompt overview keep resolving. Other kinds are namespaced,
    # because one transaction can legitimately create a skill and a same-named
    # memory entry, and a shared key would hide one of them from the audit.
    key = name if kind == "skill" else f"{kind}:{name}"
    with journal.mutation_lock():
        stats = load_stats()
        # Before kind-qualified keys existed, every entry used its bare name.
        # Move a legacy non-skill row out of the skill namespace before any
        # write, including a same-named skill write, so history and versions
        # survive the upgrade instead of becoming a duplicate audit row.
        legacy = stats.get(name)
        if isinstance(legacy, dict):
            legacy_kind = str(legacy.get("kind", "skill") or "skill")
            if legacy_kind != "skill":
                legacy_key = f"{legacy_kind}:{name}"
                stats.setdefault(legacy_key, legacy)
                del stats[name]
        previous = stats.get(key, {})
        if not isinstance(previous, dict):
            # Keep an otherwise readable ledger usable when one historical row
            # was manually corrupted; this new edit owns the replacement row.
            previous = {}
        now = time.time()
        same_edit = previous.get("journal_id") == journal_id
        # A record that left no artifact must not overwrite the row of a
        # different edit that did. Rows are keyed by name, so an abandoned or
        # rejected record for a name refine had legitimately edited before would
        # otherwise reset created_ts, bump the version, replace journal_id, and
        # relabel a live edit as failed -- losing the attribution of something
        # still in effect.
        stale = False
        if not same_edit and previous:
            if outcome in _NO_ARTIFACT_OUTCOMES or (
                outcome in _STAGED_OUTCOMES and previous.get("outcome") == "applied"
            ):
                stale = True
            elif entry_ts is not None:
                # An older edit must not overwrite a newer one's row either. A
                # failed rollback mirrors its entry back as ``applied``, and a
                # staged edit mirrors as ``pending_approval``, so outcome alone
                # cannot tell "a newer edit of this name" from "an older record
                # re-asserting itself".
                try:
                    stale = float(entry_ts) < float(previous.get("created_ts", 0) or 0)
                except (TypeError, ValueError):
                    stale = False
        if stale:
            if stats.get(name) is not legacy:
                # The legacy key migration above already rewrote the mapping;
                # persist that rather than discarding it on the way out.
                _save_stats(stats)
            return
        created_ts = previous.get("created_ts", now) if same_edit else now
        default_version = 1 if previous else 0
        raw_version = previous.get("version", default_version)
        try:
            previous_version = (
                default_version if isinstance(raw_version, bool) else int(raw_version)
            )
        except (TypeError, ValueError):
            previous_version = default_version
        version = previous_version if same_edit else previous_version + 1
        stats[key] = {
            "created_ts": created_ts,
            "updated_ts": now,
            "version": version,
            "journal_id": journal_id,
            "name": name,
            "kind": kind,
            "action": proposal.get("action", ""),
            "pattern_fingerprint": proposal.get("pattern_fingerprint", ""),
            "expected_outcome": (
                scrub_text(proposal["expected_outcome"]).strip()
                if isinstance(proposal.get("expected_outcome"), str)
                else ""
            ),
            "outcome": outcome,
            "pending_id": pending_id,
        }
        # LLM attribution for the audit display is additive and survives later
        # reconciliation calls that carry no model metadata.
        reported_model = ""
        if isinstance(llm_meta, dict) and llm_meta.get("reported_model"):
            reported_model = scrub_text(str(llm_meta["reported_model"]))[:60]
        elif isinstance(previous, dict) and previous.get("reported_model"):
            reported_model = scrub_text(str(previous["reported_model"]))[:60]
        if reported_model:
            stats[key]["reported_model"] = reported_model
        _save_stats(stats)


def record_journal_state(entry: Dict[str, Any]) -> None:
    """Mirror a reconciled journal state without resetting its creation time."""
    proposal = entry.get("proposal", {})
    record_edit(
        proposal,
        str(entry.get("id", "")),
        outcome=str(entry.get("outcome", "")),
        pending_id=str(entry.get("pending_id", "")),
        llm_meta=entry.get("llm_meta") if isinstance(entry.get("llm_meta"), dict) else None,
        entry_ts=entry.get("ts") if isinstance(entry.get("ts"), (int, float)) else None,
    )


def earliest_created_ts() -> Optional[float]:
    values: List[float] = []
    for meta in load_stats().values():
        if not isinstance(meta, dict) or meta.get("outcome", "applied") != "applied":
            continue
        created = _finite_float(meta.get("created_ts"))
        if created is not None:
            values.append(created)
    return min(values) if values else None


# ── usage counting ─────────────────────────────────────────────────────────


def _count_uses_with_scope(name: str, since_ts: float) -> Tuple[Optional[int], str]:
    """Return (count, scope): since_exact, all_time, since_approx, unavailable."""
    try:
        from tools import skill_usage as usage

        for function_name in ("get_usage_count", "usage_count", "get_use_count"):
            function = getattr(usage, function_name, None)
            if not callable(function):
                continue
            try:
                return int(function(name, since_ts=since_ts)), "since_exact"
            except TypeError:
                try:
                    return int(function(name)), "all_time"
                except Exception:
                    continue
            except Exception:
                continue
    except ImportError:
        pass

    try:
        path = state_db_path()
        if not path.is_file():
            return None, "unavailable"
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            escaped_name = (
                name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            row = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE active = 1 "
                "AND timestamp > ? AND (tool_name = ? OR content LIKE ? ESCAPE '\\')",
                (since_ts, name, f"%/{escaped_name}%"),
            ).fetchone()
            return (int(row[0]) if row else 0), "since_approx"
        finally:
            connection.close()
    except Exception as exc:
        logger.debug("Usage fallback failed for %s: %s", name, exc)
        return None, "unavailable"


def count_uses(name: str, since_ts: float) -> Optional[int]:
    """Compatibility API returning the best available count."""
    return _count_uses_with_scope(name, since_ts)[0]


def unused_skills(min_age_days: int = 14) -> List[str]:
    cutoff = time.time() - (min_age_days * 86400)
    result: List[str] = []
    for name, meta in load_stats().items():
        if not isinstance(meta, dict):
            continue
        created = _finite_float(meta.get("created_ts"))
        if (
            not _kind_is_skill(meta.get("kind"))
            or created is None
            or created > cutoff
            or meta.get("outcome", "applied") != "applied"
        ):
            continue
        uses, scope = _count_uses_with_scope(name, created)
        # since_approx is a heuristic (LIKE '%/name%' in message content); absence
        # of detected uses is not absence of use.  Only since_exact (the host's
        # authoritative usage counter) can prove a skill is genuinely unused.
        # Round 6 allowed since_approx here; Round 8 reverted that after review.
        if uses == 0 and scope == "since_exact":
            result.append(name)
    return result[:10]


# ── audit ──────────────────────────────────────────────────────────────────


def _merge_journal_stats(
    stats: Dict[str, Any], journal_entries: Optional[List[Dict[str, Any]]]
) -> Dict[str, Any]:
    """Fill ledger attribution gaps from the journal lifecycle authority."""
    merged = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in stats.items()
    }
    tracked_outcomes = {
        "prepared", "applied", "pending_approval", "cleanup_prepared",
        "cleanup_resolved", "rollback_prepared", "pending_rollback",
        "rolled_back", "rejected",
    }
    ordered = sorted(
        (entry for entry in (journal_entries or []) if isinstance(entry, dict)),
        key=lambda entry: _finite_float(entry.get("ts")) or 0,
    )
    for entry in ordered:
        outcome = entry.get("outcome")
        if outcome not in tracked_outcomes:
            continue
        proposal = entry.get("proposal")
        if not isinstance(proposal, dict) or proposal.get("action") not in ("create", "patch"):
            continue
        name = str(proposal.get("name", "")).strip()
        if not name:
            continue
        kind = str(proposal.get("kind", "skill") or "skill")
        key = name if kind == "skill" else f"{kind}:{name}"
        entry_id = str(entry.get("id", ""))
        timestamp = _finite_float(entry.get("ts")) or 0
        existing = merged.get(key)
        # A later rejected/rolled-back attempt for the same name did not change
        # the host. It must not erase attribution for a different live artifact.
        if (
            isinstance(existing, dict)
            and existing.get("journal_id") != entry_id
            and (
                outcome in _NO_ARTIFACT_OUTCOMES
                or (outcome in _STAGED_OUTCOMES and existing.get("outcome") == "applied")
            )
        ):
            continue
        llm_meta = entry.get("llm_meta")
        reported_model = (
            scrub_text(str(llm_meta["reported_model"]))[:60]
            if isinstance(llm_meta, dict) and llm_meta.get("reported_model")
            else ""
        )
        model_substituted = bool(
            isinstance(llm_meta, dict) and llm_meta.get("model_substituted")
        )
        if isinstance(existing, dict) and existing.get("journal_id") == entry_id:
            existing["outcome"] = entry.get("outcome", existing.get("outcome", ""))
            existing["pending_id"] = entry.get("pending_id", existing.get("pending_id", ""))
            if reported_model and not existing.get("reported_model"):
                existing["reported_model"] = reported_model
            if model_substituted and not existing.get("model_substituted"):
                existing["model_substituted"] = True
            continue
        if isinstance(existing, dict):
            try:
                existing_ts = float(existing.get("updated_ts", existing.get("created_ts", 0)) or 0)
            except (TypeError, ValueError):
                existing_ts = 0
            if existing_ts > timestamp:
                continue
            try:
                version = max(1, int(existing.get("version", 1) or 1)) + 1
            except (TypeError, ValueError):
                version = 2
        else:
            version = 1
        meta = {
            "created_ts": timestamp,
            "updated_ts": float(entry.get("finalized_ts", timestamp) or timestamp),
            "version": version,
            "journal_id": entry_id,
            "name": name,
            "kind": kind,
            "action": proposal.get("action", ""),
            "pattern_fingerprint": proposal.get("pattern_fingerprint", ""),
            "expected_outcome": (
                scrub_text(proposal["expected_outcome"]).strip()
                if isinstance(proposal.get("expected_outcome"), str)
                else ""
            ),
            "outcome": entry.get("outcome", ""),
            "pending_id": entry.get("pending_id", ""),
        }
        if reported_model:
            meta["reported_model"] = reported_model
        elif isinstance(existing, dict) and existing.get("reported_model"):
            meta["reported_model"] = existing["reported_model"]
        if model_substituted:
            meta["model_substituted"] = True
        elif isinstance(existing, dict) and existing.get("model_substituted"):
            meta["model_substituted"] = True
        merged[key] = meta
    return merged


def _latest_applied_memory_contents(
    journal_entries: Optional[List[Dict[str, Any]]],
) -> Dict[str, str]:
    """Map target -> last applied memory content the plugin itself wrote.

    Mirror of ``_latest_applied_skill_digests`` for kind="memory". The audit
    needs to know what refine last left behind so it can check whether that
    content still exists; without this map a memory row's verdict silently
    credits or blames refine for edits the host made afterwards.
    """
    latest: Dict[str, Tuple[float, str]] = {}
    for entry in journal_entries or []:
        if not isinstance(entry, dict) or entry.get("outcome") != "applied":
            continue
        proposal = entry.get("proposal")
        if not isinstance(proposal, dict):
            continue
        if _kind_is_skill(proposal.get("kind")) or proposal.get("kind") != "memory":
            continue
        if proposal.get("action") not in ("create", "patch"):
            continue
        name = str(proposal.get("name", "")).strip()
        content = proposal.get("content")
        if not name or not isinstance(content, str):
            continue
        try:
            ts = float(entry.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0
        existing = latest.get(name)
        if existing is None or ts >= existing[0]:
            latest[name] = (ts, content.strip())
    return {name: content for name, (_, content) in latest.items()}


def snapshot_memory_baselines(
    journal_entries: Optional[List[Dict[str, Any]]],
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Capture current memory targets for one journal generation under the lock."""
    baselines: Dict[str, Optional[Dict[str, Any]]] = {}
    for name, content in _latest_applied_memory_contents(journal_entries).items():
        baselines[f"memory:{name}"] = journal.memory_baseline("memory", content)
    return baselines


def _latest_applied_skill_digests(
    journal_entries: Optional[List[Dict[str, Any]]],
) -> Dict[str, str]:
    """Map skill name -> digest of the plugin's own last applied content.

    Hermes's own background review can edit the same skills refine does. If it
    edits one after refine did, a later ``working``/``did not help`` verdict on
    that skill would silently be crediting refine for the host's change. This
    lets ``audit()`` compare what refine last intended to leave behind against
    what is actually on disk now, without assuming refine owns every mutation.
    """
    latest: Dict[str, Tuple[float, str]] = {}
    for entry in journal_entries or []:
        if not isinstance(entry, dict) or entry.get("outcome") != "applied":
            continue
        proposal = entry.get("proposal")
        if not isinstance(proposal, dict):
            continue
        if not _kind_is_skill(proposal.get("kind")) or proposal.get("action") not in ("create", "patch"):
            continue
        name = str(proposal.get("name", "")).strip()
        content = proposal.get("content")
        if not name or not isinstance(content, str):
            continue
        try:
            ts = float(entry.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0
        existing = latest.get(name)
        if existing is None or ts >= existing[0]:
            latest[name] = (ts, journal.content_digest(content))
    return {name: digest for name, (_, digest) in latest.items()}


def snapshot_skill_baselines(
    journal_entries: Optional[List[Dict[str, Any]]],
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Capture current targets for one journal generation under the caller's lock."""
    return {
        name: journal.skill_baseline(name)
        for name in _latest_applied_skill_digests(journal_entries)
    }


def audit(
    current_patterns: Optional[List[Dict[str, Any]]] = None,
    *,
    journal_entries: Optional[List[Dict[str, Any]]] = None,
    stats_snapshot: Optional[Dict[str, Any]] = None,
    skill_baselines: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
    memory_baselines: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    patterns_available = current_patterns is not None
    by_fingerprint = {
        str(item.get("fingerprint", "")): item for item in (current_patterns or [])
    }
    now = time.time()
    rows: List[Dict[str, Any]] = []
    stats = stats_snapshot if stats_snapshot is not None else load_stats()
    merged_stats = _merge_journal_stats(stats, journal_entries)
    intended_skill_digests = _latest_applied_skill_digests(journal_entries)
    intended_memory_contents = _latest_applied_memory_contents(journal_entries)
    for key, meta in sorted(merged_stats.items()):
        if not isinstance(meta, dict):
            logger.warning("Ignoring malformed ledger row for %s", scrub_text(str(key)))
            continue
        # Legacy rows have no explicit name; their key is the name.
        name = str(meta.get("name") or key)
        created = _finite_float(meta.get("created_ts"))
        if created is None:
            created = now
        age_days = max(0, int((now - created) // 86400))
        try:
            version = max(1, int(meta.get("version", 1) or 1))
        except (TypeError, ValueError):
            version = 1
        try:
            updated_ts = float(meta.get("updated_ts", created) or created)
        except (TypeError, ValueError):
            updated_ts = created
        outcome = meta.get("outcome", "applied")
        fingerprint = str(meta.get("pattern_fingerprint", "") or "")
        recurred: Optional[bool] = None

        if outcome == "prepared":
            uses, usage_scope = None, "unavailable"
            verdict = "recovery needed"
        elif outcome == "pending_approval":
            uses, usage_scope = None, "unavailable"
            verdict = "pending approval"
        elif outcome == "cleanup_prepared":
            uses, usage_scope = None, "unavailable"
            verdict = "session cleanup pending"
        elif outcome == "cleanup_resolved":
            uses, usage_scope = None, "unavailable"
            verdict = "session note expired"
        elif outcome in ("rollback_prepared", "pending_rollback"):
            uses, usage_scope = None, "unavailable"
            verdict = "rollback pending"
        elif outcome == "rolled_back":
            uses, usage_scope = None, "unavailable"
            verdict = "rolled back"
        elif outcome == "rejected":
            uses, usage_scope = None, "unavailable"
            verdict = "rejected"
        elif outcome == "error":
            # No artifact exists, so usage and usefulness are not questions that
            # can be asked. Without this branch the row fell through to the
            # applied path and was given a "did not help" / "unused" verdict for
            # an edit that never landed.
            uses, usage_scope = None, "unavailable"
            verdict = "no edit landed"
        else:
            if meta.get("kind", "skill") == "skill":
                uses, usage_scope = _count_uses_with_scope(name, created)
            else:
                # The host exposes a usage counter only for skills. Counting a
                # memory/prompt name in conversations is neither authoritative
                # nor useful evidence, so keep that dimension unavailable.
                uses, usage_scope = None, "unavailable"
            horizon_days = _config.audit_recurrence_horizon_days()
            if fingerprint and patterns_available:
                hit = by_fingerprint.get(fingerprint)
                # Pattern exists but has no last_ts -> still active (recurred)
                if hit is not None and not (hit.get("last_ts") or 0) > created:
                    recurred = True if hit.get("last_ts") is None else False
                else:
                    recurred = bool(hit and (hit.get("last_ts") or 0) > created)

            # An empty observation window (the pattern table carries no
            # post-edit rows at all, e.g. a restored or freshly rebuilt
            # state.db) is not evidence of either recurrence or silence.
            # Folding it into "unclear" hid WHY the verdict was unavailable;
            # the operator cannot distinguish "too fresh to judge" from
            # "the window itself is empty". Only matters when the edit's own
            # fingerprint is the thing that could not be checked: without a
            # fingerprint recurrence was never computable, so the empty
            # window changes nothing for that row. Recurrence evidence still
            # wins when the fingerprint DID reappear in some other window.
            window_empty = patterns_available and not current_patterns
            if window_empty:
                # The False computed above came from an ABSENT table, not from
                # a post-edit window that stayed quiet: an empty scan has no
                # rows for any fingerprint, so "absent" means nothing. Treat
                # recurrence as unmeasured, not as observed-silence.
                recurred = None
            if window_empty and recurred is None and bool(fingerprint):
                verdict = "no recurrence window"
            elif recurred is True:
                verdict = "did not help"
            elif uses == 0 and age_days >= 14 and usage_scope == "since_exact":
                # since_approx cannot prove unused or working: the DB fallback
                # detects usage by pattern-matching message content, which can
                # miss real uses and over-count incidental mentions.  Reporting
                # "unused" on an approximate zero would mislead the operator into
                # deleting a skill that is actually in use.
                verdict = "unused"
            elif (
                uses
                and uses > 0
                and usage_scope == "since_exact"
                and (
                    recurred is False
                    # "No recurrence" only counts as evidence of success after
                    # the measured quiet-gap horizon; before that, silence is
                    # indistinguishable from a pause (median gap is minutes,
                    # p95 is 2.17 days on the reference journal). A skill used
                    # without its fingerprint returning before the horizon is
                    # NOT yet "working" — it is too early.
                    or (
                        not fingerprint
                        and age_days >= horizon_days
                    )
                )
            ):
                verdict = "working"
            else:
                verdict = "too early" if age_days < 14 else "unclear"

        if version >= 3 and verdict == "unclear":
            verdict = "churning"

        # A skill can be edited or removed by something other than refine --
        # Hermes's own background review writes to the same skills. Once the
        # current target differs from what refine last applied, *every*
        # effectiveness verdict is unreliable: "unused" and "churning" are no
        # safer to act on than "working". Read-only: never reconcile, recreate,
        # revert, or re-apply the target here.
        externally_modified = False
        attribution_unknown = False
        external_change = ""
        if meta.get("kind", "skill") == "skill" and outcome == "applied":
            intended_digest = intended_skill_digests.get(name)
            if intended_digest:
                current_baseline = (
                    journal.skill_baseline(name)
                    if skill_baselines is None
                    else skill_baselines.get(name)
                )
                if current_baseline is None:
                    attribution_unknown = True
                    verdict = "unreliable — target state unavailable"
                elif current_baseline.get("exists") is False:
                    external_change = "removed"
                elif (
                    current_baseline.get("exists") is True
                    and current_baseline.get("sha256")
                ):
                    if current_baseline["sha256"] != intended_digest:
                        external_change = "modified"
                else:
                    attribution_unknown = True
                    verdict = "unreliable — target state unavailable"
                if external_change:
                    externally_modified = True
                    verdict = f"unreliable — externally {external_change}"
            else:
                attribution_unknown = True
                verdict = "unreliable — intended state unknown"

        # Memory rows get the same treatment with the method's own limit: the
        # host has no usage counter for memory entries (counting a name in
        # conversations proves nothing), so the only checkable fact is whether
        # the exact content refine appended is still present. Exact-content
        # membership cannot tell an EDIT from a REMOVAL -- both make the
        # string disappear -- so the row says "no longer present as applied"
        # rather than guessing which. Read-only, like the skill check.
        if meta.get("kind", "skill") == "memory" and outcome == "applied":
            intended_content = intended_memory_contents.get(name)
            if intended_content:
                baseline = (
                    memory_baselines.get(f"memory:{name}")
                    if memory_baselines is not None
                    else journal.memory_baseline("memory", intended_content)
                )
                if baseline is None:
                    attribution_unknown = True
                    verdict = "unreliable — target state unavailable"
                elif baseline.get("present") is False:
                    # Honest naming: edit and removal are indistinguishable
                    # through exact membership, so the state is named for what
                    # is KNOWN (no longer present as applied), not for a
                    # guessed cause.
                    externally_modified = True
                    verdict = "unreliable — no longer present as applied"
            else:
                attribution_unknown = True
                verdict = "unreliable — intended state unknown"

        rows.append({
            "name": name,
            "kind": meta.get("kind", "skill"),
            "age_days": age_days,
            "version": version,
            "updated_ts": updated_ts,
            "uses": uses,
            "usage_scope": usage_scope,
            "pattern_recurred": recurred,
            "verdict": verdict,
            "externally_modified": externally_modified,
            "attribution_unknown": attribution_unknown,
            "journal_id": meta.get("journal_id", ""),
            "outcome": outcome,
            "reported_model": (
                scrub_text(str(meta.get("reported_model", "")))[:60]
                if meta.get("reported_model")
                else ""
            ),
            "expected_outcome": (
                scrub_text(meta["expected_outcome"]).strip()
                if isinstance(meta.get("expected_outcome"), str)
                else ""
            ),
        })
    return rows


def format_audit(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No refine-created skills recorded yet."
    lines = [f"Refine-created entries ({len(rows)}):", ""]
    lines.append(
        f"  {'name':<28} {'age':>5}  {'ver':>3}  {'uses':>7}  {'recurred':>8}  verdict"
    )
    for row in rows:
        scope = row.get("usage_scope")
        if row["uses"] is None:
            uses = "?"
        elif scope == "all_time":
            uses = f"all:{row['uses']}"
        elif scope == "since_approx":
            uses = f"~{row['uses']}"
        else:
            uses = str(row["uses"])
        recurred = {True: "yes", False: "no", None: "—"}[row["pattern_recurred"]]
        kind = str(row.get("kind", "skill") or "skill")
        name = str(row.get("name", ""))
        display_name = name if kind == "skill" else f"{kind}:{name}"
        lines.append(
            f"  {display_name[:28]:<28} {str(row['age_days']) + 'd':>5}  "
            f"{'v' + str(row.get('version', 1)):>3}  {uses:>7}  "
            f"{recurred:>8}  {row['verdict']}"
        )
        expected_outcome = str(row.get("expected_outcome", "") or "—")
        lines.append(f"      expects: {expected_outcome[:57]}")
        reported_model = str(row.get("reported_model", "") or "")
        if reported_model:
            lines.append(f"      model: {reported_model[:40]}")
        if row.get("model_substituted"):
            lines.append(
                "      ⚠ model substituted: this entry was produced by a model "
                "different from the configured target; its verdict is not "
                "trustworthy"
            )
        if row.get("externally_modified"):
            lines.append(
                "      ⚠ modified or removed since refine's edit by something else "
                "(e.g. Hermes background review) — verdict is not reliable"
            )
        elif row.get("attribution_unknown"):
            lines.append(
                "      ⚠ current skill state could not be inspected — "
                "effectiveness and removal conclusions are suppressed"
            )

    candidates = [
        row for row in rows
        if row["verdict"] in ("unused", "did not help")
        and not row.get("externally_modified")
        and not row.get("attribution_unknown")
    ]
    if candidates:
        lines.extend(["", "Candidates for removal:"])
        for row in candidates:
            kind = str(row.get("kind", "skill") or "skill")
            name = str(row.get("name", ""))
            display_name = name if kind == "skill" else f"{kind}:{name}"
            lines.append(
                f"  {display_name} — /refine rollback {row['journal_id']}"
            )
        lines.extend(["", "Nothing was deleted. Run the command yourself if you agree."])
    lines.extend([
        "",
        "Use labels: plain = timestamped host count, ~ = trajectory estimate, all: = host all-time aggregate.",
        "All-time aggregates are not used to claim post-edit usage.",
    ])
    return "\n".join(lines)
