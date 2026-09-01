"""Durable append-only journal, mutation lock, approvals, and rollback."""

import hashlib
import importlib
import json
import logging
import math
import os
import re
import socket
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

try:
    from .config import journal_dir, max_edits_per_day
    from .sanitization import LINE_BREAK_CHARS, sanitize, scrub_text
except ImportError:
    from config import journal_dir, max_edits_per_day  # noqa: F811
    from sanitization import LINE_BREAK_CHARS, sanitize, scrub_text  # noqa: F811

logger = logging.getLogger(__name__)

_BACKUPS_DIR_NAME = "backups"
_JOURNAL_FILE_NAME = "refine_journal.jsonl"
_PROMPT_NOTES_FILE_NAME = "prompt_notes.json"
_MODEL_OVERRIDE_FILE_NAME = "model_override.json"
_LOCK_FILE_NAME = ".mutation.lock"
_ATOMIC_TEMP_PREFIX = ".refine-atomic-"
_ATOMIC_TEMP_SUFFIX = ".tmp"
_RECOVERY_BACKUP_PREFIX = "refine-"
_RECOVERY_BACKUP_RE = re.compile(
    rf"^{re.escape(_RECOVERY_BACKUP_PREFIX)}(?P<pid>[0-9]+)-"
    rf"[0-9a-f]{{32}}_skill_.+\.bak$"
)
_THREAD_LOCK = threading.RLock()
_LOCK_STATE = threading.local()
# If Windows refuses an owned unlink after the retry budget, remember the exact
# token. A later acquisition in this same process may then retry removing only
# its own orphan instead of treating the live PID as a foreign lock forever.
_ORPHANED_LOCK_TOKENS: Dict[str, str] = {}
_MIGRATION_INCOMPLETE = ".migration_incomplete"
_MIGRATION_STATUS: Dict[str, Any] = {
    "outcome": "not_checked",
    "source": "",
    "destination": "",
    "active_dir": "",
    "rename_warning": "",
    "error": "",
}

# Owned here rather than in the command layer, because this module owns the
# store: the same rule then applies to the command, to a hand-edited file, and
# to any future writer, instead of once per call site.
#
# Two shapes, not one. In ``/refine model <provider>/<model>`` the slash is the
# separator, so each half must not contain one. A model *id*, however, is very
# often namespaced — ``deepseek/deepseek-chat``, ``anthropic/claude-3.5-sonnet``
# — so the id rule has to allow it. Applying the token rule to a configured
# ``llm.model`` would silently discard exactly the ids people pin.
_MODEL_TOKEN_CHARS = r"[A-Za-z0-9._:\-]{1,120}"
_MODEL_TOKEN = re.compile(rf"^{_MODEL_TOKEN_CHARS}$")
_MODEL_ID = re.compile(rf"^{_MODEL_TOKEN_CHARS}(?:/{_MODEL_TOKEN_CHARS})*$")

# A duplicate journal id is a state transition, not a replacement record. The
# loader and writer share this table so hand-edited/cross-process records cannot
# bypass checks that finalize() applies to normal writes.
_JOURNAL_TRANSITIONS = {
    "prepared": {"applied", "error", "pending_approval", "cleanup_prepared"},
    "pending_approval": {"applied", "rejected"},
    "applied": {"rollback_prepared", "cleanup_prepared"},
    "cleanup_prepared": {"cleanup_resolved"},
    "rollback_prepared": {"pending_rollback", "rolled_back", "applied"},
    "pending_rollback": {"rolled_back", "applied"},
}
_JOURNAL_IMMUTABLE_FIELDS = (
    "id", "ts", "trigger", "reason", "session_id", "proposal",
    "backup_path", "snapshot", "group", "llm_meta",
)
_CONSUMED_EDIT_OUTCOMES = frozenset({
    "applied", "pending_approval", "prepared", "cleanup_prepared",
    "cleanup_resolved", "rollback_prepared", "pending_rollback",
})
# How long a ``prepared`` record may sit before reconciliation treats it as the
# remains of a dead pass rather than a mutation in flight. Generous on purpose:
# the alternative to waiting is declaring a live edit failed, and only ``ts`` and
# the target state distinguish the two. ``prepared`` counts against the daily
# budget, so a record that never resolves costs one of three edits until this
# elapses.
_ABANDONED_PREPARED_SECONDS = 900.0


def prompt_note_content_is_structurally_safe(content: Any) -> bool:
    """Reject markup/control characters that can restructure future context.

    ``\\n`` is the one exempt line break, because a note may hold two policy
    lines and the injection renderer keeps them inside their bullet with
    ``content.replace("\\n", "\\n  ")``. Any other codepoint that ends a line
    (LINE_BREAK_CHARS -- U+2028 is category Zl and U+2029 is Zp, so neither is
    caught by the control-category test below) would render unindented, outside
    the record the bullet exists to delimit. Refusing the class here keeps that
    ``\\n``-only indent complete, and the injection path re-validates, so a note
    already in the store is refused too.
    """
    if not isinstance(content, str) or "<" in content or ">" in content:
        return False
    for character in content:
        if character == "\n":
            continue
        if (
            character in LINE_BREAK_CHARS
            or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}
        ):
            return False
    return True


def _journal_transition_error(
    previous: Dict[str, Any], current: Dict[str, Any]
) -> Optional[str]:
    """Return why a duplicate-id state is forged or an illegal transition."""
    before = str(previous.get("outcome", ""))
    after = str(current.get("outcome", ""))
    if after not in _JOURNAL_TRANSITIONS.get(before, set()):
        return f"illegal journal transition {before!r} -> {after!r}"
    if after in {"cleanup_prepared", "cleanup_resolved"}:
        cleanup_session = normalize_prompt_note_session_id(
            current.get("session_id", "")
        )
        if not cleanup_session or not _prompt_cleanup_identity_matches(
            current, cleanup_session
        ):
            return "prompt cleanup transition requires exact session-note ownership"
    for field in _JOURNAL_IMMUTABLE_FIELDS:
        if previous.get(field) != current.get(field):
            return f"journal transition changed immutable field {field!r}"

    previous_recovery = previous.get("recovery", {})
    current_recovery = current.get("recovery", {})
    if not isinstance(previous_recovery, dict) or not isinstance(current_recovery, dict):
        return "journal recovery metadata must be an object"
    expected_recovery = dict(previous_recovery)
    previous_pending_id = previous.get("pending_id")
    current_pending_id = current.get("pending_id")
    pending_id = current_pending_id
    if after in {"pending_approval", "pending_rollback"}:
        if not isinstance(pending_id, str) or not pending_id:
            return "pending journal state requires a pending_id"
        # Forward approval and rollback approval are separate phases; the latter
        # legitimately replaces the earlier host pending id.
        expected_recovery["pending_id"] = pending_id
    elif current_pending_id != previous_pending_id:
        return "non-pending journal transition changed pending_id"
    if current_recovery != expected_recovery:
        return "journal transition changed immutable recovery metadata"

    finalized_ts = current.get("finalized_ts")
    if (
        not isinstance(finalized_ts, (int, float))
        or isinstance(finalized_ts, bool)
        or not math.isfinite(float(finalized_ts))
    ):
        return "journal transition requires a finite finalized_ts"
    return None


def _validate_journal_transition(
    previous: Dict[str, Any], current: Dict[str, Any]
) -> None:
    error = _journal_transition_error(previous, current)
    if error:
        raise ValueError(error)


def valid_model_identifier(value: str) -> bool:
    """Whether text is one slash-free provider/model token, not free-form prose.

    Requires at least one alphanumeric character: ``.`` and ``---`` match the
    character class but name no model, and pinning one would turn every later
    pass into a host-side model error.
    """
    return bool(_MODEL_TOKEN.fullmatch(value)) and any(
        char.isalnum() for char in value
    )


def valid_model_id(value: str) -> bool:
    """Whether text is a model id, allowing the common namespaced form."""
    return bool(_MODEL_ID.fullmatch(value)) and any(
        char.isalnum() for char in value
    )


def model_override_field_problem(value: str, *, allow_namespace: bool = False) -> str:
    """Say why a provider/model value is unusable, or "" when it is fine.

    Set ``allow_namespace`` for a model id, which may be ``vendor/name``; leave it
    off for a value that came from one side of the command's ``provider/model``
    split, where a slash cannot appear.

    Returns a reason rather than a bool so the refusal can name which rule failed.
    "Refusing an unsafe value" reads as a credential accusation for a name that
    merely had the wrong shape, and vice versa — and the caller must be able to
    explain the refusal without echoing the value, which may be a pasted secret.

    Public because the Hermes config writes the same field. If the rule lived on
    the command path only, the identical value would be refused from
    ``/refine model`` and accepted from ``config.yaml``.
    """
    accepted = valid_model_id(value) if allow_namespace else valid_model_identifier(value)
    if not accepted:
        return (
            "it is not a model identifier (letters, digits, '.', '_', ':', '-'"
            + (", '/'" if allow_namespace else "")
            + ", at least one alphanumeric)"
        )
    # The check that matters most. ``ghp_`` + 36 characters is a valid identifier,
    # so shape alone would persist a pasted token verbatim while the command echo
    # reported it redacted — telling the user a value was protected when it
    # was not. It can also fire on a legitimate name such as
    # ``my-token-model:latest``; refusing that is the safe side of the trade, and
    # the message says which rule rejected it.
    if scrub_text(value) != value:
        return "it matches a credential pattern, so it is refused rather than stored"
    return ""


def model_override_field_is_safe(value: str, *, allow_namespace: bool = False) -> bool:
    """Whether a provider/model value may be stored and sent to the host."""
    return not model_override_field_problem(value, allow_namespace=allow_namespace)


def ensure_dirs() -> Path:
    directory = journal_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / _BACKUPS_DIR_NAME).mkdir(exist_ok=True)
    return directory


def journal_path() -> Path:
    return ensure_dirs() / _JOURNAL_FILE_NAME


def journal_read_path() -> Path:
    """Journal location for readers, without creating anything.

    Readers must not materialize a mistyped ``journal_dir``: a diagnostic that
    creates the directory it is meant to inspect destroys its own evidence.
    """
    return journal_dir() / _JOURNAL_FILE_NAME


def backups_dir() -> Path:
    return ensure_dirs() / _BACKUPS_DIR_NAME


def prompt_notes_path() -> Path:
    """Return the plugin-owned prompt-note store; never a host memory path."""
    return ensure_dirs() / _PROMPT_NOTES_FILE_NAME


def prompt_notes_read_path() -> Path:
    """Prompt-note store for readers, without creating anything."""
    return journal_dir() / _PROMPT_NOTES_FILE_NAME


def model_override_read_path() -> Path:
    """Model override store for readers, without creating anything."""
    return journal_dir() / _MODEL_OVERRIDE_FILE_NAME


def _read_model_override_bytes() -> Optional[bytes]:
    """Return the raw store, or None when there is genuinely no override.

    A missing file is the common case and must stay on the fast path, so it is
    reported rather than raised: only a *failed* open is worth retrying.

    ``FileNotFoundError`` is absence too, not a failure. It is caught explicitly
    because the file can vanish between the check and the open — a concurrent
    ``/refine model auto`` does exactly that — and retrying it would spend the
    whole budget and then report "could not be read" about a file the user had
    just deliberately deleted.
    """
    path = model_override_read_path()
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except FileNotFoundError:
        return None


def read_model_override_state() -> "tuple[Optional[Dict[str, str]], str]":
    """Return the override and why it is, or is not, in force.

    States: ``absent``, ``ok``, ``rejected`` (present but unusable) and
    ``unreadable`` (present but could not be read on this attempt — on Windows a
    concurrent write can deny a read for a moment).

    The last two are kept apart from ``absent`` deliberately. Collapsing them
    would leave a file that pins one model while every diagnostic reports a
    different one, with a log line as the only trace — the invisible failure this
    project treats as worse than an error.

    Never raises: this runs while a proposal call is being assembled, and an
    exception here would surface as a generic LLM failure rather than as a
    problem with the override store.
    """
    # Bytes, not text: decoding here would raise UnicodeDecodeError, which is not
    # an OSError, so a store hand-edited in a non-UTF-8 codepage would escape this
    # function into the proposal call and end every pass as an ordinary no_op.
    # Decoding below keeps that input in the "rejected" state it belongs to.
    try:
        # Only OSError is retried — a sharing denial is the transient case, and it
        # is measured, not assumed: under a concurrent atomic replace this open is
        # denied for a moment on Windows a few times per few hundred reads. A
        # reader that gave up would report "no override" and send the call to a
        # different model than the one the user pinned.
        raw = _retry_on_contention(
            _read_model_override_bytes, _READ_RETRY_BUDGET_SECONDS, OSError
        )
    except Exception as exc:
        # Broad on purpose. "Never raises" has to hold for every exception type,
        # not just the one expected here: anything escaping would be assembled
        # into a proposal call and journaled as an ordinary no_op with
        # success=true, which is the outcome this function exists to prevent.
        logger.warning("Cannot read the model override: %s", scrub_text(str(exc)))
        return None, "unreadable"
    if raw is None:
        return None, "absent"
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "rejected"
    if not isinstance(data, dict):
        return None, "rejected"
    model = str(data.get("model", "") or "").strip()
    provider = str(data.get("provider", "") or "").strip()
    if not model and not provider:
        return None, "rejected"
    # Validate on the way out as well as on the way in. A hand-edited, partially
    # written or externally produced store must not inject arbitrary strings into
    # the host's LLM call arguments.
    if (provider and not model_override_field_is_safe(provider)) or (
        model and not model_override_field_is_safe(model, allow_namespace=True)
    ):
        logger.warning("Ignoring an unusable model override on disk")
        return None, "rejected"
    return {"model": model, "provider": provider}, "ok"


def read_model_override() -> Optional[Dict[str, str]]:
    """Return the user's command-set model override, or None when not in force."""
    return read_model_override_state()[0]


def write_model_override(provider: str, model: str) -> None:
    """Persist the model override atomically, refusing anything unsafe.

    Mirrors ``_write_prompt_notes``: the store refuses unsafe content instead of
    trusting its caller to have checked. Raises ``ValueError`` so the command
    layer reports a refusal rather than reporting success over a rejected write.
    """
    for name, value, namespaced in (
        ("provider", provider, False),
        ("model", model, True),
    ):
        problem = (
            model_override_field_problem(value, allow_namespace=namespaced)
            if value
            else ""
        )
        if problem:
            # Names the rule but never the value: the value may be a pasted
            # credential, and this message is echoed back into the conversation.
            raise ValueError(f"Refusing to store that {name} because {problem}")
    if not provider and not model:
        raise ValueError("Refusing to store an empty model override")
    payload = json.dumps(
        {"provider": provider, "model": model, "set_ts": time.time()},
        ensure_ascii=False,
    )
    with mutation_lock():
        _atomic_write_text(ensure_dirs() / _MODEL_OVERRIDE_FILE_NAME, payload)


def clear_model_override() -> str:
    """Remove the model override file, reporting what actually happened.

    Returns ``removed``, ``absent`` when there was nothing to remove, or
    ``failed`` when the file survives — on Windows an unlink can fail with
    ``PermissionError`` while another session thread holds it open, so it gets the
    same bounded retry as the read and the write. Confirming "removed" in either
    of the last two cases would report an action that did not take place.

    The command participates in the same generation lock as journal writes and
    migration, so a successful pin cannot be lost (or a cleared pin resurrected)
    while runtime data is published. The answer comes from the unlink itself
    rather than from a preceding existence check.
    """
    with mutation_lock():
        path = model_override_read_path()
        try:
            _retry_on_contention(path.unlink, _UNLINK_RETRY_BUDGET_SECONDS)
            return "removed"
        except FileNotFoundError:
            return "absent"
        except Exception as exc:
            logger.warning("Cannot remove the model override: %s", scrub_text(str(exc)))
            try:
                return "failed" if path.exists() else "removed"
            except OSError:
                return "failed"


# ── legacy journal directory migration ─────────────────────────────────────

_MIGRATION_MARKER = ".migrated_from"
_MIGRATION_FILES = [
    _JOURNAL_FILE_NAME,
    "skill_stats.json",
    _PROMPT_NOTES_FILE_NAME,
    _MODEL_OVERRIDE_FILE_NAME,
]
_MIGRATION_DIRS = [_BACKUPS_DIR_NAME]


def migration_status() -> Dict[str, Any]:
    """Return the last process-local migration decision for status reporting."""
    return sanitize(dict(_MIGRATION_STATUS))


def _set_migration_status(
    outcome: str,
    *,
    source: Path,
    destination: Path,
    active_dir: Path,
    rename_warning: str = "",
    error: str = "",
) -> str:
    _MIGRATION_STATUS.update({
        "outcome": outcome,
        "source": scrub_text(str(source)),
        "destination": scrub_text(str(destination)),
        "active_dir": scrub_text(str(active_dir)),
        "rename_warning": scrub_text(rename_warning),
        "error": scrub_text(error),
    })
    return outcome


def _mutation_lock_path(directory: Path) -> Path:
    """Return a stable sibling lock path that survives directory renames."""
    directory.parent.mkdir(parents=True, exist_ok=True)
    return directory.parent / f".{directory.name}{_LOCK_FILE_NAME}"


def _new_lock_lease() -> socket.socket:
    """Bind one non-inheritable loopback lease released automatically on death."""
    lease = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            lease.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        lease.set_inheritable(False)
        lease.bind(("127.0.0.1", 0))
        lease.listen(1)
        return lease
    except Exception:
        lease.close()
        raise


def _lock_payload(token: str, lease: socket.socket) -> str:
    return json.dumps({
        "pid": os.getpid(),
        "created": time.time(),
        "token": token,
        "lease_port": int(lease.getsockname()[1]),
    })


def _publish_lock(path: Path, payload: str) -> None:
    """Atomically claim ``path`` with a complete, already-fsynced record."""
    claim = path.parent / (
        f"{path.name}.refine-claim-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link is create-if-absent on every supported platform: unlike
        # rename it never replaces another owner's canonical lock on POSIX.
        os.link(claim, path)
    finally:
        try:
            _retry_on_contention(
                claim.unlink, _UNLINK_RETRY_BUDGET_SECONDS, OSError
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Publication may already have succeeded. A claim cleanup failure
            # must neither mask an earlier link error nor invalidate ownership
            # of the complete canonical lock; dead-owner cleanup can retry it.
            logger.warning(
                "Cannot remove Refine lock claim file: %s", scrub_text(str(exc))
            )


def _cleanup_lock_claims(lock_path: Path) -> None:
    """Remove only strict dead-process claim temps after canonical acquisition."""
    prefix = f"{lock_path.name}.refine-claim-"
    pattern = re.compile(
        rf"^{re.escape(prefix)}(?P<pid>[0-9]+)-[0-9a-f]{{32}}\.tmp$"
    )
    try:
        candidates = list(lock_path.parent.iterdir())
    except OSError:
        return
    for candidate in candidates:
        match = pattern.fullmatch(candidate.name)
        if not match or _pid_is_alive(int(match.group("pid"))):
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            candidate.unlink()
        except OSError:
            continue


@contextmanager
def _migration_lock(source: Path, timeout: float = 30.0) -> Iterator[None]:
    """Serialize migration with writers still using the legacy store."""
    lock_path = _mutation_lock_path(source)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + timeout
    lease: Optional[socket.socket] = None
    with _THREAD_LOCK:
        try:
            _clear_owned_orphan(lock_path)
            while True:
                # Recover before allocating our own ephemeral port. Otherwise
                # the OS could hand us the dead owner's port and make our own
                # socket look like proof that the stale owner is still alive.
                _try_clear_stale_lock(lock_path)
                candidate = _new_lock_lease()
                payload = _lock_payload(token, candidate)
                try:
                    _publish_lock(lock_path, payload)
                except FileExistsError:
                    candidate.close()
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for refine migration lock: {lock_path}"
                        )
                    time.sleep(0.05)
                    continue
                lease = candidate
                break
            _cleanup_lock_claims(lock_path)
            try:
                yield
            finally:
                try:
                    current = json.loads(lock_path.read_text(encoding="utf-8"))
                    if current.get("token") == token:
                        _retry_on_contention(
                            lock_path.unlink, _UNLINK_RETRY_BUDGET_SECONDS, OSError
                        )
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    _ORPHANED_LOCK_TOKENS[str(lock_path)] = token
                    logger.error(
                        "Could not release refine migration lock: %s",
                        scrub_text(str(exc)),
                    )
        finally:
            if lease is not None:
                lease.close()


def migrate_legacy_journal_dir(
    *,
    _new_dir: "Optional[Path]" = None,
    _legacy_dir: "Optional[Path]" = None,
) -> str:
    """Migrate legacy runtime data as one recoverable store generation.

    Data is copied to a sibling staging directory first. Publication is marked
    incomplete until every artifact is in place, so another process can safely
    retry after interruption. Any failure keeps the intact legacy directory as
    the active store for this process; a partial destination is never read.
    """
    try:
        from . import config as _cfg
    except ImportError:
        import config as _cfg  # type: ignore

    entry = _cfg._get_refine_entry()
    configured = entry.get("journal_dir")
    new_dir = Path(_new_dir if _new_dir is not None else _cfg.hermes_home() / "refine")
    legacy = Path(
        _legacy_dir if _legacy_dir is not None else _cfg.legacy_journal_dir()
    )
    if isinstance(configured, str) and configured.strip():
        active = Path(configured)
        _cfg._set_runtime_journal_dir(None)
        return _set_migration_status(
            "user_configured",
            source=legacy,
            destination=new_dir,
            active_dir=active,
        )

    marker = new_dir / _MIGRATION_MARKER
    incomplete = new_dir / _MIGRATION_INCOMPLETE

    def _legacy_has_data() -> bool:
        return legacy.is_dir() and any(
            (legacy / name).exists()
            for name in _MIGRATION_FILES + _MIGRATION_DIRS
        )

    def _destination_has_data() -> bool:
        return new_dir.is_dir() and any(
            (new_dir / name).exists()
            for name in _MIGRATION_FILES + _MIGRATION_DIRS
        )

    if marker.is_file():
        _cfg._set_runtime_journal_dir(None)
        return _set_migration_status(
            "not_needed", source=legacy, destination=new_dir, active_dir=new_dir
        )
    if not _legacy_has_data():
        active = legacy if incomplete.is_file() and legacy.is_dir() else new_dir
        _cfg._set_runtime_journal_dir(
            active if active == legacy else None,
            commit_marker=marker if active == legacy else None,
        )
        outcome = "failed" if incomplete.is_file() else "not_needed"
        return _set_migration_status(
            outcome,
            source=legacy,
            destination=new_dir,
            active_dir=active,
            error=(
                "An incomplete destination exists but the legacy source is unavailable."
                if outcome == "failed"
                else ""
            ),
        )

    import shutil as _shutil

    try:
        with _migration_lock(legacy):
            if marker.is_file():
                _cfg._set_runtime_journal_dir(None)
                return _set_migration_status(
                    "not_needed",
                    source=legacy,
                    destination=new_dir,
                    active_dir=new_dir,
                )
            if not _legacy_has_data():
                _cfg._set_runtime_journal_dir(None)
                return _set_migration_status(
                    "not_needed",
                    source=legacy,
                    destination=new_dir,
                    active_dir=new_dir,
                )
            if _destination_has_data() and not incomplete.is_file():
                _cfg._set_runtime_journal_dir(None)
                return _set_migration_status(
                    "not_needed",
                    source=legacy,
                    destination=new_dir,
                    active_dir=new_dir,
                )

            stage = Path(tempfile.mkdtemp(
                prefix=f".{new_dir.name}.migrate-", dir=str(new_dir.parent)
            ))
            try:
                for name in _MIGRATION_FILES:
                    src = legacy / name
                    if src.is_file():
                        _shutil.copy2(str(src), str(stage / name))
                src_backups = legacy / _BACKUPS_DIR_NAME
                if src_backups.is_dir():
                    dst_backups = stage / _BACKUPS_DIR_NAME
                    dst_backups.mkdir()
                    for item in src_backups.iterdir():
                        if item.is_file():
                            _shutil.copy2(str(item), str(dst_backups / item.name))

                # Force staged bytes before exposing any of them as active data.
                for staged_file in stage.rglob("*"):
                    if staged_file.is_file():
                        with staged_file.open("rb+") as handle:
                            os.fsync(handle.fileno())

                new_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(
                    incomplete,
                    json.dumps({"from": str(legacy), "ts": time.time()}),
                )
                for name in _MIGRATION_FILES:
                    staged = stage / name
                    if staged.is_file():
                        os.replace(str(staged), str(new_dir / name))
                staged_backups = stage / _BACKUPS_DIR_NAME
                if staged_backups.is_dir():
                    destination_backups = new_dir / _BACKUPS_DIR_NAME
                    destination_backups.mkdir(exist_ok=True)
                    for item in staged_backups.iterdir():
                        if item.is_file():
                            os.replace(str(item), str(destination_backups / item.name))

                _atomic_write_text(
                    marker,
                    json.dumps({"from": str(legacy), "ts": time.time()}),
                )
                incomplete.unlink(missing_ok=True)
            finally:
                _shutil.rmtree(stage, ignore_errors=True)

            _cfg._set_runtime_journal_dir(None)
            rename_warning = ""
            ts_suffix = time.strftime("%Y%m%d-%H%M%S")
            renamed = legacy.parent / f"refine.migrated-{ts_suffix}"
            try:
                _retry_on_contention(
                    lambda: legacy.rename(renamed),
                    _WRITE_RETRY_BUDGET_SECONDS,
                    OSError,
                )
            except OSError as exc:
                rename_warning = scrub_text(str(exc))
                logger.warning(
                    "Legacy journal dir could not be renamed: %s", rename_warning
                )
            return _set_migration_status(
                "migrated",
                source=legacy,
                destination=new_dir,
                active_dir=new_dir,
                rename_warning=rename_warning,
            )
    except Exception as exc:
        safe_error = scrub_text(str(exc))
        logger.warning("Journal directory migration failed: %s", safe_error)
        # Legacy was never deleted and is the only complete generation.
        _cfg._set_runtime_journal_dir(legacy, commit_marker=marker)
        return _set_migration_status(
            "failed",
            source=legacy,
            destination=new_dir,
            active_dir=legacy,
            error=safe_error,
        )


def normalize_prompt_note_session_id(session_id: Any) -> str:
    """Accept only a stable, already-safe hook/session identifier."""
    raw = str(session_id).strip()
    safe = scrub_text(raw).strip()
    return safe if raw and raw == safe and len(safe) <= 64 else ""


def _normalize_prompt_note(note: Any) -> Optional[Dict[str, str]]:
    """Validate one plugin-owned note and canonicalize legacy notes as global."""
    if not isinstance(note, dict):
        return None
    note_id = note.get("id")
    content = note.get("content")
    scope = note.get("scope", "global")
    if (
        not isinstance(note_id, str)
        or len(note_id) != 12
        or any(char not in "0123456789abcdef" for char in note_id)
        or not isinstance(content, str)
        or not content.strip()
        or scrub_text(content) != content
        or not prompt_note_content_is_structurally_safe(content)
        or scope not in ("global", "session")
    ):
        return None
    normalized = {"id": note_id, "content": content, "scope": scope}
    if scope == "session":
        session_id = normalize_prompt_note_session_id(note.get("session_id", ""))
        if not session_id:
            return None
        normalized["session_id"] = session_id
    return normalized


def _load_prompt_notes() -> Optional[List[Dict[str, str]]]:
    """Return validated prompt notes, [] when absent, or None when unavailable."""
    # A reader must not create journal_dir: this runs on the pre_llm_call path
    # of every turn, and materializing a mistyped path there would erase the
    # very evidence /refine status exists to report.
    path = prompt_notes_read_path()
    if not path.exists():
        return []
    if not path.is_file():
        logger.warning("Prompt-note store is not a regular file")
        return None
    try:
        document = json.loads(
            _retry_on_contention(
                lambda: path.read_text(encoding="utf-8"),
                _READ_RETRY_BUDGET_SECONDS,
                OSError,
            )
        )
        raw_notes = document.get("notes") if isinstance(document, dict) else None
        if not isinstance(raw_notes, list):
            raise ValueError("notes must be a list")
        notes: List[Dict[str, str]] = []
        seen_ids = set()
        for raw_note in raw_notes:
            note = _normalize_prompt_note(raw_note)
            if note is None or note["id"] in seen_ids:
                raise ValueError("unsafe prompt note")
            seen_ids.add(note["id"])
            notes.append(note)
        return notes
    except Exception as exc:
        logger.warning("Cannot read prompt-note store: %s", scrub_text(str(exc)))
        return None


def load_prompt_notes() -> Optional[List[Dict[str, str]]]:
    """Read safe prompt notes. Callers hold a mutation lock when consistency matters."""
    return _load_prompt_notes()


def _write_prompt_notes(notes: List[Dict[str, str]]) -> None:
    """Atomically persist only validated, already-scrubbed note objects."""
    safe_notes = []
    seen_ids = set()
    for raw_note in notes:
        note = _normalize_prompt_note(raw_note)
        if note is None or note["id"] in seen_ids:
            raise ValueError("Refusing to write an unsafe prompt note")
        seen_ids.add(note["id"])
        safe_notes.append(note)
    _atomic_write_text(
        prompt_notes_path(),
        json.dumps({"notes": safe_notes}, ensure_ascii=False, separators=(",", ":")),
    )


def prompt_note_content_exists(content: str) -> Optional[bool]:
    """Return None for unavailable storage so callers fail closed before mutation."""
    with mutation_lock():
        notes = _load_prompt_notes()
        if notes is None:
            return None
        return any(note["content"] == content for note in notes)


def normalize_prompt_note_content(content: str) -> str:
    """Canonicalize a note once so journal proof and storage always agree."""
    return scrub_text(str(content)).strip()


def new_prompt_note(
    content: str, *, scope: str = "global", session_id: str = ""
) -> Optional[Dict[str, str]]:
    """Preflight storage and allocate a stable ID without mutating the store."""
    candidate: Dict[str, str] = {
        "id": uuid.uuid4().hex[:12],
        "content": normalize_prompt_note_content(content),
        "scope": scope,
    }
    if scope == "session":
        candidate["session_id"] = session_id
    note = _normalize_prompt_note(candidate)
    if note is None:
        return None
    with mutation_lock():
        if _load_prompt_notes() is None:
            return None
        return note


def add_prompt_note(note: Dict[str, str]) -> Dict[str, Any]:
    """Persist one note atomically; this is plugin-owned and needs no host approval."""
    safe_note = _normalize_prompt_note(note)
    if safe_note is None:
        return {"success": False, "error": "Prompt note is invalid"}
    with mutation_lock():
        notes = _load_prompt_notes()
        if notes is None:
            return {"success": False, "error": "Prompt-note store is unavailable"}
        if any(
            item["id"] == safe_note["id"] or item["content"] == safe_note["content"]
            for item in notes
        ):
            return {"success": False, "error": "Prompt note already exists"}
        try:
            _write_prompt_notes(notes + [safe_note])
        except Exception as exc:
            return {
                "success": False,
                "error": f"Cannot persist prompt note: {scrub_text(str(exc))}",
            }
        return {"success": True, "note_id": safe_note["id"]}


def _prompt_cleanup_identity_matches(
    entry: Dict[str, Any], session_id: str
) -> bool:
    """Prove that one journal record owns a note for this exact session."""
    proposal = entry.get("proposal", {})
    recovery = entry.get("recovery", {})
    note_id = recovery.get("note_id") if isinstance(recovery, dict) else None
    return bool(
        isinstance(proposal, dict)
        and proposal.get("kind") == "prompt"
        and proposal.get("action") == "create"
        and proposal.get("scope") == "session"
        and proposal.get("session_id") == session_id
        and entry.get("session_id") == session_id
        and recovery.get("type") == "prompt_note"
        and isinstance(note_id, str)
        and len(note_id) == 12
        and all(char in "0123456789abcdef" for char in note_id)
        and proposal.get("name") == note_id
        and proposal.get("note_id") == note_id
        and isinstance(proposal.get("content"), str)
        and bool(proposal.get("content"))
    )


def _prompt_cleanup_note_matches(
    entry: Dict[str, Any], note: Dict[str, str], session_id: str
) -> bool:
    """Bind cleanup intent to exact immutable journal and prompt-store data."""
    proposal = entry.get("proposal", {})
    recovery = entry.get("recovery", {})
    return bool(
        _prompt_cleanup_identity_matches(entry, session_id)
        and note.get("id") == recovery.get("note_id")
        and note.get("content") == proposal.get("content")
        and note.get("scope") == proposal.get("scope")
        and note.get("session_id") == proposal.get("session_id")
    )


def clear_session_prompt_notes(
    session_id: str,
    *,
    timeout: float = 30.0,
    mirror: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Durably expire exact notes owned by one ended/reset session.

    ``cleanup_prepared`` is fsynced before the prompt store changes and
    ``cleanup_resolved`` only after exact-id absence is proven. Both outcomes
    remain consumed edits: normal session expiry is not rollback evidence.
    Exact conflicts are retained without blocking independently proven siblings.
    ``None`` means the durable operation itself did not complete.

    ``removed``/``note_ids`` describe notes actually taken out of the store;
    ``journal_ids``/``entries`` the subset whose terminal transition is durable.

    ``mirror`` receives each returned entry while this call still holds the
    mutation lock, and callers of *this* function must not mirror afterwards:
    ``ledger.record_edit`` takes the same lock with its own 30s default, which on
    a host callback thread would reintroduce exactly the stall ``timeout`` exists
    to bound, and would leave a window where the journal says the note expired
    and the ledger still says it is live. The rule is about the short host
    timeout, so it does not extend to ``reconcile()``, whose caller mirrors after
    the lock on an ordinary refine thread that may wait the full default.
    """
    safe_session_id = normalize_prompt_note_session_id(session_id)
    if not safe_session_id:
        return None
    with mutation_lock(timeout=timeout):
        notes = _load_prompt_notes()
        if notes is None:
            return None
        try:
            entries_value = _load_entries()
        except Exception as exc:
            logger.warning(
                "Cannot clear session prompt notes because the journal is unreadable: %s",
                scrub_text(str(exc)),
            )
            return None

        session_notes = [
            note for note in notes
            if note.get("scope") == "session"
            and note.get("session_id") == safe_session_id
        ]
        by_note_id: Dict[str, List[Dict[str, Any]]] = {}
        cleanup_pending: List[Dict[str, Any]] = []
        rollback_pending: List[Dict[str, Any]] = []
        for entry in entries_value:
            if not _prompt_cleanup_identity_matches(entry, safe_session_id):
                continue
            note_id = str(entry.get("recovery", {}).get("note_id", ""))
            by_note_id.setdefault(note_id, []).append(entry)
            if entry.get("outcome") == "cleanup_prepared":
                cleanup_pending.append(entry)
            elif entry.get("outcome") == "rollback_prepared":
                rollback_pending.append(entry)

        selected_cleanup: List[tuple[Dict[str, str], Dict[str, Any]]] = []
        selected_rollback: List[tuple[Dict[str, str], Dict[str, Any]]] = []
        conflicts: List[str] = []
        for note in session_notes:
            matches = [
                entry for entry in by_note_id.get(note["id"], [])
                if entry.get("outcome") in {
                    "prepared", "applied", "cleanup_prepared", "rollback_prepared",
                }
                and _prompt_cleanup_note_matches(entry, note, safe_session_id)
            ]
            if len(matches) != 1:
                logger.warning(
                    "Retained session prompt note %s: exact journal ownership is not unique",
                    note["id"],
                )
                conflicts.append(note["id"])
                continue
            selected = (note, matches[0])
            if matches[0].get("outcome") == "rollback_prepared":
                selected_rollback.append(selected)
            else:
                selected_cleanup.append(selected)

        prepared: Dict[str, Dict[str, Any]] = {
            str(entry["id"]): entry for entry in cleanup_pending
        }
        rolling_back: Dict[str, Dict[str, Any]] = {
            str(entry["id"]): entry for entry in rollback_pending
        }
        pending_removal = selected_cleanup + selected_rollback
        # Read back from the store rather than assumed from the selection, so
        # this cannot report a note as expired that is in fact still present.
        removed: List[tuple[Dict[str, str], Dict[str, Any]]] = []
        resolved: List[Dict[str, Any]] = []
        store_written = False

        def cleanup_result(error: str = "") -> Dict[str, Any]:
            # Exactly one return path builds a result, so mirroring here sends
            # each resolved entry once.
            entries_out = [sanitize(entry) for entry in resolved]
            if mirror is not None:
                for entry in entries_out:
                    try:
                        mirror(entry)
                    except Exception as exc:
                        # The journal is the authority; a ledger mirror is a
                        # display convenience that ``_merge_journal_stats``
                        # rebuilds later. It must never undo a durable
                        # transition or fail the cleanup that already happened.
                        logger.warning(
                            "Cannot mirror prompt-note cleanup in ledger: %s",
                            scrub_text(str(exc)),
                        )
            return {
                "complete": not conflicts and not error,
                "removed": len(removed),
                "note_ids": [note["id"] for note, _entry in removed],
                "conflicts": conflicts,
                "error": scrub_text(error),
                "journal_ids": [str(entry["id"]) for entry in resolved],
                "entries": entries_out,
            }

        def incomplete(reason: str) -> Dict[str, Any]:
            """Abandon the pass with a named cause, keeping durable work.

            The cause travels in the result, not only in the log, because the
            caller turns ``error`` into the operator-visible auto event while a
            bare ``None`` collapses every distinct cause into one generic
            "did not complete". ``complete`` is already False and the entry list
            is empty when nothing resolved, so the failure contract is unchanged.
            The result may legitimately carry no durable work at all -- a rollback
            intent whose note was already gone reaches this without any store
            write -- so read ``journal_ids``/``removed`` rather than assuming it.
            """
            logger.warning("Cannot complete session prompt cleanup: %s", scrub_text(reason))
            return cleanup_result(reason)

        try:
            # JSONL has no multi-record transaction. A partial preparation is
            # still safe: every prepared id retains its note, and reconcile can
            # later finish only those exact witnessed entries.
            for _note, entry in selected_cleanup:
                entry_id = str(entry["id"])
                if entry.get("outcome") != "cleanup_prepared":
                    entry = finalize(entry_id, "cleanup_prepared")
                prepared[entry_id] = entry

            remove_ids = {note["id"] for note, _entry in pending_removal}
            if remove_ids:
                _write_prompt_notes([
                    note for note in notes if note["id"] not in remove_ids
                ])
                # True as of this instant; corrected below by reading the store
                # back. If the pass dies in between, reporting the write is more
                # accurate than reporting that nothing was removed.
                store_written = True
                removed = pending_removal

            current_notes = _load_prompt_notes()
            if current_notes is None:
                # The store write already landed, so this is not "nothing
                # happened": name it rather than reporting a generic failure.
                return incomplete(
                    "prompt note store became unreadable after the cleanup write"
                )
            current_by_id = {note["id"]: note for note in current_notes}
            removed = [
                item for item in pending_removal
                if item[0]["id"] not in current_by_id
            ]
            for outcome, pending in (
                ("cleanup_resolved", prepared),
                ("rolled_back", rolling_back),
            ):
                source_outcome = (
                    "cleanup_prepared"
                    if outcome == "cleanup_resolved"
                    else "rollback_prepared"
                )
                for entry_id, snapshot in pending.items():
                    note_id = str(snapshot.get("recovery", {}).get("note_id", ""))
                    current_note = current_by_id.get(note_id)
                    if current_note is not None:
                        # A changed/recreated note is not the exact target whose
                        # absence this durable intent is allowed to certify.
                        if not _prompt_cleanup_note_matches(
                            snapshot, current_note, safe_session_id
                        ):
                            if note_id not in conflicts:
                                conflicts.append(note_id)
                            continue
                        # The exact note survived its own removal, so absence
                        # cannot be certified.
                        return incomplete(
                            f"exact prompt note {note_id} survived the store write"
                        )
                    current = get_entry(entry_id)
                    if current and current.get("outcome") == source_outcome:
                        resolved.append(finalize(entry_id, outcome))
                    elif current and current.get("outcome") == outcome:
                        resolved.append(current)
                    else:
                        return incomplete(
                            f"journal entry {entry_id} is no longer {source_outcome}"
                        )
        except Exception as exc:
            safe_error = scrub_text(str(exc))
            logger.warning("Cannot clear session prompt notes: %s", safe_error)
            # A prior terminal transition in this batch is already durable and
            # reconciliation will not emit it again. Return those exact entries
            # so callers can mirror journal authority even though a later id
            # remains pending. Once the store write has landed the same applies
            # to the cause itself: notes are gone and the journal has not caught
            # up, which is not the same event as "there was nothing to clean up".
            if resolved or store_written:
                return cleanup_result(safe_error)
            return None

        return cleanup_result()


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is a POSIX existence probe but is not guaranteed
        # to be signal-free on Windows. Query a process handle instead.
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)  # query limited info
            if not handle:
                return ctypes.get_last_error() == 5  # access denied still means alive
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True  # fail closed: do not clear a lock on uncertainty
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _try_clear_stale_lock(path: Path) -> None:
    """Clear only a dead owner proved by its released loopback lease.

    PID/mtime checks cannot authorize a pathname unlink: another contender can
    replace the inspected file between the final check and ``unlink``. Every new
    lock therefore keeps an exclusive loopback port bound for its whole critical
    section. Only one contender can bind that same port after process death, and
    it revalidates the exact file before every Windows unlink retry. Legacy,
    partial, or malformed lock files have no authoritative lease and remain
    fail-closed for explicit operator recovery.
    """
    try:
        first_stat = path.stat()
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return
    lease_port = data.get("lease_port") if isinstance(data, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    if (
        isinstance(lease_port, bool)
        or not isinstance(lease_port, int)
        or not 1 <= lease_port <= 65535
        or not isinstance(token, str)
        or not token
    ):
        return

    recovery = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            recovery.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        recovery.set_inheritable(False)
        try:
            recovery.bind(("127.0.0.1", lease_port))
            recovery.listen(1)
        except OSError:
            return

        def unlink_if_unchanged() -> None:
            try:
                current_stat = path.stat()
                current_raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return
            if (
                current_raw != raw
                or current_stat.st_mtime_ns != first_stat.st_mtime_ns
                or current_stat.st_size != first_stat.st_size
            ):
                return
            path.unlink()

        _retry_on_contention(
            unlink_if_unchanged, _UNLINK_RETRY_BUDGET_SECONDS, OSError
        )
    except FileNotFoundError:
        pass
    finally:
        recovery.close()


@contextmanager
def _acquire_mutation_lock(*, wait: bool, timeout: float = 0.0) -> Iterator[bool]:
    """Acquire the re-entrant thread/process lock, optionally without waiting.

    ``timeout`` bounds the whole acquisition, in-process contention included.
    Bounding only the lock file would let a caller on a host callback thread wait
    forever behind another thread of the same process.
    """
    deadline = time.monotonic() + timeout
    if wait:
        acquired_thread = (
            _THREAD_LOCK.acquire(blocking=False)
            if timeout <= 0
            else _THREAD_LOCK.acquire(timeout=timeout)
        )
    else:
        acquired_thread = _THREAD_LOCK.acquire(blocking=False)
    if not acquired_thread:
        yield False
        return
    lease: Optional[socket.socket] = None
    try:
        depth = getattr(_LOCK_STATE, "depth", 0)
        if depth:
            _LOCK_STATE.depth = depth + 1
            try:
                yield True
            finally:
                _LOCK_STATE.depth -= 1
            return

        token = uuid.uuid4().hex

        def _release_owned_lock(path: Path) -> None:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if current.get("token") == token:
                    _retry_on_contention(
                        path.unlink, _UNLINK_RETRY_BUDGET_SECONDS, OSError
                    )
            except FileNotFoundError:
                pass
            except Exception as exc:
                _ORPHANED_LOCK_TOKENS[str(path)] = token
                logger.error(
                    "Could not release refine mutation lock (will retry on next acquisition): %s",
                    scrub_text(str(exc)),
                )

        # A failed migrator can switch from legacy to the committed destination
        # while it waits. Re-check the active generation after acquiring the
        # stable sibling lock and retry there before yielding to a writer.
        while True:
            locked_directory = ensure_dirs()
            lock_path = _mutation_lock_path(locked_directory)
            _clear_owned_orphan(lock_path)
            while True:
                # Never allocate a candidate lease before stale recovery: an
                # ephemeral-port reuse could otherwise make this process block
                # its own proof that the previous owner died.
                _try_clear_stale_lock(lock_path)
                candidate = _new_lock_lease()
                payload = _lock_payload(token, candidate)
                try:
                    _publish_lock(lock_path, payload)
                except FileExistsError:
                    candidate.close()
                    if not wait:
                        yield False
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for refine mutation lock: {lock_path}"
                        )
                    time.sleep(0.05)
                    continue
                lease = candidate
                break

            _cleanup_lock_claims(lock_path)
            if ensure_dirs() == locked_directory:
                break
            _release_owned_lock(lock_path)
            lease.close()
            lease = None

        _LOCK_STATE.depth = 1
        try:
            _cleanup_interrupted_artifacts(locked_directory)
            yield True
        finally:
            _LOCK_STATE.depth = 0
            _release_owned_lock(lock_path)
    finally:
        if lease is not None:
            lease.close()
        _THREAD_LOCK.release()


@contextmanager
def mutation_lock(timeout: float = 30.0) -> Iterator[None]:
    """Serialize refine mutations across threads and processes."""
    with _acquire_mutation_lock(wait=True, timeout=timeout) as acquired:
        if not acquired:  # Another thread of this process still owns the lock.
            raise TimeoutError("Timed out waiting for refine mutation lock")
        yield


@contextmanager
def try_mutation_lock() -> Iterator[bool]:
    """Attempt mutation serialization once, without queueing behind another owner."""
    with _acquire_mutation_lock(wait=False) as acquired:
        yield acquired


# ── durable file I/O ───────────────────────────────────────────────────────


# One base owns every store-contention budget, so they cannot drift apart. The
# write waits longest because it races a *reader*, which holds the file for a
# whole read; a read and an unlink race a single atomic replace, which is orders
# of magnitude shorter. The write budget is not smaller for a measured reason: a
# 0.1s write budget lost a two-thread stress race 10 times out of 12, this one
# lost none in 12. Every budget is spent only under actual contention, and their
# sum stays well inside the timeout host callbacks use for the mutation lock.
_CONTENTION_BUDGET_SECONDS = 0.5
_WRITE_RETRY_BUDGET_SECONDS = _CONTENTION_BUDGET_SECONDS
_READ_RETRY_BUDGET_SECONDS = _CONTENTION_BUDGET_SECONDS / 5
_UNLINK_RETRY_BUDGET_SECONDS = _CONTENTION_BUDGET_SECONDS / 5
_RETRY_MAX_DELAY = 0.05


def _retry_on_contention(operation, budget: float, errors=PermissionError):
    """Run a store operation, tolerating a momentary Windows sharing denial.

    On Windows a file operation fails while another handle on the target is open,
    and every store here is read lock-free from the per-turn hook path of a
    gateway that serves several channels at once. POSIX does not fail this way, so
    this is inert there.

    This does not paper over the race: nothing about the operation's atomicity
    changes, only its scheduling, and once the budget is spent the original error
    is raised unchanged rather than swallowed.
    """
    deadline = time.monotonic() + budget
    delay = 0.005
    while True:
        try:
            return operation()
        except errors:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(delay, _RETRY_MAX_DELAY))
            delay *= 2


def _clear_owned_orphan(path: Path) -> None:
    """Retry only a dead lease carrying the exact token this process owned."""
    key = str(path)
    token = _ORPHANED_LOCK_TOKENS.get(key)
    if not token:
        return
    try:
        current = json.loads(
            _retry_on_contention(
                lambda: path.read_text(encoding="utf-8"),
                _READ_RETRY_BUDGET_SECONDS,
                OSError,
            )
        )
        if current.get("token") != token:
            _ORPHANED_LOCK_TOKENS.pop(key, None)
            return
        # The previous context has closed its lease by now. Reuse the same
        # lease-authorized stale path as foreign recovery rather than performing
        # a token-check/path-unlink sequence with its own replacement race.
        _try_clear_stale_lock(path)
        try:
            remaining = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _ORPHANED_LOCK_TOKENS.pop(key, None)
            return
        if remaining.get("token") != token:
            _ORPHANED_LOCK_TOKENS.pop(key, None)
    except FileNotFoundError:
        _ORPHANED_LOCK_TOKENS.pop(key, None)
    except Exception as exc:
        logger.error("Could not recover owned refine lock: %s", scrub_text(str(exc)))


def _replace_with_retry(temp_name: str, path: Path) -> None:
    """Atomically replace the target, tolerating a concurrent reader on Windows.

    This sits under ``_atomic_write_text``, so it covers every store written that
    way — prompt notes and skill snapshots as well as the model override. That is
    deliberate: all of them are read lock-free from hook paths, so all of them can
    lose the same race, and fixing it once at the writer is better than three
    times at the call sites.
    """
    _retry_on_contention(
        lambda: os.replace(temp_name, path), _WRITE_RETRY_BUDGET_SECONDS
    )


def _cleanup_interrupted_artifacts(directory: Path) -> None:
    """Remove only unowned remnants of interrupted plugin writes.

    The caller has just acquired the cross-process mutation lock, so no live
    Refine mutation can own one of these files. Atomic staging files use an
    explicit plugin marker and are never recovery sources. Recovery backups are
    removed only when the readable journal has no entry referencing their exact
    basename; an unreadable journal retains every backup fail-closed.
    """
    roots = (directory, directory / _BACKUPS_DIR_NAME)
    for root in roots:
        try:
            candidates = list(root.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                "Cannot inspect Refine atomic staging files: %s",
                scrub_text(str(exc)),
            )
            continue
        for candidate in candidates:
            if not (
                candidate.name.startswith(_ATOMIC_TEMP_PREFIX)
                and candidate.name.endswith(_ATOMIC_TEMP_SUFFIX)
            ):
                continue
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                _retry_on_contention(
                    candidate.unlink, _UNLINK_RETRY_BUDGET_SECONDS, OSError
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "Cannot remove interrupted Refine atomic staging file: %s",
                    scrub_text(str(exc)),
                )

    entries_value, state = _load_entries_state()
    if state == "unreadable":
        return
    referenced = {
        Path(str(entry.get("backup_path", ""))).name
        for entry in entries_value
        if Path(str(entry.get("backup_path", ""))).name
    }
    backup_root = directory / _BACKUPS_DIR_NAME
    try:
        candidates = list(backup_root.iterdir())
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            "Cannot inspect Refine recovery backups: %s", scrub_text(str(exc))
        )
        return
    for candidate in candidates:
        match = _RECOVERY_BACKUP_RE.fullmatch(candidate.name)
        if not match or candidate.name in referenced:
            continue
        try:
            owner_pid = int(match.group("pid"))
        except (TypeError, ValueError):
            continue
        # A public caller may intentionally capture recovery and journal it in a
        # following call without one outer lock. Keep that narrow live-owner
        # window; a hard-killed owner is the only unreferenced copy reclaimed.
        if _pid_is_alive(owner_pid):
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            _retry_on_contention(
                candidate.unlink, _UNLINK_RETRY_BUDGET_SECONDS, OSError
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "Cannot remove unreferenced Refine recovery backup: %s",
                scrub_text(str(exc)),
            )


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace plugin-owned backup/stat files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=_ATOMIC_TEMP_PREFIX,
        suffix=_ATOMIC_TEMP_SUFFIX,
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _replay_entries(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Collapse physical JSONL records into validated latest logical states."""
    latest: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("journal contains an invalid JSON record") from exc
        if not isinstance(entry, dict):
            raise ValueError("journal record must be an object")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("journal record id must be a non-empty string")
        timestamp = entry.get("ts")
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp))
        ):
            raise ValueError("journal record timestamp must be finite numeric data")
        if not isinstance(entry.get("outcome"), str) or not entry["outcome"]:
            raise ValueError("journal record outcome must be a non-empty string")
        if not isinstance(entry.get("proposal"), dict):
            raise ValueError("journal record proposal must be an object")
        if entry_id not in latest:
            order.append(entry_id)
        else:
            _validate_journal_transition(latest[entry_id], entry)
        latest[entry_id] = entry
    return [latest[entry_id] for entry_id in order]


def _load_entries_state() -> "tuple[List[Dict[str, Any]], str]":
    """Return collapsed entries plus ``ok``, ``absent``, or ``unreadable``."""
    path = journal_read_path()

    def _read():
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            return _replay_entries(handle)

    try:
        entries_value = _retry_on_contention(_read, _READ_RETRY_BUDGET_SECONDS)
    except FileNotFoundError:
        return [], "absent"
    except Exception as exc:
        logger.error("Failed to read journal: %s", scrub_text(str(exc)))
        return [], "unreadable"
    return entries_value, "ok"


def _load_entries() -> List[Dict[str, Any]]:
    """Return journal entries, raising when the store is present but unreadable."""
    entries_value, state = _load_entries_state()
    if state == "unreadable":
        raise IOError("Journal unreadable")
    return entries_value


def _load_entries_safe() -> "tuple[List[Dict[str, Any]], str]":
    """Compatibility alias for callers that need the explicit read state."""
    return _load_entries_state()



def entries() -> List[Dict[str, Any]]:
    """Return the latest durable state of each logical journal record."""
    return _load_entries()


def recent_refinements(limit: int) -> List[Dict[str, Any]]:
    """Return capped create/patch outcomes in chronological order for model feedback."""
    try:
        capped_limit = max(0, int(limit))
    except (TypeError, ValueError):
        return []
    if not capped_limit:
        return []
    included_outcomes = {
        "applied", "pending_approval", "error", "rejected", "rolled_back",
        "cleanup_prepared", "cleanup_resolved", "rollback_prepared",
        "pending_rollback",
    }
    refinements: List[Dict[str, Any]] = []
    for entry in entries():
        proposal = entry.get("proposal", {})
        if not isinstance(proposal, dict):
            continue
        if (
            proposal.get("action") in ("create", "patch")
            and entry.get("outcome") in included_outcomes
        ):
            refinements.append(entry)
    return refinements[-capped_limit:]


def last_attempt_ts(trigger: Optional[str] = None) -> Optional[float]:
    """Return the most recent durable attempt timestamp, optionally by trigger."""
    latest: Optional[float] = None
    for entry in _load_entries():
        if trigger is not None and entry.get("trigger") != trigger:
            continue
        try:
            timestamp = float(entry.get("ts"))
        except (TypeError, ValueError):
            continue
        if latest is None or timestamp > latest:
            latest = timestamp
    return latest


def _append_entry(entry: Dict[str, Any]) -> None:
    """Append one fsynced JSON line without rewriting journal history."""
    safe_entry = sanitize(entry)
    record = json.dumps(safe_entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = record.encode("utf-8")
    with mutation_lock():
        path = journal_path()
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            separator = b""
            if size:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    # Isolate a corrupt/partial prior tail so this valid record
                    # remains independently loadable.
                    separator = b"\n"
            handle.seek(0, os.SEEK_END)
            handle.write(separator + encoded)
            handle.flush()
            os.fsync(handle.fileno())


def _new_entry(
    *,
    trigger: str,
    reason: str,
    session_id: str,
    proposal: Dict[str, Any],
    outcome: str,
    backup_path: str = "",
    error: str = "",
    recovery: Optional[Dict[str, Any]] = None,
    group: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    llm_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "trigger": trigger,
        "reason": reason,
        "session_id": str(session_id)[:64],
        "proposal": proposal,
        "outcome": outcome,
        "backup_path": backup_path,
        "recovery": recovery or {},
        "error": error,
    }
    if snapshot:
        entry["snapshot"] = snapshot
    if group:
        entry["group"] = group
    # LLM attribution — additive, old entries without it read fine.
    if llm_meta and isinstance(llm_meta, dict):
        entry["llm_meta"] = llm_meta
    return entry


def log(
    *,
    trigger: str,
    reason: str,
    session_id: str,
    proposal: Dict[str, Any],
    outcome: str,
    backup_path: str = "",
    error: str = "",
    recovery: Optional[Dict[str, Any]] = None,
    group: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    llm_meta: Optional[Dict[str, Any]] = None,
) -> str:
    entry = _new_entry(
        trigger=trigger,
        reason=reason,
        session_id=session_id,
        proposal=proposal,
        outcome=outcome,
        backup_path=backup_path,
        error=error,
        recovery=recovery,
        group=group,
        snapshot=snapshot,
        llm_meta=llm_meta,
    )
    _append_entry(entry)
    return entry["id"]


def prepare(
    *,
    trigger: str,
    reason: str,
    session_id: str,
    proposal: Dict[str, Any],
    backup_path: str = "",
    recovery: Optional[Dict[str, Any]] = None,
    group: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    llm_meta: Optional[Dict[str, Any]] = None,
) -> str:
    return log(
        trigger=trigger,
        reason=reason,
        session_id=session_id,
        proposal=proposal,
        outcome="prepared",
        backup_path=backup_path,
        recovery=recovery,
        group=group,
        snapshot=snapshot,
        llm_meta=llm_meta,
    )


def finalize(
    entry_id: str,
    outcome: str,
    *,
    error: str = "",
    pending_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one durable state transition for a logical record atomically."""
    # get_entry() and _append_entry() must share one re-entrant lock: otherwise
    # two threads can both validate the same prepared state before either writes.
    with mutation_lock():
        entry = get_entry(entry_id)
        if not entry:
            raise KeyError(f"Prepared journal entry {entry_id} not found")
        updated = dict(entry)
        updated["outcome"] = outcome
        updated["error"] = scrub_text(error)
        if pending_id is not None:
            updated["pending_id"] = scrub_text(str(pending_id))
            recovery = dict(updated.get("recovery", {}))
            recovery["pending_id"] = updated["pending_id"]
            updated["recovery"] = recovery
        updated["finalized_ts"] = time.time()
        _validate_journal_transition(entry, updated)
        _append_entry(updated)
        return sanitize(updated)


def get_entry(entry_id: str) -> Optional[Dict[str, Any]]:
    for entry in _load_entries():
        if entry.get("id") == entry_id:
            return entry
    return None


def is_reversible(entry: Optional[Dict[str, Any]]) -> bool:
    if not entry or entry.get("outcome") != "applied":
        return False
    proposal = entry.get("proposal", {})
    kind = proposal.get("kind")
    action = proposal.get("action")
    if kind == "skill" and action == "create":
        return bool(proposal.get("name") and proposal.get("content"))
    if kind == "skill" and action == "patch":
        # Ask the restore path itself rather than checking for a path string, so
        # "reversible" cannot promise more than rollback can actually deliver.
        return snapshot_before_content(entry) is not None
    if kind == "memory":
        recovery = entry.get("recovery", {})
        if recovery.get("type") != "memory_append" or not recovery.get("content"):
            return False
        # Ask what rollback can actually deliver, as the skill-patch branch does.
        # The entry must still be locatable: prefix intact and the exact content
        # present at or after its planned position. Unknown host state leaves the
        # promise standing rather than withdrawing it on a transient read failure.
        values = _memory_entries(str(recovery.get("target", "memory")))
        if values is None:
            return True
        index = recovery.get("index")
        return bool(
            _memory_prefix_matches(recovery, values)
            and isinstance(index, int)
            and recovery.get("content") in values[index:]
        )
    if kind == "prompt":
        recovery = entry.get("recovery", {})
        return bool(
            action == "create"
            and recovery.get("type") == "prompt_note"
            and recovery.get("note_id")
            and proposal.get("content")
        )
    return False


def count_today_applied() -> int:
    """Count today's edits that are applied, reserved, or rollback-in-flight.

    Returns max_edits_per_day() when the journal is unreadable, so the budget
    gate stays closed rather than silently allowing unlimited edits.
    """
    today = datetime.now(timezone.utc).date()
    try:
        all_entries = _load_entries()
    except IOError:
        return max_edits_per_day()
    count = 0
    for entry in all_entries:
        if entry.get("outcome") not in _CONSUMED_EDIT_OUTCOMES:
            continue
        try:
            if datetime.fromtimestamp(entry.get("ts", 0), tz=timezone.utc).date() == today:
                count += 1
        except (OSError, OverflowError, ValueError, TypeError):
            continue
    return count


def daily_limit_reached() -> bool:
    return count_today_applied() >= max_edits_per_day()


def was_applied_recently(proposal: Dict[str, Any], within_days: int) -> bool:
    """Return True when an identical edit exists or journal is unreadable (fail closed)."""
    target = proposal_hash(proposal)
    cutoff = time.time() - (within_days * 86400)
    try:
        all_entries = _load_entries()
    except IOError:
        return True
    for entry in all_entries:
        if entry.get("outcome") not in _CONSUMED_EDIT_OUTCOMES:
            continue
        if (entry.get("ts") or 0) >= cutoff and proposal_hash(entry.get("proposal", {})) == target:
            return True
    return False


def proposal_hash(proposal: Dict[str, Any]) -> str:
    """Identify a proposal by the storage operation it would actually perform.

    Skill ``create`` and ``patch`` are distinct operations: a newly created skill
    may legitimately need a patch with the same rendered content inside the dedup
    window. Memory is different: both accepted actions call the same
    ``MemoryStore.add("memory", content)`` append, and the proposal's ``name`` is
    ignored by that store. Prompt-note IDs are generated only when the write is
    prepared, so they are not part of the semantic create operation either.
    Canonicalizing these fields prevents the same future-context text from being
    added again under a storage-generated identity.
    """
    kind = str(proposal.get("kind", ""))
    action = str(proposal.get("action", ""))
    name = str(proposal.get("name", ""))
    content = str(proposal.get("content", ""))
    if kind == "memory" and action in ("create", "patch"):
        action = "append"
        name = "memory"
        # The store strips before saving, so two proposals differing only in
        # surrounding whitespace are the same append. Without this the dedup
        # window misses a padded re-proposal, which then spends a budget slot
        # only for the host to refuse it as an exact duplicate.
        content = content.strip()
    elif kind == "prompt" and action == "create":
        name = "prompt"
    key = "|".join([
        kind,
        action,
        name,
        hashlib.sha1(content.encode("utf-8", "replace")).hexdigest(),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]



# ── recovery metadata and target-state proof ───────────────────────────────


def _read_skill_state(name: str) -> tuple:
    """Return (known, content); absence is known only from an explicit not-found."""
    from tools.skills_tool import skill_view

    try:
        # Baselines and backups must use literal SKILL.md bytes. The host's
        # default preprocessing can render inline shell directives, producing
        # changing content and executing commands on every guard read.
        raw = skill_view(name, preprocess=False)
        result = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception as exc:
        logger.warning("Cannot view skill '%s': %s", name, scrub_text(str(exc)))
        return False, None
    if not isinstance(result, dict):
        return False, None
    if not result.get("success"):
        error = str(result.get("error", "")).lower()
        return (True, None) if "not found" in error else (False, None)
    direct = result.get("content")
    if isinstance(direct, str):
        return True, direct
    skill_path = result.get("skill_dir", "") or result.get("path", "")
    if not skill_path:
        return False, None
    path = Path(skill_path)
    if path.is_dir():
        path = path / "SKILL.md"
    try:
        return True, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True, None
    except Exception as exc:
        logger.warning("Cannot read skill file '%s': %s", path, scrub_text(str(exc)))
        return False, None


def _skill_view_result(name: str) -> Optional[Dict[str, Any]]:
    """Compatibility view used by callers that require an existing skill."""
    known, content = _read_skill_state(name)
    return {"success": True, "content": content} if known and content is not None else None


def read_skill_content(name: str) -> Optional[str]:
    known, content = _read_skill_state(name)
    return content if known else None


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


def content_digest(content: str) -> str:
    """Public wrapper over the internal digest used by planning baseline capture."""
    return _content_digest(content)


def skill_baseline(name: str) -> Optional[Dict[str, Any]]:
    """Return the current skill identity for planning-baseline comparison.

    Returns:
        None — host state is unknown (read error); cannot confirm or deny.
        {"exists": False, "sha256": ""} — skill definitively does not exist.
        {"exists": True, "sha256": "<hex>"} — skill exists with this content digest.
    """
    known, content = _read_skill_state(name)
    if not known:
        return None
    if content is None:
        return {"exists": False, "sha256": ""}
    return {"exists": True, "sha256": _content_digest(content)}


_BACKUP_RETENTION_SECONDS = 30 * 86400
_TERMINAL_BACKUP_RETENTION_SECONDS = 90 * 86400
# Outcomes whose backup is still the pre-edit copy someone may need.
#
# The first five can still lead to a rollback. ``error`` cannot, and that is
# exactly why it is here: it is recorded when the host reported success but the
# target no longer matches the proposal, so the skill may already be modified
# while ``/refine rollback`` is unavailable, and the journal snapshot is stored
# scrubbed — a credential-shaped line comes back redacted, leaving the ``.bak``
# as the only faithful copy. ``conflict`` normally removes its own backup and
# journals an empty path; it is listed for the branch where that removal fails
# (a Windows sharing violation), which leaves a real referenced file behind.
_ROLLBACKABLE_BACKUP_OUTCOMES = {
    "prepared", "pending_approval", "applied", "rollback_prepared", "pending_rollback",
}
# Terminal: no state transition leads out of these, so their backups would never
# be pruned at all if they were treated like the rollbackable ones. A backup is
# written unscrubbed on purpose (rollback must restore the user's real content),
# so "keep forever" would also mean "keep any credential in that skill in
# cleartext forever". They get a longer window instead of an unbounded one.
_TERMINAL_BACKUP_OUTCOMES = {"error", "conflict"}
_BACKUP_RETENTION_OUTCOMES = _ROLLBACKABLE_BACKUP_OUTCOMES | _TERMINAL_BACKUP_OUTCOMES


def prune_expired_backups() -> List[Path]:
    """Remove aged orphan ``.bak`` files without rewriting journal history.

    A backup referenced by an entry that can still roll back is kept regardless of
    age. One referenced only by a terminal ``error`` or ``conflict`` entry is kept
    for the longer terminal window, because the target may already have changed
    while rollback is unavailable. Everything else goes at the ordinary cutoff.
    Comparing basenames preserves migration's legacy-path fallback, where
    append-only entries retain their former absolute path while the backup itself
    is copied into the active backup directory. Any unreadable journal or
    filesystem failure fails closed by retaining the candidate.
    """
    now = time.time()
    cutoff = now - _BACKUP_RETENTION_SECONDS
    terminal_cutoff = now - _TERMINAL_BACKUP_RETENTION_SECONDS
    with mutation_lock():
        try:
            active_entries = _load_entries()
        except Exception as exc:
            logger.warning(
                "Cannot prune refine backups because the journal is unreadable: %s",
                scrub_text(str(exc)),
            )
            return []
        referenced_names = set()
        terminal_names = set()
        for entry in active_entries:
            proposal = entry.get("proposal")
            outcome = entry.get("outcome")
            if (
                outcome not in _BACKUP_RETENTION_OUTCOMES
                or not isinstance(proposal, dict)
                or proposal.get("kind") != "skill"
                or proposal.get("action") != "patch"
            ):
                continue
            backup_path = Path(str(entry.get("backup_path", "")))
            if not backup_path.name:
                continue
            if outcome in _TERMINAL_BACKUP_OUTCOMES:
                terminal_names.add(backup_path.name)
            else:
                referenced_names.add(backup_path.name)
        try:
            candidates = list(backups_dir().iterdir())
        except OSError as exc:
            logger.warning("Cannot inspect refine backups for retention: %s", scrub_text(str(exc)))
            return []
        removed: List[Path] = []
        for candidate in candidates:
            try:
                # ``referenced_names`` first: an entry that can still roll back
                # outranks a terminal one naming the same file.
                limit = terminal_cutoff if candidate.name in terminal_names else cutoff
                if (
                    candidate.suffix != ".bak"
                    or not candidate.is_file()
                    or candidate.name in referenced_names
                    or candidate.stat().st_mtime >= limit
                ):
                    continue
                candidate.unlink()
                removed.append(candidate)
            except OSError as exc:
                logger.warning(
                    "Cannot prune expired refine backup %s: %s",
                    candidate.name,
                    scrub_text(str(exc)),
                )
        return removed


def prepare_skill_recovery(name: str) -> Optional[Dict[str, Any]]:
    """Capture a skill's pre-edit state as a snapshot and owned backup."""
    # The outer refine transaction already holds this lock. Keeping the helper
    # safe on its own prevents startup orphan cleanup from seeing the newly
    # written backup before its caller can journal the recovery intent.
    with mutation_lock():
        known, before = _read_skill_state(name)
        if not known or before is None:
            return None
        backup = (
            backups_dir()
            / f"{_RECOVERY_BACKUP_PREFIX}{os.getpid()}-{uuid.uuid4().hex}_skill_{name}.bak"
        )
        try:
            _atomic_write_text(backup, before)
        except Exception as exc:
            logger.warning("Cannot back up skill '%s': %s", name, scrub_text(str(exc)))
            return None
        # Retention is opportunistic: a cleanup failure must not invalidate the
        # newly created durable recovery copy that this edit still needs.
        try:
            prune_expired_backups()
        except Exception as exc:
            logger.warning("Cannot prune expired refine backups: %s", scrub_text(str(exc)))
        return {
            "backup_path": str(backup),
            "snapshot": {
                "kind": "skill",
                "name": name,
                "before": before,
                "before_sha256": _content_digest(before),
            },
        }


def _snapshot_of(entry: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = entry.get("snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def snapshot_before_content(entry: Dict[str, Any]) -> Optional[str]:
    """Return the verified pre-edit content, preferring the journal snapshot.

    Falls back to the backup file both for entries written before snapshots
    existed and for a snapshot whose digest no longer matches its content.
    """
    snapshot = _snapshot_of(entry)
    before = snapshot.get("before")
    digest = str(snapshot.get("before_sha256", ""))
    if isinstance(before, str) and digest:
        if _content_digest(before) == digest:
            return before
        logger.warning(
            "Refine journal snapshot for '%s' does not match its digest; "
            "falling back to the backup file",
            scrub_text(str(snapshot.get("name", ""))),
        )
    backup_path = Path(str(entry.get("backup_path", "")))
    if not backup_path.is_file() and backup_path.name:
        # Pre-snapshot journal entries store an absolute legacy path. Migration
        # copies backups without rewriting append-only history, so resolve the
        # same basename in the active generation before declaring it lost.
        migrated_backup = journal_dir() / _BACKUPS_DIR_NAME / backup_path.name
        if migrated_backup.is_file():
            backup_path = migrated_backup
    if not backup_path.is_file():
        return None
    try:
        return backup_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(
            "Cannot read refine backup %s: %s", backup_path, scrub_text(str(exc))
        )
        return None


def _memory_entries(target: str) -> Optional[List[str]]:
    from tools.memory_tool import MemoryStore

    try:
        store = MemoryStore()
        store.load_from_disk()
        return list(store._entries_for(target))  # noqa: SLF001 - host has no public reader
    except Exception as exc:
        logger.warning("Cannot read %s memory: %s", target, scrub_text(str(exc)))
        return None


def memory_baseline(
    target: str, content: str, memory_entries: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """Locate the plugin's own last applied memory content among current entries.

    Returns:
        None — host memory state is unavailable (read error); cannot confirm
        or deny anything.
        {"present": True, "index": <int>} — the exact stripped content the
        plugin appended still sits in the store.
        {"present": False, "index": None} — the exact string is no longer in
        the store.

    Limit, named honestly: membership of one exact string cannot distinguish
    "the entry was edited" from "the entry was removed". A host-side edit of
    the entry (Hermes consolidation rewrites entries freely) and a deletion
    both collapse to ``present: False``. Callers must therefore report
    "no longer present as applied", never "was deleted". Nothing here infers
    WHY the content is gone.
    """
    if memory_entries is not None:
        values = list(memory_entries)
    else:
        values = _memory_entries(target)
        if values is None:
            return None
    wanted = (content or "").strip()
    if not wanted:
        return None
    for position, entry in enumerate(values):
        if isinstance(entry, str) and entry.strip() == wanted:
            return {"present": True, "index": position}
    return {"present": False, "index": None}


def backup_memory(target: str) -> Optional[str]:
    entries_value = _memory_entries(target)
    if entries_value is None:
        return None
    return "\n\n---\n\n".join(entries_value)


def _memory_file_lock(store: Any, target: str):
    """The host's per-file memory lock, or a no-op when it exposes none.

    Only a missing or renamed private is treated as a compatibility case; any
    other failure propagates so the caller reports it and restores state, rather
    than quietly downgrading to an unlocked rewrite.
    """
    try:
        return store._file_lock(store._path_for(target))  # noqa: SLF001
    except AttributeError as exc:
        logger.warning(
            "Host exposes no memory file lock; rolling back unlocked: %s",
            scrub_text(str(exc)),
        )
        return nullcontext()


def _reload_memory_target(store: Any, target: str) -> str:
    """Re-read memory inside the lock; return the host's drift marker if any."""
    try:
        return str(store._reload_target(target) or "")  # noqa: SLF001
    except AttributeError:
        # Older host: no drift detection to consult, so fall back to a plain read.
        store.load_from_disk()
        return ""


def memory_recovery(target: str, content: str) -> Optional[Dict[str, Any]]:
    entries_value = _memory_entries(target)
    if entries_value is None:
        return None
    digest = hashlib.sha256(
        json.dumps(entries_value, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "type": "memory_append",
        "target": target,
        "index": len(entries_value),
        "prefix_digest": digest,
        "content": content,
    }


def _memory_prefix_matches(recovery: Dict[str, Any], values: List[str]) -> bool:
    index = recovery.get("index")
    if not isinstance(index, int) or index < 0 or index > len(values):
        return False
    digest = hashlib.sha256(
        json.dumps(list(values[:index]), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return digest == recovery.get("prefix_digest")


def target_matches_applied(entry: Dict[str, Any]) -> Optional[bool]:
    """Prove the proposal target; return None when target state is unavailable."""
    proposal = entry.get("proposal", {})
    kind = proposal.get("kind")
    if kind == "skill":
        known, content = _read_skill_state(str(proposal.get("name", "")))
        return (content == str(proposal.get("content", ""))) if known else None
    if kind == "memory":
        recovery = entry.get("recovery", {})
        values = _memory_entries(str(recovery.get("target", "memory")))
        if values is None:
            return None
        index = recovery.get("index")
        # The append is proven by presence at or after its planned position, not
        # by occupying that exact slot. A staged write is replayed by the host as
        # a plain append whenever approval happens, so anything the agent stored
        # during the approval window sits in between and shifts it down. Keying
        # on the exact slot reported an approved edit as rejected. Entries before
        # the planned position are still pinned by the prefix digest.
        return bool(
            _memory_prefix_matches(recovery, values)
            and isinstance(index, int)
            and recovery.get("content") in values[index:]
        )
    if kind == "prompt":
        recovery = entry.get("recovery", {})
        if recovery.get("type") != "prompt_note":
            return False
        notes = _load_prompt_notes()
        if notes is None:
            return None
        return any(
            note["id"] == recovery.get("note_id")
            and note["content"] == proposal.get("content", "")
            for note in notes
        )
    return False


def rollback_target_matches(entry: Dict[str, Any]) -> Optional[bool]:
    """Prove rollback state; return None when target state is unavailable."""
    proposal = entry.get("proposal", {})
    kind = proposal.get("kind")
    if kind == "skill":
        name = str(proposal.get("name", ""))
        if proposal.get("action") == "create":
            known, content = _read_skill_state(name)
            return (content is None) if known else None
        expected = snapshot_before_content(entry)
        if expected is None:
            # Without a restore source there is nothing to compare against, so
            # the rollback state is unknown rather than proven false. Returning
            # False here would let ``reconcile`` declare an approved staged
            # rollback rejected and push the record back to ``applied``.
            return None
        known, current = _read_skill_state(name)
        return (current == expected) if known else None
    if kind == "memory":
        recovery = entry.get("recovery", {})
        values = _memory_entries(str(recovery.get("target", "memory")))
        if values is None:
            return None
        index = recovery.get("index")
        # Mirror of the applied check: gone means absent at or after the planned
        # position, with everything before it unchanged.
        return bool(
            _memory_prefix_matches(recovery, values)
            and isinstance(index, int)
            and recovery.get("content") not in values[index:]
        )
    if kind == "prompt":
        recovery = entry.get("recovery", {})
        if recovery.get("type") != "prompt_note":
            return False
        note_id = recovery.get("note_id")
        if not isinstance(note_id, str) or not note_id:
            return None
        notes = _load_prompt_notes()
        if notes is None:
            return None
        return not any(note["id"] == note_id for note in notes)
    return False


def _prepared_is_abandoned(entry: Dict[str, Any]) -> bool:
    """Whether a ``prepared`` record is too old for any pass to still own it.

    ``prepared`` is the short window between "backup taken" and the host write
    returning. A pass that dies inside that window leaves the record behind, and
    because ``count_today_applied`` counts ``prepared`` as consumed, it burns one
    of the day's three edits permanently. Age is what separates that corpse from
    a mutation still in flight, so a missing or unusable timestamp means "not
    abandoned" rather than "old enough".
    """
    ts = entry.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    return (time.time() - float(ts)) >= _ABANDONED_PREPARED_SECONDS


def _rollback_prepared_is_abandoned(entry: Dict[str, Any]) -> bool:
    """Whether an unfinalized rollback intent is safely old enough to recover.

    The original edit timestamp says nothing about when rollback started. Only
    the transition's finite ``finalized_ts`` bounds a currently in-flight host
    rollback; unknown or malformed metadata must remain pending.
    """
    finalized_ts = entry.get("finalized_ts")
    if (
        isinstance(finalized_ts, bool)
        or not isinstance(finalized_ts, (int, float))
        or not math.isfinite(float(finalized_ts))
    ):
        return False
    return (time.time() - float(finalized_ts)) >= _ABANDONED_PREPARED_SECONDS


def _pending_exists(subsystem: str, pending_id: str) -> Optional[bool]:
    """Return True/False for known approval state, None when host lookup failed."""
    if not pending_id:
        return False
    try:
        from tools.write_approval import get_pending

        raw = get_pending(subsystem, pending_id)
        result = json.loads(raw) if isinstance(raw, str) else raw
        return bool(result)
    except Exception as exc:
        logger.warning("Cannot query pending approval %s: %s", pending_id, scrub_text(str(exc)))
        return None


def _interrupted_pending_id(
    entry: Dict[str, Any], *, rollback: bool
) -> Optional[str]:
    """Refuse to guess an approval ID lost after durable host staging.

    ``""`` means a legacy host is proven unable to stage this mutation.
    ``None`` means a staging-capable host cannot provide causal identity, so
    reconciliation must retain the nonterminal intent and budget reservation.
    The current Hermes queue has no caller correlation field; payload and time
    similarity are deliberately insufficient because another actor can submit
    the same write.
    """
    proposal = entry.get("proposal", {})
    if not isinstance(proposal, dict):
        return None
    kind = proposal.get("kind")
    if kind not in {"skill", "memory"}:
        return ""
    subsystem = "skills" if kind == "skill" else "memory"

    try:
        approval = importlib.import_module("tools.write_approval")
    except ModuleNotFoundError as exc:
        if exc.name in {"tools", "tools.write_approval"}:
            # Only exact module absence proves this host predates approvals.
            return ""
        logger.warning(
            "Cannot load host approval capability while recovering %s: %s",
            scrub_text(str(entry.get("id", ""))),
            scrub_text(str(exc)),
        )
        return None
    except Exception as exc:
        logger.warning(
            "Cannot load host approval capability while recovering %s: %s",
            scrub_text(str(entry.get("id", ""))),
            scrub_text(str(exc)),
        )
        return None
    enumerate_pending = getattr(approval, "list_pending", None)
    if not callable(enumerate_pending):
        # Importing the approval module is itself evidence that this host can
        # stage writes. Missing individual APIs may identify an older approval
        # implementation, but cannot prove an earlier request never queued.
        # Only exact module absence above authorizes legacy target inference.
        return None

    try:
        records = enumerate_pending(subsystem)
    except Exception as exc:
        logger.warning(
            "Cannot enumerate pending %s writes while recovering %s: %s",
            subsystem,
            scrub_text(str(entry.get("id", ""))),
            scrub_text(str(exc)),
        )
        return None
    if not isinstance(records, list):
        logger.warning("Host returned a malformed pending %s list", subsystem)
        return None
    pending_count = getattr(approval, "pending_count", None)
    if not callable(pending_count):
        logger.warning(
            "Cannot verify completeness of pending %s enumeration", subsystem
        )
        return None
    try:
        if pending_count(subsystem) != len(records):
            logger.warning(
                "Host pending %s enumeration was incomplete or changed concurrently",
                subsystem,
            )
            return None
    except Exception as exc:
        logger.warning(
            "Cannot verify pending %s enumeration: %s",
            subsystem,
            scrub_text(str(exc)),
        )
        return None

    # Hermes does not persist a caller correlation value in these records.
    # Payload equality, queue origin, and timestamps can narrow candidates but
    # cannot prove that any request belongs to this journal entry: another actor
    # may stage the same operation after our prepare. Even an empty queue is not
    # proof of absence because our request may already have been approved or
    # rejected. Keep the intent nonterminal and budget-consuming until the host
    # exposes exact identity rather than attributing a foreign mutation.
    logger.warning(
        "Host approval queue cannot causally identify interrupted %s for journal entry %s",
        "rollback" if rollback else "write",
        scrub_text(str(entry.get("id", ""))),
    )
    return None


def _reconcile_cleanup_prepared(snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Complete one exact cleanup intent without inferring from legacy absence."""
    entry_id = str(snapshot.get("id", ""))
    session_id = normalize_prompt_note_session_id(snapshot.get("session_id", ""))
    if not entry_id or not session_id:
        return None
    with try_mutation_lock() as acquired:
        if not acquired:
            logger.warning(
                "Left journal entry %s cleanup_prepared: another process holds the mutation lock",
                entry_id,
            )
            return None
        current = get_entry(entry_id)
        if (
            not current
            or current.get("outcome") != "cleanup_prepared"
            or not _prompt_cleanup_identity_matches(current, session_id)
        ):
            return None
        notes = _load_prompt_notes()
        if notes is None:
            return None
        note_id = str(current.get("recovery", {}).get("note_id", ""))
        index = next(
            (position for position, note in enumerate(notes) if note["id"] == note_id),
            None,
        )
        if index is not None:
            if not _prompt_cleanup_note_matches(current, notes[index], session_id):
                # Refine will not remove a note whose content, scope, or session
                # no longer matches the intent it recorded, so this entry stays
                # cleanup_prepared until the store is repaired. Name the note:
                # nothing else in the plugin can resolve it.
                logger.warning(
                    "Left journal entry %s cleanup_prepared: prompt note %s "
                    "no longer matches the recorded intent",
                    entry_id,
                    note_id,
                )
                return None
            _write_prompt_notes(notes[:index] + notes[index + 1:])
            verified = _load_prompt_notes()
            if verified is None or any(note["id"] == note_id for note in verified):
                return None
        return finalize(entry_id, "cleanup_resolved")


def reconcile() -> List[Dict[str, Any]]:
    """Lazily reconcile approvals, rollback intents, and prompt cleanup intents."""
    changed: List[Dict[str, Any]] = []
    for snapshot in _load_entries():
        entry_id = str(snapshot.get("id", ""))
        outcome = snapshot.get("outcome")
        if outcome not in {
            "prepared", "pending_approval", "cleanup_prepared",
            "rollback_prepared", "pending_rollback",
        }:
            continue
        proposal = snapshot.get("proposal", {})
        subsystem = "skills" if proposal.get("kind") == "skill" else "memory"
        try:
            if outcome == "cleanup_prepared":
                resolved = _reconcile_cleanup_prepared(snapshot)
                if resolved is not None:
                    changed.append(resolved)
                continue
            if outcome == "prepared":
                if proposal.get("kind") in {"skill", "memory"}:
                    interrupted_pending = _interrupted_pending_id(
                        snapshot, rollback=False
                    )
                    if interrupted_pending is None:
                        continue
                    if interrupted_pending:
                        changed.append(finalize(
                            entry_id,
                            "pending_approval",
                            pending_id=interrupted_pending,
                        ))
                        continue
                applied_state = target_matches_applied(snapshot)
                if applied_state is True:
                    changed.append(finalize(entry_id, "applied"))
                    continue
                # Absence has to be proven, not inferred from "does not match".
                # A pass that died after the host write but before finalize also
                # leaves a ``prepared`` record, and any later edit of that target
                # makes it stop matching. Declaring that one un-applied would lie
                # in the journal, drop the edit out of ``is_reversible``, let its
                # backup be pruned, and hand back a budget slot for a mutation
                # that really happened. ``rollback_target_matches`` is the
                # positive test: the target looks exactly as it would after a
                # rollback, so the edit is genuinely not there.
                # A prompt note is exempt. Session end does journal its own
                # cleanup intent now (``cleanup_prepared`` ->
                # ``cleanup_resolved``), but only for a note whose exact
                # ownership it can prove: a note dropped by an older build, by
                # hand, or by a cleanup that could not prove ownership leaves no
                # receipt at all. Absence therefore still proves nothing about
                # whether this note was ever written, and session end is also
                # exactly when the automatic pass runs.
                if (
                    proposal.get("kind") != "prompt"
                    and rollback_target_matches(snapshot) is True
                    and _prepared_is_abandoned(snapshot)
                ):
                    # Read-then-act: a slow host call can outlive the age
                    # threshold, so re-prove it while no other process is
                    # mutating. Within one thread the lock is re-entrant, so this
                    # guards against other processes and threads, not against the
                    # caller itself -- every production caller already holds it.
                    with try_mutation_lock() as acquired:
                        if not acquired:
                            logger.warning(
                                "Left journal entry %s prepared: another process "
                                "holds the mutation lock",
                                entry_id,
                            )
                            continue
                        current = get_entry(entry_id)
                        if not current or current.get("outcome") != "prepared":
                            continue
                        if rollback_target_matches(current) is not True:
                            continue
                        changed.append(finalize(
                            entry_id,
                            "error",
                            error=(
                                "Abandoned while prepared: the target is now in "
                                "its pre-edit state, so there is nothing to "
                                "reverse. Whether the edit never landed or landed "
                                "and was undone elsewhere is not distinguishable "
                                "from the target alone."
                            ),
                        ))
                continue
            if outcome == "pending_approval":
                pending = _pending_exists(subsystem, str(snapshot.get("pending_id", "")))
                if pending is not False:
                    # While the host record still exists (or its state cannot be
                    # queried), an already-matching target is not proof that this
                    # particular request was approved.
                    continue
                applied_state = target_matches_applied(snapshot)
                if applied_state is True:
                    changed.append(finalize(entry_id, "applied"))
                elif applied_state is False:
                    # The host record is gone and the target never changed. A user
                    # denial and a refusal at replay time (duplicate, size limit,
                    # content scan) are indistinguishable from here, so the record
                    # must not claim which one happened.
                    changed.append(finalize(
                        entry_id,
                        "rejected",
                        error="Approval did not result in a write (denied or refused by the host)",
                    ))
                continue
            if outcome == "rollback_prepared":
                if proposal.get("kind") == "skill":
                    interrupted_pending = _interrupted_pending_id(
                        snapshot, rollback=True
                    )
                    if interrupted_pending is None:
                        continue
                    if interrupted_pending:
                        changed.append(finalize(
                            entry_id,
                            "pending_rollback",
                            pending_id=interrupted_pending,
                        ))
                        continue
                if rollback_target_matches(snapshot) is True:
                    changed.append(finalize(entry_id, "rolled_back"))
                    continue
                # An interrupted rollback can leave its intent durable even
                # though the original applied target is still provably present.
                # Never infer this from age alone: require a finite timestamp on
                # the rollback transition, positive target proof, and re-prove
                # both state and target while serialized with other mutations.
                if (
                    _rollback_prepared_is_abandoned(snapshot)
                    and target_matches_applied(snapshot) is True
                ):
                    with try_mutation_lock() as acquired:
                        if not acquired:
                            logger.warning(
                                "Left journal entry %s rollback_prepared: another "
                                "process holds the mutation lock",
                                entry_id,
                            )
                            continue
                        current = get_entry(entry_id)
                        if (
                            not current
                            or current.get("outcome") != "rollback_prepared"
                            or not _rollback_prepared_is_abandoned(current)
                            or target_matches_applied(current) is not True
                        ):
                            continue
                        changed.append(finalize(
                            entry_id,
                            "applied",
                            error=(
                                "Abandoned rollback intent: the original applied "
                                "target is still present after a finite stale "
                                "rollback transition."
                            ),
                        ))
                continue
            if outcome == "pending_rollback":
                pending = _pending_exists(subsystem, str(snapshot.get("pending_id", "")))
                if pending is not False:
                    continue
                rollback_state = rollback_target_matches(snapshot)
                if rollback_state is True:
                    changed.append(finalize(entry_id, "rolled_back"))
                elif rollback_state is False:
                    changed.append(
                        finalize(entry_id, "applied", error="Rollback approval rejected")
                    )
        except Exception as exc:
            logger.warning("Cannot reconcile journal entry %s: %s", entry_id, scrub_text(str(exc)))
    return changed


# ── rollback side effects ──────────────────────────────────────────────────


def _restore_applied(entry_id: str, error: str) -> None:
    try:
        finalize(entry_id, "applied", error=error)
    except Exception as exc:
        logger.warning("Cannot restore applied state for %s: %s", entry_id, scrub_text(str(exc)))


def rollback_skill(entry_id: str) -> Dict[str, Any]:
    entry = get_entry(entry_id)
    if not is_reversible(entry):
        return {"success": False, "error": f"Journal entry {entry_id} is not a reversible skill edit"}
    proposal = entry.get("proposal", {})
    if proposal.get("kind") != "skill":
        return {"success": False, "error": "Journal entry is not a skill edit"}
    name = str(proposal.get("name", ""))
    action = proposal.get("action")

    current = read_skill_content(name)
    expected = str(proposal.get("content", ""))
    if current != expected:
        return {"success": False, "error": f"Rollback conflict: skill '{name}' changed after refine applied it"}
    backup_content = ""
    if action != "create":
        restored = snapshot_before_content(entry)
        if restored is None:
            return {
                "success": False,
                "error": (
                    f"Cannot restore skill '{name}': the journal carries no verified "
                    "snapshot and its backup file is missing or unreadable"
                ),
            }
        backup_content = restored

    try:
        if entry.get("outcome") != "rollback_prepared":
            entry = finalize(entry_id, "rollback_prepared")
    except Exception as exc:
        return {"success": False, "error": f"Cannot journal rollback intent: {scrub_text(str(exc))}"}

    try:
        from tools.skill_manager_tool import skill_manage

        raw = (
            skill_manage(action="delete", name=name)
            if action == "create"
            else skill_manage(action="edit", name=name, content=backup_content)
        )
        result = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception as exc:
        error = f"Rollback failed: {scrub_text(str(exc))}"
        _restore_applied(entry_id, error)
        return {"success": False, "error": error}

    if not result.get("success"):
        error = scrub_text(str(result.get("error", "Rollback host operation failed")))
        _restore_applied(entry_id, error)
        return sanitize(result)

    if result.get("staged"):
        pending_id = str(result.get("pending_id", ""))
        if not pending_id:
            return {
                "success": False,
                "staged": True,
                "outcome": "rollback_prepared",
                "journal_id": entry_id,
                "error": (
                    "Rollback was staged without a pending_id; retained the "
                    "rollback intent for reconciliation"
                ),
            }
        try:
            finalize(entry_id, "pending_rollback", pending_id=pending_id)
        except Exception as exc:
            return {
                "success": False,
                "staged": True,
                "pending_id": pending_id,
                "error": (
                    "Rollback was reserved but pending state finalization failed; "
                    f"recovery id: {entry_id}. {scrub_text(str(exc))}"
                ),
            }
        result["message"] = "Rollback is pending approval; target has not been marked rolled back"
        return sanitize(result)

    current_entry = get_entry(entry_id) or entry
    if not rollback_target_matches(current_entry):
        error = "Rollback host reported success but the target state did not change"
        _restore_applied(entry_id, error)
        return {"success": False, "error": error}
    try:
        finalize(entry_id, "rolled_back")
    except Exception as exc:
        return {
            "success": False,
            "error": (
                "Rollback changed the target but journal finalization failed; "
                f"recovery id: {entry_id}. {scrub_text(str(exc))}"
            ),
        }
    result["message"] = result.get("message", f"Skill '{name}' rolled back")
    return sanitize(result)


def rollback_memory(entry_id: str) -> Dict[str, Any]:
    """Remove exactly refine's own append, under the host's per-file lock.

    This does not go through the host's gated removal, and the reason is not
    convenience. That removal binds by substring and pops a single match even
    when that match is a strict superstring of the text given. Under the approval
    gate the removal is staged and replayed later, so between staging and
    approval the entry can be replaced or extended and the replay then deletes
    the *user's* entry -- a delete of something refine never created, which is
    the one thing this plugin may never do.

    Refine knows the exact content and position of its own append, so it removes
    that entry itself while holding the host's file lock, re-reading and
    re-proving inside it. The trade is a disclosed gap: a memory rollback is not
    approval-gated. See the README section on identifying a memory entry.
    """
    with mutation_lock():
        return _rollback_memory_locked(entry_id)


def _rollback_memory_locked(entry_id: str) -> Dict[str, Any]:
    entry = get_entry(entry_id)
    if not is_reversible(entry):
        return {"success": False, "error": f"Journal entry {entry_id} is not a reversible memory edit"}
    recovery = entry.get("recovery", {})
    if recovery.get("type") != "memory_append":
        return {"success": False, "error": "Memory recovery metadata is missing"}
    target = str(recovery.get("target", "memory"))
    expected = recovery.get("content", "")
    index = recovery.get("index")

    try:
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        # Everything from the re-read to the persist happens inside the host's own
        # file lock. ``save_to_disk`` rewrites the whole file and does not lock,
        # re-read, or check drift by itself, so doing this outside the lock would
        # discard whatever another session appended in between. Refine's mutation
        # lock cannot substitute: it does not serialize the host's other writers.
        with _memory_file_lock(store, target):
            drift = _reload_memory_target(store, target)
            if drift:
                return {
                    "success": False,
                    "error": (
                        "Memory rollback conflict: the memory file changed "
                        "outside refine and was backed up by the host"
                    ),
                }
            values = store._entries_for(target)  # noqa: SLF001
            if not isinstance(index, int) or index < 0 or index > len(values):
                return {"success": False, "error": "Memory rollback conflict: appended entry position changed"}
            if not _memory_prefix_matches(recovery, list(values)):
                return {"success": False, "error": "Memory rollback conflict: target entry or earlier memory changed"}
            # Prove this is refine's own append: exact content, at or after the
            # planned position, with everything before it pinned by the digest.
            # An approved staged write lands after whatever was stored while it
            # waited, so the planned slot itself cannot be trusted. Proven and
            # acted on inside the lock, so nothing can change in between.
            try:
                position = list(values).index(expected, index)
            except ValueError:
                return {"success": False, "error": "Memory rollback conflict: appended entry position changed"}
            if entry.get("outcome") != "rollback_prepared":
                entry = finalize(entry_id, "rollback_prepared")
            del values[position]
            store.save_to_disk(target)
    except Exception as exc:
        latest = get_entry(entry_id) or entry
        if not rollback_target_matches(latest):
            _restore_applied(entry_id, scrub_text(str(exc)))
        return {"success": False, "error": f"Memory rollback failed: {scrub_text(str(exc))}"}

    latest = get_entry(entry_id) or entry
    if not rollback_target_matches(latest):
        error = "Memory rollback target state did not change"
        _restore_applied(entry_id, error)
        return {"success": False, "error": error}
    try:
        finalize(entry_id, "rolled_back")
    except Exception as exc:
        return {
            "success": False,
            "error": (
                "Memory rollback changed the target but journal finalization failed; "
                f"recovery id: {entry_id}. {scrub_text(str(exc))}"
            ),
        }
    # ``target`` is the host's own name for the store ("memory" or "user"), so
    # interpolating it before the word "memory" read as "memory memory entry" on
    # every personal-memory rollback -- including in the live evidence quoted in
    # VERIFICATION.md.
    return {
        "success": True,
        "message": f"Removed the exact appended entry from the {target} store",
    }


def rollback_prompt_note(entry_id: str) -> Dict[str, Any]:
    """Remove only the unchanged plugin-owned note identified by this journal entry."""
    with mutation_lock():
        entry = get_entry(entry_id)
        if not is_reversible(entry):
            return {"success": False, "error": f"Journal entry {entry_id} is not a reversible prompt note"}
        proposal = entry.get("proposal", {})
        recovery = entry.get("recovery", {})
        if proposal.get("kind") != "prompt" or recovery.get("type") != "prompt_note":
            return {"success": False, "error": "Prompt-note recovery metadata is missing"}
        note_id = recovery.get("note_id")
        expected = proposal.get("content", "")
        notes = _load_prompt_notes()
        if notes is None:
            return {"success": False, "error": "Prompt-note store is unavailable"}
        index = next((i for i, note in enumerate(notes) if note["id"] == note_id), None)
        if index is None:
            return {"success": False, "error": "Prompt-note rollback conflict: note is missing"}
        if notes[index]["content"] != expected:
            return {"success": False, "error": "Prompt-note rollback conflict: note changed after refine applied it"}
        try:
            if entry.get("outcome") != "rollback_prepared":
                entry = finalize(entry_id, "rollback_prepared")
            _write_prompt_notes(notes[:index] + notes[index + 1:])
        except Exception as exc:
            _restore_applied(entry_id, scrub_text(str(exc)))
            return {"success": False, "error": f"Prompt-note rollback failed: {scrub_text(str(exc))}"}

        latest = get_entry(entry_id) or entry
        if not rollback_target_matches(latest):
            error = "Prompt-note rollback target state did not change"
            _restore_applied(entry_id, error)
            return {"success": False, "error": error}
        try:
            finalize(entry_id, "rolled_back")
        except Exception as exc:
            return {
                "success": False,
                "error": (
                    "Prompt-note rollback changed the target but journal finalization failed; "
                    f"recovery id: {entry_id}. {scrub_text(str(exc))}"
                ),
            }
        return {"success": True, "message": f"Removed prompt note {note_id}"}
