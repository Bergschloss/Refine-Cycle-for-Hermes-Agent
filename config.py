"""Config reader for the refine plugin.

Reads ``plugins.entries.refine.*`` from the Hermes config.yaml.
All values have sensible defaults — config.yaml only provides overrides.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
_RUNTIME_JOURNAL_DIR: Optional[Path] = None
_RUNTIME_JOURNAL_COMMIT_MARKER: Optional[Path] = None


def hermes_home() -> Path:
    """Resolve the Hermes data directory the way Hermes itself does.

    Hardcoding ``~/.hermes`` is wrong on Windows, where the data lives in
    ``%LOCALAPPDATA%\\hermes``, and wrong under profiles. Getting it wrong is
    not loud: the plugin simply finds no trajectory and returns no_op forever
    without ever explaining why.
    """
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        env_home = os.environ.get("HERMES_HOME", "").strip()
        if env_home:
            return Path(env_home)
        if os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            if local_app_data:
                return Path(local_app_data) / "hermes"
        return Path(os.path.expanduser("~/.hermes"))


def state_db_path() -> Path:
    return hermes_home() / "state.db"


_WRITE_APPROVAL_SUBSYSTEMS = ("memory", "skills")
_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z0-9_-]+):\s*(?:#.*)?$")
_WRITE_APPROVAL_ON = re.compile(r"^(\s+write_approval:\s*)(true|yes|on|1)(\s*(?:#.*)?)$", re.I)


def disable_host_write_approval() -> List[str]:
    """Turn the host's skills/memory write-approval gate off, and say which changed.

    Refine exists to improve the agent without anyone clicking approve. With that
    gate on, the host queues **every** memory and skill write — the agent's own as
    much as refine's — and nothing lands until a human drains the queue. Left on,
    it looks like the agent simply stopped learning: no error, no output, writes
    piling up invisibly. So refine turns it off rather than documenting a footgun
    and hoping the footgun is read.

    This is the one place refine writes to the Hermes config, and it is
    deliberately the narrowest possible write: only a ``write_approval: true``
    line inside the ``memory:`` or ``skills:`` block is rewritten, so comments,
    key order, formatting and every other value survive. A ``.refine-bak`` copy
    is kept next to the file. An administrator-managed config is left alone.

    Never raises: a failure here must not stop the plugin from registering.
    """
    changed: List[str] = []
    try:
        if _host_config_is_managed():
            return []
        path = hermes_home() / "config.yaml"
        original = path.read_text(encoding="utf-8")
    except Exception:
        return []

    section = ""
    lines = original.splitlines(keepends=True)
    for index, line in enumerate(lines):
        top = _TOP_LEVEL_KEY.match(line)
        if top:
            section = top.group(1)
            continue
        if section not in _WRITE_APPROVAL_SUBSYSTEMS:
            continue
        match = _WRITE_APPROVAL_ON.match(line.rstrip("\r\n"))
        if match:
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = f"{match.group(1)}false{match.group(3)}{ending}"
            changed.append(section)
    if not changed:
        return []

    try:
        backup = path.with_suffix(path.suffix + ".refine-bak")
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
        temp = path.with_suffix(path.suffix + ".refine-tmp")
        temp.write_text("".join(lines), encoding="utf-8")
        os.replace(str(temp), str(path))
    except Exception:
        logger.warning(
            "Could not turn off host write approval for %s; "
            "writes will queue until it is turned off by hand",
            ", ".join(changed),
            exc_info=True,
        )
        return []
    return changed


def _host_config_is_managed() -> bool:
    """Whether an administrator pins this config, in which case refine must not write."""
    try:
        from hermes_cli import managed_scope

        return any(
            managed_scope.is_key_managed(f"{subsystem}.write_approval")
            for subsystem in _WRITE_APPROVAL_SUBSYSTEMS
        )
    except Exception:
        return False


def _resolve_hermes_home_placeholder(value: str) -> str:
    """Replace the documented <HERMES_HOME> pseudo-variable with the real path.

    README documents ``journal_dir: "<HERMES_HOME>/refine-data"`` as valid
    configuration. On Windows, angle brackets in paths are syntactically
    illegal and cause silent OSError when mkdir() is called. Substitution
    happens once here; all config path readers route through this helper.
    """
    if "<HERMES_HOME>" in value:
        return value.replace("<HERMES_HOME>", str(hermes_home()))
    return value


def _load_raw_config() -> Optional[Dict[str, Any]]:
    """Load the full Hermes config.yaml."""
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        logger.warning("Cannot load Hermes config; using defaults")
        return None


def _refine_entry_from_raw(raw: Any) -> Dict[str, Any]:
    """Extract the plugin entry from one already-loaded config snapshot."""
    if not isinstance(raw, dict) or not raw:
        return {}
    plugins_cfg = raw.get("plugins", {})
    if not isinstance(plugins_cfg, dict):
        return {}
    entries = plugins_cfg.get("entries", {})
    if not isinstance(entries, dict):
        return {}
    entry = entries.get("refine", {})
    if not isinstance(entry, dict):
        return {}
    return entry


def _get_refine_entry() -> Dict[str, Any]:
    return _refine_entry_from_raw(_load_raw_config())


def _parse_bool(value, default: bool, key_for_log: str) -> bool:
    """Unified boolean parser for config keys including trust flags.

    A bare ``bool(value)`` is wrong for exactly the values people write: unquoted
    ``0`` arrives as ``int`` and quoted ``"false"`` as a non-empty ``str``, and
    both are truthy. That failure is silent and it fails *open* — the operator
    believes a switch is off while the plugin acts as if it is on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
    elif isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "yes", "on", "1"):
            return True
        if normalized in ("false", "no", "off", "0"):
            return False
    if value is not None:
        logger.warning(
            "Config key '%s' has unrecognized boolean value; using default", key_for_log
        )
    return default


def get_bool(key: str, default: bool) -> bool:
    """Read a boolean config key with a default. Accepts 0/1 and string forms."""
    return _parse_bool(_get_refine_entry().get(key), default, key)


def _get_fail_closed_bool(key: str, default: bool = True) -> bool:
    """Read a trust/privacy switch from one config snapshot, failing closed.

    Checking availability and then calling ``get_bool`` loads the file twice. An
    editor can replace config.yaml between those reads: the first succeeds, the
    second fails, and the ordinary ``True`` default silently re-enables the very
    behavior this guard is meant to protect. Load once, parse that same object,
    and return ``False`` if no mapping was available.
    """
    raw = _load_raw_config()
    if not isinstance(raw, dict):
        return False
    return _parse_bool(_refine_entry_from_raw(raw).get(key), default, key)


def _int_from_entry(
    entry: Dict[str, Any], key: str, default: int, min_val: int
) -> int:
    """Parse one integer config key out of an already-loaded entry.

    Split out of ``get_int`` so a caller that must inspect the entry before
    choosing a key can decide and read from the *same* snapshot. Loading twice
    lets config.yaml change in between, which is the failure
    ``_get_fail_closed_bool`` documents for booleans.
    """
    val = entry.get(key)
    parsed: Optional[int] = None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        parsed = int(val)
    elif isinstance(val, str):
        try:
            parsed = int(val.strip())
        except ValueError:
            pass
    if parsed is not None:
        if parsed < min_val:
            logger.warning(
                "Config key '%s' is below minimum %d; using the minimum",
                key,
                min_val,
            )
            return min_val
        return parsed
    if val is not None:
        logger.warning("Config key '%s' has unrecognized integer value; using default", key)
    return default


def get_int(key: str, default: int, min_val: int = 1) -> int:
    """Read an integer config key with a default and visible floor clamp."""
    return _int_from_entry(_get_refine_entry(), key, default, min_val)


def _coerce_string_config_value(value: Any, key: str) -> Tuple[str, str]:
    """Return safe string config text plus a user-visible conversion issue."""
    if isinstance(value, str):
        return value, ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        logger.warning("Config key '%s' uses a numeric value; coercing it to text", key)
        return str(value), f"config {key} was coerced from a numeric value"
    if value is not None:
        logger.warning("Config key '%s' has an unrecognized string value; ignoring it", key)
        return "", f"config {key} was ignored because it must be text or a number"
    return "", ""


def get_str(key: str, default: str = "") -> str:
    """Read a string config key, accepting non-boolean numeric YAML scalars."""
    entry = _get_refine_entry()
    value, _issue = _coerce_string_config_value(entry.get(key), key)
    return value if value else default


def config_available() -> bool:
    """Whether the Hermes config was both readable and shaped like a config.

    A file that parses into a non-mapping (a bare list, a string) is as
    unusable as one that fails to parse, so it must not be reported as
    available: every accessor below would raise on it.
    """
    return isinstance(_load_raw_config(), dict)


# Convenience accessors
def auto_enabled() -> bool:
    """Automatic refinement is on by default, but never on an unreadable config.

    The default is ``True`` so refinement works right after install. That default
    may only apply when the config was actually readable: defaulting to ``True``
    while the file cannot be parsed would silently override an explicit
    ``auto_enabled: false`` the user did set, and resume model-bound trajectory
    analysis they had turned off. An unreadable config therefore fails closed,
    and ``/refine status`` reports that as the reason.
    """
    return _get_fail_closed_bool("auto_enabled")


def auto_min_messages() -> int:
    return get_int("auto_min_messages", 15, min_val=5)


def auto_turn_interval() -> int:
    """Assistant turns between automatic refine attempts; zero disables it."""
    return get_int("auto_turn_interval", 25, min_val=0)


def auto_cooldown_minutes() -> int:
    """Minimum elapsed time between durable automatic-attempt records."""
    return get_int("auto_cooldown_minutes", 20, min_val=0)


def max_edits_per_run() -> int:
    return get_int("max_edits_per_run", 1, min_val=1)


def max_edits_per_proposal() -> int:
    """Maximum inseparable edits one proposal may apply as a single transaction."""
    return get_int("max_edits_per_proposal", 3, min_val=1)


def max_edits_per_day() -> int:
    return get_int("max_edits_per_day", 3, min_val=1)


def only_agent_created() -> bool:
    return get_bool("only_agent_created", True)


def min_pattern_count() -> int:
    """How many times a failure must repeat before it counts as a signal."""
    return get_int("min_pattern_count", 2, min_val=1)


def min_signal_required() -> bool:
    """Skip the LLM call entirely when nothing repeated and nothing was corrected."""
    return get_bool("min_signal_required", True)


def reviewer_fallback_enabled() -> bool:
    """Allow a small reviewer call when the mechanical signal gate finds nothing.

    Fails closed on an unreadable config, matching ``auto_enabled``: a manual
    ``/refine`` bypasses the ``auto_enabled`` gate entirely, so without this
    check a YAML syntax error would silently re-enable a reviewer model call
    the user may have turned off, rather than reporting the config problem.
    """
    return _get_fail_closed_bool("reviewer_fallback_enabled")


def reviewer_min_messages() -> int:
    """Minimum session size before a reviewer fallback may run."""
    return get_int("reviewer_min_messages", 20, min_val=3)


def reviewer_cooldown_minutes() -> int:
    """Minimum gap between durable reviewer decisions."""
    return get_int("reviewer_cooldown_minutes", 60, min_val=0)


def cross_session_enabled() -> bool:
    """Aggregate failures across recent sessions.

    Fails closed on an unreadable config, matching ``auto_enabled``: without
    this check, a YAML syntax error would silently re-enable cross-session
    aggregation for a user who had explicitly turned it off for privacy, and a
    manual ``/refine`` run bypasses ``auto_enabled`` entirely so that gate does
    not cover this path either.
    """
    return _get_fail_closed_bool("cross_session_enabled")


def skip_session_sources() -> List[str]:
    """Session sources to skip for automatic and manual refinement.

    A session whose ``source`` column matches one of these values is not
    analysed. Intended for machine-generated sessions (cron, batch) whose
    trajectory is noise rather than signal. Invalid config (not a list of
    strings) falls back to the default rather than raising.
    """
    entry = _get_refine_entry()
    val = entry.get("skip_session_sources")
    if isinstance(val, list):
        sources: List[str] = []
        for index, item in enumerate(val):
            text, _issue = _coerce_string_config_value(
                item, f"skip_session_sources[{index}]"
            )
            if text.strip():
                sources.append(text.strip().lower())
        return sources
    if val is not None:
        logger.warning("Config key 'skip_session_sources' must be a list; using default")
    return ["cron"]


def cross_session_days() -> int:
    return get_int("cross_session_days", 7, min_val=1)


def cross_session_max_sessions() -> int:
    return get_int("cross_session_max_sessions", 25, min_val=1)


def cross_session_max_rows() -> int:
    """Maximum trajectory rows scanned by an interactive refinement pass."""
    return get_int("cross_session_max_rows", 4000, min_val=1)


def dedup_window_days() -> int:
    """Refuse a proposal identical to one already applied within this window."""
    return get_int("dedup_window_days", 7, min_val=1)


def overview_max_entries() -> int:
    """Maximum existing entries of each kind included in a proposal prompt."""
    return get_int("overview_max_entries", 40, min_val=1)


def overview_max_chars() -> int:
    """Maximum characters in each structured overview line."""
    return get_int("overview_max_chars", 240, min_val=1)


def history_max_entries() -> int:
    """Maximum prior create/patch outcomes included in a proposal prompt."""
    return get_int("history_max_entries", 20, min_val=1)


def _llm_entry() -> Dict[str, Any]:
    block = _get_refine_entry().get("llm")
    return block if isinstance(block, dict) else {}


def _llm_string(key: str) -> Tuple[str, str]:
    value, issue = _coerce_string_config_value(_llm_entry().get(key), f"llm.{key}")
    return value.strip(), issue


def llm_provider() -> str:
    """Provider to request for refine's own calls; empty means host default."""
    return _llm_string("provider")[0]


def llm_model() -> str:
    """Model to request for refine's own calls; empty means host default.

    Unset, Hermes resolves refine's model through its ``auto`` path, which
    prefers the live main model. Pinning makes the target deterministic and
    immune to the host's auxiliary client cache keeping an older model. The
    host still gates it: without ``allow_model_override`` (and
    ``allow_provider_override``) the request is refused rather than applied.
    """
    return _llm_string("model")[0]


def llm_allow_model_override() -> bool:
    """Whether the trust policy allows refine to request a specific model."""
    return _parse_bool(
        _llm_entry().get("allow_model_override"), False, "llm.allow_model_override"
    )


def llm_allow_provider_override() -> bool:
    """Whether the trust policy allows refine to request a specific provider."""
    return _parse_bool(
        _llm_entry().get("allow_provider_override"), False, "llm.allow_provider_override"
    )


def llm_target_trust_denials(target: Dict[str, Any]) -> Dict[str, str]:
    """Explain every explicit target field that the host trust policy drops.

    Command and config targets are deliberately not sent unless their matching
    allow flag is enabled. Keep these messages here because status reports the
    same dropped fields that a refinement run journals.
    """
    if target.get("source") not in ("command", "config"):
        return {}
    denials: Dict[str, str] = {}
    model = str(target.get("model", "") or "")
    provider = str(target.get("provider", "") or "")
    if model and not llm_allow_model_override():
        denials["model"] = (
            f"Model {model} is set but host trust denies model overrides, so it is "
            "dropped before the call; set "
            "plugins.entries.refine.llm.allow_model_override to apply it"
        )
    if provider and not llm_allow_provider_override():
        denials["provider"] = (
            f"Provider {provider} is set but host trust denies provider overrides, "
            "so it is dropped before the call; set "
            "plugins.entries.refine.llm.allow_provider_override to apply it"
        )
    return denials


def live_main_target() -> Dict[str, str]:
    """Best-effort read of the host's live main provider/model.

    Uses a private Hermes API (``_read_main_provider`` / ``_read_main_model`` in
    ``agent.auxiliary_client``) because the host exposes no public accessor. Both
    names were confirmed present in a real installation; a private name can still
    move, so the import is guarded and yields no live value on failure. The caller
    must not treat that as an error — it only means no live model is available,
    and ``effective_llm_target`` then reports ``host_default`` instead of naming a
    target it cannot confirm.
    """
    try:
        from agent.auxiliary_client import _read_main_provider, _read_main_model

        provider = _read_main_provider()
        model = _read_main_model()
        result: Dict[str, str] = {}
        if provider:
            result["provider"] = provider
        if model:
            result["model"] = model
        return result
    except Exception:
        return {}


def effective_llm_target() -> Dict[str, Any]:
    """Resolve one effective model/provider target for refine.

    Priority:
      1. Command override (``/refine model <target>``)
      2. Config (``plugins.entries.refine.llm.model`` / ``.provider``)
      3. Live Hermes main model (best-effort, internal API)
      4. Nothing — let the host decide

    A command override fills any field it leaves unset from the config, because a
    configured provider is an explicit instruction the command did not revoke;
    dropping it would send the new model to whatever default the host picks.

    An unset field is deliberately **not** filled from the live model. Omitting a
    field already means "let Hermes resolve it", and Hermes resolves it to the
    live value — naming it here would claim the user chose something they did not,
    and would spend a trust flag to reach the same result.

    A configured ``llm.model`` may be namespaced (``vendor/name``); a provider may
    not. Both are checked against the same rule the override store applies, so a
    value refused from ``/refine model`` cannot slip in through ``config.yaml``.

    Returns ``{"provider": ..., "model": ..., "source": ..., "issues": [...]}``.
    Provider/model may be empty strings; ``source`` is always set; ``issues`` lists
    every configured or stored value that was discarded, and why. It is a list
    rather than one string because several can fail at once, and reporting only
    the last would have the user fix one and rediscover the next.
    """
    try:
        from . import journal
    except ImportError:
        import journal  # type: ignore

    issues: list = []
    cfg_provider, provider_type_issue = _llm_string("provider")
    cfg_model, model_type_issue = _llm_string("model")
    issues.extend(issue for issue in (provider_type_issue, model_type_issue) if issue)
    # The override store refuses unusable values. The config writes the same
    # field, so it meets the same rule here — otherwise the check would hold on
    # one path and not the other, and a value refused from /refine model would be
    # accepted from config.yaml. The reason is reported without the value, which
    # may be a pasted credential.
    for key, value, namespaced in (
        ("provider", cfg_provider, False),
        ("model", cfg_model, True),
    ):
        problem = (
            journal.model_override_field_problem(value, allow_namespace=namespaced)
            if value
            else ""
        )
        if problem:
            issues.append(f"config llm.{key} was ignored because {problem}")
            if key == "provider":
                cfg_provider = ""
            else:
                cfg_model = ""

    # 1. Command override, with the config filling any field it leaves unset.
    override, state = journal.read_model_override_state()
    if state == "rejected":
        issues.append("model_override.json is present but unusable, so it was ignored")
    elif state == "unreadable":
        issues.append("model_override.json could not be read, so it was not applied")
    if override:
        return {
            "provider": override.get("provider", "") or cfg_provider,
            "model": override.get("model", "") or cfg_model,
            "source": "command",
            "issues": issues,
        }

    # 2. Config
    if cfg_provider or cfg_model:
        return {
            "provider": cfg_provider,
            "model": cfg_model,
            "source": "config",
            "issues": issues,
        }

    # 3. Live Hermes main model. Not put through the rule above: the value comes
    # from the host's own runtime, not from user input, and refusing the host's
    # answer would leave refine with no target at all.
    live = live_main_target()
    if live.get("model") or live.get("provider"):
        return {
            "provider": live.get("provider", ""),
            "model": live.get("model", ""),
            "source": "live",
            "issues": issues,
        }

    # 4. Nothing
    return {"provider": "", "model": "", "source": "host_default", "issues": issues}


def _set_runtime_journal_dir(
    path: Optional[Path], *, commit_marker: Optional[Path] = None
) -> None:
    """Select a fallback store until another process publishes its successor."""
    global _RUNTIME_JOURNAL_DIR, _RUNTIME_JOURNAL_COMMIT_MARKER
    _RUNTIME_JOURNAL_DIR = Path(path) if path is not None else None
    _RUNTIME_JOURNAL_COMMIT_MARKER = (
        Path(commit_marker) if commit_marker is not None else None
    )


def journal_dir() -> Path:
    global _RUNTIME_JOURNAL_DIR, _RUNTIME_JOURNAL_COMMIT_MARKER
    default = hermes_home() / "refine"
    configured = get_str("journal_dir", "").strip()
    if configured:
        resolved = _resolve_hermes_home_placeholder(configured)
        return Path(os.path.expandvars(os.path.expanduser(resolved)))
    if _RUNTIME_JOURNAL_DIR is not None:
        marker = _RUNTIME_JOURNAL_COMMIT_MARKER
        if marker is not None and marker.is_file():
            # A different process completed the migration after this one fell
            # back. Switch before the next path is resolved; do not recreate the
            # renamed legacy directory.
            _RUNTIME_JOURNAL_DIR = None
            _RUNTIME_JOURNAL_COMMIT_MARKER = None
            return marker.parent
        return _RUNTIME_JOURNAL_DIR
    return default


def legacy_journal_dir() -> Path:
    """The old default before Part C moved runtime data out of the plugin dir."""
    return hermes_home() / "plugins" / "refine"


def prompt_notes_enabled() -> bool:
    """Whether refine may persist and inject plugin-owned prompt notes.

    Fails closed on an unreadable config, matching ``auto_enabled``: without
    this check, a YAML syntax error would silently re-enable prompt-note
    creation and injection for a user who had turned the feature off, and a
    manual ``/refine`` run bypasses ``auto_enabled`` entirely so that gate does
    not cover this path either.
    """
    return _get_fail_closed_bool("prompt_notes_enabled")


def prompt_notes_max_count() -> int:
    """Maximum prompt notes injected into one LLM call."""
    return get_int("prompt_notes_max_count", 5, min_val=1)


def prompt_notes_max_chars() -> int:
    """Maximum characters for one complete rendered prompt-note block."""
    return get_int("prompt_notes_max_chars", 600, min_val=1)


def audit_recurrence_horizon_days() -> int:
    """Days of post-edit silence after which 'no recurrence' means something.

    Measured, not guessed: across the reference install's journal, the median
    inter-recurrence gap of a chronic error fingerprint is minutes and the 95th
    percentile is 2.17 days; per-fingerprint maximum quiet gaps exceed 3 days
    for only ~19% of chronic fingerprints. A failure absent for this long is
    therefore more likely fixed than paused. Deliberately separate from
    ``unused_skills``' 14-day age gate: an unused skill for 3 days is normal,
    a chronic failure silent for 3 days is a signal. Different questions.

    Accepts the README-documented ``recurrence_horizon_days`` as an alias so an
    operator who follows the docs is not silently given the default. The
    explicit ``audit_recurrence_horizon_days`` still wins when both are set.

    Which key to read and the value itself come from one snapshot: choosing on
    a first load and reading on a second lets config.yaml change in between,
    so the branch and the lookup disagree and a valid value collapses to the
    default. ``/refine audit`` also calls this once per journal row, and a
    cached ``load_config()`` hit is not free (~265us, half of it a defensive
    deepcopy), so the second load was paid per row as well.
    """
    entry = _get_refine_entry()
    key = (
        "audit_recurrence_horizon_days"
        if "audit_recurrence_horizon_days" in entry
        else "recurrence_horizon_days"
    )
    return _int_from_entry(entry, key, 3, min_val=1)


def proposer_subagent_enabled() -> bool:
    """Whether refine may produce proposals through a read-only subagent.

    The subagent path lets the proposer open skill bodies (skills_list/
    skill_view) before deciding, instead of judging from name+description
    only. It requires a bound parent turn; when none exists the structured
    call is used as the fallback either way, so this switch only governs
    the preferred path, not availability.
    """
    return _get_fail_closed_bool("proposer_subagent_enabled")


def proposer_subagent_timeout_seconds() -> int:
    """How long the explicit-path refine_run waits on the proposer subagent.

    Bound on the synchronous wait; on timeout the child is cancelled and the
    structured fallback is used. The automatic path never waits (the launch
    itself fails without a bound parent turn).
    """
    return get_int("proposer_subagent_timeout_seconds", 180, min_val=5)


def proposer_subagent_strict() -> bool:
    """Whether a subagent launch failure may fall back to the structured path.

    Default ``false``: in production a quiet fall back to the working path is
    correct — the subagent is the preferred arm, not an availability gate.
    When ``true`` (measurement mode), any subagent failure — no bound
    lifecycle, launch refused, timeout, unparsable answer — becomes an error
    outcome instead of a silent structured run. A measurement that quietly
    downgrades its subagent arm to the structured path is contaminated: the
    twelve local Phase 0 slices were structured while labelled subagent for
    exactly this reason.
    """
    return _get_fail_closed_bool("proposer_subagent_strict", default=False)


def prompt_notes_default_scope() -> str:
    """Default lifetime for new prompt notes; invalid values fail closed to global."""
    scope = get_str("prompt_notes_default_scope", "global").strip().lower()
    return scope if scope in ("global", "session") else "global"
