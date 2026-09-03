#!/usr/bin/env python3
"""Refine-Cycle installer: plugin + invocation-bound Hermes host capability.

Single cross-platform entry point (Python 3 standard library only).
install.sh / install.ps1 are thin wrappers that exec this file.

Usage:
  python install.py                 # install plugin + apply host patch
  python install.py --patch-only    # verify/apply only the host capability
  python install.py --plugin-only   # install only the plugin files
  python install.py --rollback      # restore host + remove plugin (per metadata)
  python install.py --status        # report detected state, change nothing
  python install.py --hermes-src PATH  # point at a specific Hermes checkout

Supported Hermes base: any clean checkout that one of the bundled route patches
(assets/invocation-route-*.patch) applies to, decided by trying each with
`git apply --check` rather than by pinning a commit -- a version test refuses
hosts a patch fits and accepts hosts it does not. The installer classifies the
host as stock / patched / partial / dirty / incompatible and refuses anything it
cannot handle instead of half-applying. Every mutation is preceded by a backup
recorded in rollback metadata.

The install also raises the Hermes memory budget from the stock 2200 characters to
4400, in config.yaml and in the host's config_defaults.py, because refine writes
into that store and the stock size was set for weaker models. Only a value still
at exactly the stock default is moved -- an operator's own number is never
overwritten -- and --rollback reverses the same one-number substitution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
METADATA_NAME = "refine-install-metadata.json"
# Informational only. This is the base the first bundled patch was cut against;
# it is NOT a gate. Which bundled patch fits a given host is decided by trying
# each one with `git apply --check`, not by comparing this string to HEAD --
# Hermes moved 72 commits across the patch targets between v2026.8.16 and
# v2026.8.31, yet the 8.16 patch still lands 39 of its 40 hunks on 8.31, so a
# version test refuses hosts a patch fits and accepts hosts it does not. The
# gate this constant used to drive made a fresh install on 8.31 impossible.
EXPECTED_BASE_PREFIX = "df4b6514"
PATCHED_MARKER = "plugin_invocation_scope"

# Bundled route patches live here; each is cut against one Hermes base but may
# apply to others. Selection is by applicability (git apply --check), newest
# base first, so the most specific patch that fits wins.
PATCH_DIR = PLUGIN_DIR / "assets"
PATCH_GLOB = "invocation-route-*.patch"

# The nine files the patch touches (relative to the Hermes checkout root).
PATCH_FILES = [
    "agent/auxiliary_client.py",
    "agent/plugin_llm.py",
    "agent/turn_context.py",
    "cli.py",
    "gateway/run.py",
    "hermes_cli/plugins.py",
    "run_agent.py",
    "tui_gateway/methods_tools.py",
]
PATCH_TEST_FILE = "tests/agent/test_plugin_invocation_route.py"
ALL_PATCH_CONTENT = PATCH_FILES + [PATCH_TEST_FILE]


def _patch_sort_key(path: Path) -> tuple[int, ...]:
    """Numeric ordering of a patch's version. A lexical sort is wrong here.

    The names carry a dotted version (``invocation-route-v2026.8.31.patch``), and
    string order puts ``v2026.10.1`` before ``v2026.9.2`` because '1' < '9'.
    Sorting on the extracted integers keeps 10 after 9.
    """
    return tuple(int(n) for n in re.findall(r"\d+", path.name))


def patch_candidates() -> list[Path]:
    """Bundled route patches, newest base first.

    Newest first so the most specific patch that fits a host wins over an older
    one that also happens to apply -- e.g. on real v2026.8.31 both the 8.16 and
    the 8.31 patch check clean, and the 8.31 one is the right choice.
    """
    return sorted(PATCH_DIR.glob(PATCH_GLOB), key=_patch_sort_key, reverse=True)


def _git_apply_checks(src: Path, patch: Path, *reverse: str) -> bool:
    """True when `git apply --check [reverse] <patch>` succeeds (mutates nothing)."""
    r = run_git(src, "apply", "--check", *reverse, str(patch))
    return r.returncode == 0


def select_patch(src: Path) -> Path | None:
    """The newest bundled patch that applies cleanly to ``src``, or None.

    Decided by trying, not by pinning a commit: this is what makes a clean host
    on any base the patch fits installable, and a host the patch does not fit
    honestly refused.
    """
    for patch in patch_candidates():
        if _git_apply_checks(src, patch):
            return patch
    return None


def select_reverse_patch(src: Path) -> Path | None:
    """The newest bundled patch that reverses cleanly out of ``src``, or None.

    The reverse of a partially/fully applied host: the caller does not know which
    bundled patch was applied, so the one that reverse-checks clean is it. Newest
    first for the same reason as select_patch -- prefer the more specific base.
    """
    for patch in patch_candidates():
        if _git_apply_checks(src, patch, "-R"):
            return patch
    return None

# Top-level packages that belong to the Hermes host, not to the plugin. An
# unresolved one of these during import verification means this environment
# cannot see Hermes, which is not an incomplete install.
HOST_IMPORT_ROOTS = (
    "agent", "gateway", "hermes_cli", "tui_gateway", "hermes_constants",
)

# Per-file applied-markers: each file gets a symbol only the patched version defines.
FILE_MARKERS = {
    "agent/auxiliary_client.py": "_call_route_locked_once",
    "agent/plugin_llm.py": "bind_invocation",
    "agent/turn_context.py": "set_plugin_invocation_agent",
    "cli.py": "plugin_invocation_scope_for_agent",
    "gateway/run.py": "plugin_invocation_scope",
    "hermes_cli/plugins.py": "plugin_invocation_scope",
    "run_agent.py": "plugin_invocation_scope",
    "tui_gateway/methods_tools.py": "plugin_invocation_scope_for_agent",
}

# What gets installed is read from the checkout, not listed here. This list used
# to be hardcoded and it drifted: notify.py and refine_trace.py joined the plugin
# and never joined the list, so a fresh install copied a tree whose core.py
# raised ModuleNotFoundError on import -- a plugin that could not load at all,
# while the installer printed SUCCESS. The asymmetry decides the rule: an extra
# file costs a few KB, a missing one costs the whole plugin.
PLUGIN_MANIFEST_EXTRAS = ("plugin.yaml",)

# core.py imports every one of these at module level, so the absence of any one
# of them is a dead install. Derivation should cover them already; this is the
# guard for derivation itself going wrong.
REQUIRED_PLUGIN_MODULES = (
    "__init__.py", "config.py", "core.py", "journal.py", "ledger.py", "llm.py",
    "notify.py", "patterns.py", "refine_trace.py", "sanitization.py",
    "plugin.yaml",
)


def _is_shipped(p: Path) -> bool:
    """True for a repo file that belongs in an install.

    Skips scratch and probe files (a leading underscore) while keeping dunders,
    so ``__init__.py`` ships and ``_probe_whatever.py`` does not.
    """
    return p.is_file() and (not p.name.startswith("_") or p.name.startswith("__"))


def plugin_files() -> list[str]:
    """Every file to copy into the installed plugin, derived from the checkout."""
    rels = [p.name for p in sorted(PLUGIN_DIR.glob("*.py")) if _is_shipped(p)]
    rels += [n for n in PLUGIN_MANIFEST_EXTRAS if (PLUGIN_DIR / n).is_file()]
    rels += [
        f"tests/{p.name}"
        for p in sorted((PLUGIN_DIR / "tests").glob("*.py"))
        if _is_shipped(p)
    ]
    # The route patches travel with install.py, because install.py travels with
    # the plugin. The SUCCESS banner tells the operator to run
    # `python install.py --rollback` from the installed tree, and a re-install or
    # --patch-only from there needs the patches. Without them patch_candidates()
    # is empty and the installer refuses its own host with "none bundled" --
    # measured on a real clean install, where the shipped suite also failed for
    # the same reason. A few KB against an installer that cannot install.
    rels += [f"assets/{p.name}" for p in sorted(PATCH_DIR.glob(PATCH_GLOB))]
    return rels


def hermes_home_dir(src: Path | None = None) -> Path:
    """The Hermes data directory, resolved the way the plugin itself resolves it.

    Installing into a different directory than the one the plugin reads is the
    defect that made this plugin inert on Windows: Hermes stores its data in
    %LOCALAPPDATA%\\hermes, and an installer that hardcodes ~/.hermes copies the
    files somewhere nothing ever looks -- silently, exit 0. So an explicit
    HERMES_HOME wins (a user pointing somewhere is not to be second-guessed) and
    everything else goes through config.hermes_home(), the one place this project
    resolves Hermes paths.

    ``src`` is the Hermes checkout, and it goes on sys.path for the duration:
    config.hermes_home() asks the HOST helper ``hermes_constants`` first, and
    without the checkout importable that branch is unreachable from a standalone
    ``python install.py`` -- so the installer would silently use the generic
    fallback while the gateway used the host's answer. That is the same divergence
    in a new place. verify_plugin_imports already puts ``src`` on the subprocess
    PYTHONPATH for this reason.
    """
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    added = [str(p) for p in (src,) if p is not None]
    sys.path[:0] = added
    # Importing the host helper out of the checkout writes
    # <checkout>/__pycache__/hermes_constants.*.pyc otherwise -- measured on a real
    # 8.31 worktree. Reading the host's answer must not modify the host.
    dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_refine_install_config", PLUGIN_DIR / "config.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return Path(module.hermes_home())
    except Exception:
        # config.py ships beside this file, so reaching here means the plugin
        # tree is broken; resolve the way config would have without the host.
        if os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            if local_app_data:
                return Path(local_app_data) / "hermes"
        return Path(os.path.expanduser("~/.hermes"))
    finally:
        sys.dont_write_bytecode = dont_write
        for entry in added:
            if entry in sys.path:
                sys.path.remove(entry)


def _emit(stream, text: str) -> None:
    """Print what the console can render; never raise on the console's encoding.

    These messages carry '->' as U+2192 and '...' as U+2026. A legacy Windows
    console is cp1251/cp866 and can encode neither, so a plain print raises
    UnicodeEncodeError -- and the arrow line sits between copying the plugin
    files and writing the metadata, so that crash leaves a copied plugin with no
    metadata, after which --rollback reports "No rollback metadata found" and the
    copy stays behind. Degrade the text, never the install.
    """
    try:
        print(text, file=stream, flush=True)
        return
    except UnicodeEncodeError:
        pass
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        degraded = text.encode(enc, "replace").decode(enc, "replace")
    except (LookupError, UnicodeError):
        # A stream can name a codec Python does not have, or over-report what it
        # can encode. ASCII is the floor every console renders.
        degraded = text.encode("ascii", "replace").decode("ascii")
    try:
        print(degraded, file=stream, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), file=stream, flush=True)


def say(msg: str) -> None:
    _emit(sys.stdout, msg)


def fail(msg: str, code: int = 1) -> None:
    _emit(sys.stderr, f"ERROR: {msg}")
    sys.exit(code)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        timeout=60, encoding="utf-8", errors="replace",
    )


def find_hermes_src(explicit: str | None) -> Path:
    """Locate the active Hermes checkout without POSIX-only tools."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("HERMES_SRC")
    if env:
        candidates.append(Path(env))
    # systemd ExecStart of the user's gateway service, parsed portably
    for unit_dir in (
        Path("/etc/systemd/system"),
        Path.home() / ".config/systemd/user",
    ):
        unit = unit_dir / "hermes-gateway.service"
        try:
            text = unit.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # last ExecStart= line wins (drop-ins append overrides)
        for line in reversed(text.splitlines()):
            s = line.strip()
            if s.startswith("ExecStart=") and "=" in s:
                first = s.split("=", 1)[1].split()[0]
                # .../venv/bin/python|hermes -> walk up to the checkout root
                p = Path(first)
                for parent in p.parents:
                    if (parent / "hermes_cli" / "plugins.py").is_file():
                        candidates.append(parent)
                        break
                break
    # Common locations
    home = Path.home()
    for base in (home / "releases", home):
        if base.is_dir():
            try:
                entries = sorted(base.iterdir())
            except OSError:
                continue
            for e in entries:
                if "hermes" in e.name.lower() and (e / "hermes_cli" / "plugins.py").is_file():
                    candidates.append(e)
    for c in candidates:
        c = c.resolve()
        if (c / "hermes_cli" / "plugins.py").is_file():
            return c
    fail(
        "Cannot locate an active Hermes checkout. Set HERMES_SRC=/path/to/hermes-agent "
        "or pass --hermes-src."
    )


def git_head_short(repo: Path) -> str | None:
    r = run_git(repo, "rev-parse", "--short=10", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def applied_patch_files(src: Path) -> list[str]:
    """Patch targets that carry their own applied-marker, read off disk.

    The single source of truth for "is the host capability there", shared by
    classify_host and by the paths that report host state without classifying.
    Checking only hermes_cli/plugins.py answers a narrower question and calls a
    partially patched host healthy.
    """
    applied = []
    for rel in PATCH_FILES:
        f = src / rel
        if not f.is_file():
            continue
        body = f.read_text(encoding="utf-8", errors="replace")
        if FILE_MARKERS.get(rel, PATCHED_MARKER) in body:
            applied.append(rel)
    return applied


def classify_host(src: Path) -> tuple[str, str]:
    """Return (state_class, detail). States: stock, patched, partial, dirty, incompatible.

    "stock" no longer means "the base commit we pinned"; it means "a clean tree
    that some bundled patch applies to". The commit id is not a gate -- a version
    test refuses hosts the patch fits (fresh v2026.8.31 was impossible) and
    accepts hosts it does not. Applicability is decided by trying each bundled
    patch with `git apply --check`.

    Order matters. The dirty check runs BEFORE applicability: a user's
    uncommitted edit to a patch target can itself make `git apply --check` fail,
    and reporting that as "no patch fits this host" sends them chasing a version
    problem they do not have. So: not-a-checkout -> patched -> partial ->
    marker-outside-expected-files -> dirty -> select_patch -> refuse.
    """
    head = git_head_short(src)
    plugins_py = src / "hermes_cli" / "plugins.py"
    text = plugins_py.read_text(encoding="utf-8", errors="replace")
    has_marker = PATCHED_MARKER in text
    applied_count = len(applied_patch_files(src))
    if head is None:
        return ("incompatible", "not a git checkout; refusing to patch blind")
    if has_marker and applied_count == len(PATCH_FILES):
        return ("patched", f"all {len(PATCH_FILES)} files carry the marker at {head}")
    if 0 < applied_count < len(PATCH_FILES):
        return ("partial", f"{applied_count}/{len(PATCH_FILES)} files patched at {head}")
    if has_marker and applied_count == 0:
        return ("incompatible", f"marker found only outside expected files at {head}")
    # Unpatched base. Is the tree clean enough to patch? This runs
    # unconditionally now -- there is no base to gate it on.
    st = run_git(src, "status", "--short", "--", *PATCH_FILES)
    if st.returncode == 0 and st.stdout.strip():
        touched = [l.split()[-1] for l in st.stdout.splitlines()]
        return ("dirty", f"user-modified patch targets before install: {touched}")
    # Clean and unpatched: installable only if some bundled patch actually fits.
    chosen = select_patch(src)
    if chosen is not None:
        return ("stock", f"clean base {head}; {chosen.name} applies")
    tried = [p.name for p in patch_candidates()] or ["none bundled"]
    return (
        "incompatible",
        f"no bundled route patch applies to base {head}; tried: {tried}. "
        "The patch needs rebasing onto this host's version.",
    )


def backup_host(src: Path, meta_dir: Path) -> Path:
    """Zip the nine patch-target files (pre-mutation) into the metadata dir."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = meta_dir / f"host-backup-{stamp}.zip"
    # The stamp has one-second resolution and ZipFile("w") truncates, so two
    # backups inside the same second silently became one -- measured: a second
    # run's backup overwrote the first, and rollback then restored the
    # installer's own intermediate state instead of the user's.
    counter = 1
    while backup_path.exists():
        backup_path = meta_dir / f"host-backup-{stamp}-{counter}.zip"
        counter += 1
    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rel in PATCH_FILES:
            f = src / rel
            if f.is_file():
                z.write(f, arcname=rel)
    return backup_path


def record_host_backup(src: Path, meta_dir: Path, meta: dict) -> None:
    """Back up the patch targets and persist the pointer BEFORE mutating the host.

    Every failure between a host mutation and the end of the run used to leave a
    host nothing could undo: the zip was on disk, no metadata referenced it, and
    --rollback answered "No rollback metadata found; nothing to roll back". Both
    mutating paths -- the reverse of a partial host and the patch apply itself --
    now record first, so the fail() that follows is recoverable. Measured before
    this: a partial host whose apply failed marker verification lost the user's
    three patched files, wrote two orphan zips, and denied anything had happened.
    """
    previous = dict(meta.get("host") or {})
    fresh = str(backup_host(src, meta_dir))
    # Computed before the mutation: afterwards they exist. Unioned with what an
    # earlier run recorded, or a second run would drop the file the first one
    # created and rollback would leave it on the host.
    created = set(previous.get("created_files") or [])
    created |= {rel for rel in ALL_PATCH_CONTENT if not (src / rel).is_file()}

    keep = previous.get("backup") if previous.get("backup") and Path(previous["backup"]).is_file() else None
    host = {
        # The FIRST backup stays the restore target: it holds the state the user
        # actually had before any of our runs. A later run's zip holds whatever
        # this installer had already done to the host, and restoring that would
        # reinstate an intermediate state while claiming a faithful undo.
        "backup": keep or fresh,
        "created_files": sorted(created),
        # Stamped when the backup is taken, not refreshed per run: the top-level
        # base_head moves with every invocation, so a --plugin-only run on an
        # upgraded host used to make an old backup look current and silence the
        # moved-on warning entirely.
        "base_head": previous.get("base_head") or git_head_short(src),
    }
    if keep and fresh != keep:
        host["extra_backups"] = sorted((previous.get("extra_backups") or []) + [fresh])
    meta["host"] = host
    write_metadata(meta_dir, meta)


def check_patch_applies(src: Path, patch: Path) -> None:
    """Refuse a patch that cannot apply, before anything is written anywhere.

    `git apply --check` mutates nothing, so it belongs ahead of the backup: a run
    that is going to refuse should not leave a zip and a metadata document behind
    for a host it never touched. Measured on a real 8.31 tree, where an untracked
    leftover made the patch unappliable and the run still wrote both.
    """
    r = run_git(src, "apply", "--check", str(patch))
    if r.returncode != 0:
        fail(f"patch does not apply cleanly; aborting without changes.\n{r.stderr}")


def apply_patch(src: Path, meta_dir: Path, patch: Path) -> None:
    """Apply the chosen route patch, having already recorded a way back.

    git is already required (we classified via HEAD); `git apply` is used on
    every platform the host itself supports. Files are written through temp +
    rename by git, keeping the mutation atomic per file. The caller must have
    called record_host_backup first -- every fail() below leaves host files
    changed, and the recorded backup is what makes them recoverable.

    ``patch`` is threaded in rather than read from a module global: the
    applicable patch is chosen per host (select_patch), and --check, the apply,
    and this function's own failure-reversal must all use the SAME one. A stale
    global here reversed a different patch than it applied.
    """
    check_patch_applies(src, patch)
    r = run_git(src, "apply", str(patch))
    if r.returncode != 0:
        fail(
            "patch application failed mid-way; the host may be half-patched. "
            f"Restore it with `python install.py --rollback` (backup recorded in "
            f"{meta_dir / METADATA_NAME}).\n{r.stderr}"
        )
    missing = [rel for rel in PATCH_FILES if rel not in applied_patch_files(src)]
    if missing:
        rb = run_git(src, "apply", "-R", str(patch))
        left = applied_patch_files(src)
        # Read the markers back rather than reporting git's exit code as if it
        # were the outcome: "patch reversed rc=0" was a claim about the host that
        # nothing had checked.
        fail(
            f"verification failed ({missing}); attempted reverse rc={rb.returncode}, "
            f"{len(left)}/{len(PATCH_FILES)} files still carry the marker. "
            f"`python install.py --rollback` restores the recorded backup."
        )


def compile_all(src: Path) -> None:
    """Byte-compile the patched host files to prove they parse.

    Into a temp directory, not beside the sources: the host tree is the user's,
    and __pycache__ entries left in it that --rollback does not remove contradict
    "host restored byte-for-byte from backup". Measured after --patch-only plus
    --rollback on a throwaway host: 8 .pyc across 5 __pycache__ directories still
    there, with the run reporting a faithful restore.
    """
    import py_compile

    with tempfile.TemporaryDirectory(prefix="refine-compile-") as td:
        for index, rel in enumerate(PATCH_FILES):
            py_compile.compile(
                str(src / rel), cfile=str(Path(td) / f"{index}.pyc"), doraise=True
            )


def python_of(src: Path) -> str:
    """Pick the interpreter that runs this Hermes checkout."""
    for cand in (
        src / ".venv" / "bin" / "python",
        src / ".venv" / "Scripts" / "python.exe",
        src / ".venv" / "Scripts" / "python",
    ):
        if cand.is_file():
            return str(cand)
    return sys.executable


def plugin_dest_for(src: Path | None = None) -> Path:
    """Where the plugin goes. Known before any copying, so it can be recorded first."""
    return hermes_home_dir(src) / "plugins" / "refine"


def remove_recorded_plugin_files(dest: Path, recorded) -> tuple[list[str], list[str]]:
    """Delete only the files install_plugin recorded copying. Returns (removed, kept).

    An rmtree of ``dest`` is not a rollback of this installer's work, because
    ``dest`` is not exclusively this installer's. ``plugin_dest_for()`` returns
    ``<hermes home>/plugins/refine`` and ``config.legacy_journal_dir()`` returns
    the identical path, so on a host whose runtime data has not migrated, the
    destination also holds ``refine_journal.jsonl``, ``backups/`` and
    ``skill_stats.json``. Removing those deletes the pre-edit backups Invariant 5
    guarantees and the journal that names them -- refine destroying the record of
    its own edits while printing "Plugin removed" and exiting 0. Whatever else the
    operator keeps there goes too.

    ``meta["plugin_files"]`` is written by install_plugin for exactly this, so
    nothing has to be guessed. Only paths inside ``dest`` are touched: a record
    carried forward from a run under a different HERMES_HOME names files this
    rollback is not removing, and deleting outside the destination it just
    reported is not a thing a rollback should do quietly.
    """
    removed: list[str] = []
    kept: list[str] = []
    dest_key = os.path.normcase(str(dest))
    parents: set[Path] = set()
    for entry in recorded or []:
        target = Path(str(entry))
        key = os.path.normcase(str(target))
        if key != dest_key and not key.startswith(dest_key + os.sep):
            kept.append(str(target))
            continue
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
            removed.append(str(target))
            parents.add(target.parent)
        except OSError:
            # Windows with a running gateway holding the module open is the usual
            # cause. A file left behind must keep the record that finds it.
            kept.append(str(target))
    # Deepest first, so a directory only becomes prunable after its children are.
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        current = parent
        while True:
            ckey = os.path.normcase(str(current))
            if ckey != dest_key and not ckey.startswith(dest_key + os.sep):
                break
            try:
                current.rmdir()
            except OSError:
                break  # not empty, or in use: leave it and everything above it
            current = current.parent
    return removed, kept


def install_plugin(meta: dict, src: Path | None = None) -> str:
    """Copy plugin files into <hermes home>/plugins/refine (idempotent)."""
    dest = plugin_dest_for(src)
    files = plugin_files()
    absent = [m for m in REQUIRED_PLUGIN_MODULES if m not in files]
    if absent:
        fail(
            f"refusing to install an incomplete plugin: {absent} not found in "
            f"{PLUGIN_DIR}. Installing without them yields a plugin that cannot "
            "be imported at all."
        )
    copied = []
    for rel in files:
        s = PLUGIN_DIR / rel
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if s == d:
            continue
        shutil.copy2(s, d)
        copied.append(str(d))
    meta["plugin_dest"] = str(dest)
    meta["plugin_files"] = copied
    return str(dest)


def verify_plugin_imports(dest: Path, python: str, src: Path) -> str:
    """Import the tree that was just copied, using the interpreter that will load it.

    The pre-existing capability verification checks the HOST patch markers and
    never touches the copied plugin, which is exactly how an install missing
    notify.py could print SUCCESS while the plugin was unloadable. ``core``
    imports every sibling module at module level, so one import exercises the
    whole copy.

    A missing PLUGIN module is fatal, and so is a copy that does not parse:
    neither can come from anywhere but the copied tree, and both mean the plugin
    cannot load anywhere. A missing HOST module (``agent.*``, ``gateway.*``) is
    not fatal -- it means this environment cannot resolve Hermes the way the
    gateway does, not that the install is incomplete, and failing on it would
    refuse a good install. That distinction is why --plugin-only on an unpatched
    host still succeeds: an unpatched host genuinely cannot satisfy the import.

    What is not allowed is claiming to know which of the two it was. Anything
    that is neither a known plugin-module absence nor an identifiable host
    resolution failure is now reported as unclassified and NOT verified. The
    previous wording asserted "this is host resolution in this environment" for
    every failure it did not recognise -- including a syntax error in core.py,
    which it waved through with exit 0.
    """
    owned = sorted(
        Path(rel).stem for rel in plugin_files()
        if rel.endswith(".py") and "/" not in rel
    )
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(src)] + ([existing] if existing else []))
    # Importing host modules from the checkout writes agent/__pycache__/*.pyc into
    # it. Small, but --plugin-only promises not to write into the host tree, and a
    # promise with an asterisk is not one. Measured: the pyc appeared next to the
    # host sources on every plugin-only run.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run(
        [python, "-c", "import core"],
        cwd=str(dest), capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", env=env,
    )
    if r.returncode == 0:
        say("Plugin import verified in a fresh interpreter.")
        return "verified"
    stderr = r.stderr or ""
    tail = [line for line in stderr.strip().splitlines() if line.strip()]
    detail = tail[-1] if tail else "no stderr"
    def _unresolved(name: str) -> bool:
        # "No module named 'gateway'" and "No module named 'gateway.run'" are the
        # same finding; matching only the first form missed every submodule.
        return (
            f"No module named '{name}'" in stderr
            or f"No module named '{name}." in stderr
        )

    # Present on disk means the absence is not about our copy. The Hermes
    # checkout is on this subprocess's PYTHONPATH, so host code runs during
    # verification and a generic name it fails to import (``config`` is both a
    # plugin module and a common host one) must not be read as a broken copy.
    absent = [
        name for name in owned
        if _unresolved(name) and not (dest / f"{name}.py").is_file()
    ]
    if absent:
        fail(
            f"the installed plugin is missing its own module(s) {absent}; the copy "
            f"is incomplete and the plugin cannot load.\n  {detail}\n  tree: {dest}"
        )
    if "SyntaxError" in stderr or "IndentationError" in stderr:
        fail(
            "the installed plugin does not parse; the copy cannot load in any "
            f"environment.\n  {detail}\n  tree: {dest}"
        )
    host_absent = [name for name in HOST_IMPORT_ROOTS if _unresolved(name)]
    if host_absent:
        say(
            f"NOTE: could not fully import the plugin here ({detail}). The "
            f"unresolved name is a Hermes host module {host_absent}, so this is "
            "host resolution in this environment, not an incomplete install."
        )
        return "host-unresolved"
    say(
        f"NOTE: the plugin copy could NOT be imported here and the failure was not "
        f"classified ({detail}). No plugin module is missing and the copy parses, "
        "so it may still be fine -- but this run did not verify it. Check the tree "
        f"before relying on it: {dest}"
    )
    return "unclassified"


# ---------------------------------------------------------------------------
# Memory budget. Refine's whole purpose is writing lessons into the Hermes memory
# store, and the stock budget was sized for weaker models: 2200 characters, about
# 800 tokens. Measured on a real install, six applied edits filled a third of it
# in a day. A plugin that fills the store it depends on is not usable at the stock
# budget, so the install raises it.
#
# TWO files, because neither one alone reaches everybody. Hermes writes the key
# out into config.yaml from config_defaults, so an existing user's file already
# contains it and the code default is never consulted -- only the file matters. A
# user who installs the plugin before Hermes has ever generated a config has no
# file, so only the default matters. Doing one and not the other leaves half of
# all users at 2200.
#
# The rule is a FLOOR: anything below it comes up to it, anything at or above it is
# the operator's own number and is left exactly as it is. So a stock 2200 and a
# hand-set 3000 both become 4400, while 20000 stays 20000. One rule, three
# properties: a bigger budget is never reduced, the operation is idempotent, and
# nobody's deliberate choice is overwritten.
MEMORY_LIMIT_STOCK_DEFAULT = 2200
MEMORY_LIMIT_FLOOR = 4400
CONFIG_DEFAULTS_REL = "hermes_cli/config_defaults.py"

# Only the digits are replaced, so indentation, quoting and any trailing comment
# survive untouched.
_YAML_LIMIT_RE = re.compile(
    r"^(?P<lead>[ \t]*memory_char_limit[ \t]*:[ \t]*)(?P<value>\d+)",
    re.MULTILINE,
)
_DEFAULTS_LIMIT_RE = re.compile(
    r"(?P<lead>[\"']memory_char_limit[\"'][ \t]*:[ \t]*)(?P<value>\d+)"
)


def _limit_regex_for(path: Path) -> "re.Pattern[str]":
    return _YAML_LIMIT_RE if path.suffix in (".yaml", ".yml") else _DEFAULTS_LIMIT_RE


def _find_limit(path: Path) -> tuple[str, int, str, tuple[int, int]]:
    """Locate the single memory_char_limit value in ``path``.

    Returns ``(status, value, text, span)``. An empty status means exactly one
    value was found and the other three are usable; any other status is the reason
    the file cannot be touched, and the caller prints it. Nothing here raises: a
    silent skip on a user's live config is indistinguishable from success.

    Read with ``newline=""`` so the caller can write the file back verbatim.
    Without it, editing an LF config on Windows rewrites every line to CRLF and
    the diff the user sees is their whole config instead of one number.

    ``errors="surrogateescape"`` and not ``"replace"``, because the text read here
    is written back: config.yaml is the user's live file, holds their provider,
    model and keys, and by design has no backup. ``"replace"`` turns every byte
    that is not valid UTF-8 into U+FFFD, and _write_limit then persists those
    replacement characters over the user's actual bytes -- a permanent,
    unrecoverable edit to content this installer was never asked to touch, for a
    run whose only job is one number. Windows Notepad before 2019 saved ANSI by
    default, so a cp1251 comment in a config is not hypothetical.
    surrogateescape round-trips those bytes instead, and _write_limit restores
    them verbatim.
    """
    if not path.is_file():
        return "absent", 0, "", (0, 0)
    with open(
        path, "r", encoding="utf-8", errors="surrogateescape", newline=""
    ) as handle:
        text = handle.read()
    matches = list(_limit_regex_for(path).finditer(text))
    if not matches:
        return "key-absent", 0, text, (0, 0)
    if len(matches) > 1:
        # Two declarations mean the effective value is not knowable from a regex,
        # and guessing which one wins is how a config gets silently broken.
        return f"ambiguous ({len(matches)} occurrences)", 0, text, (0, 0)
    match = matches[0]
    return "", int(match.group("value")), text, (match.start("value"), match.end("value"))


def _write_limit(path: Path, text: str, span: tuple[int, int], value: int) -> None:
    """Replace just the digits, so indentation, quoting and comments survive.

    Paired with _find_limit's surrogateescape: any byte that was not valid UTF-8
    is carried in ``text`` as a lone surrogate and goes back out as the original
    byte. Both halves need the handler -- writing with the default strict codec
    would raise on the surrogate and abort the raise instead.
    """
    updated = text[: span[0]] + str(value) + text[span[1] :]
    with open(
        path, "w", encoding="utf-8", errors="surrogateescape", newline=""
    ) as handle:
        handle.write(updated)


def _raise_limit(
    path: Path,
    *,
    floor: int,
    before_write: Callable[[int], None] | None = None,
) -> tuple[str, int]:
    """Bring one integer up to ``floor`` when it is below it.

    The rule is a FLOOR, not an exact match: anything under ``floor`` comes up to
    it, anything at or above it is the operator's own number and is left as it is.
    So a stock 2200 and a hand-set 3000 both become 4400, while 20000 stays 20000.
    This can therefore never lower a limit, and running it twice changes nothing.

    Returns ``(status, previous_value)``. The previous value is what rollback
    restores -- assuming everyone started at the stock default would hand a user
    who had 3000 a 2200 they never chose.

    ``before_write`` is called with the previous value at the one moment that
    matters: after this function has decided it is going to write, and before it
    writes. That is where the way back has to be recorded -- see
    raise_memory_limit. It exists as a hook rather than as a second read at the
    call site because a read-then-decide-then-record-then-write split there would
    re-read the file and could disagree with what actually gets written.
    """
    status, value, text, span = _find_limit(path)
    if status:
        return status, 0
    if value == floor:
        return f"already {floor}", value
    if value > floor:
        return f"left alone (operator set {value}, above {floor})", value
    if before_write is not None:
        before_write(value)
    _write_limit(path, text, span, floor)
    return "raised", value


def _lower_limit(path: Path, *, expect: int, restore: int) -> str:
    """Put one integer back, but only if it is still the number we wrote."""
    status, value, text, span = _find_limit(path)
    if status:
        return status
    if value != expect:
        return f"left alone (now {value}, not the {expect} this install wrote)"
    if value == restore:
        return f"already {restore}"
    _write_limit(path, text, span, restore)
    return "restored"


def raise_memory_limit(src: Path, meta: dict, *, include_host: bool) -> None:
    """Bring the memory budget up to the floor, and record how to undo it.

    Merges into any record carried forward by new_metadata instead of replacing
    it. A repeat install finds the value already at the floor and would otherwise
    record "already 4400", erasing the one note that says this install raised the
    file from 2200 -- after which rollback leaves the raise in place forever.

    Each raise is recorded AND persisted before the file is written, not after the
    whole function returns. Two targets are written in sequence and the caller
    persists once at the end, so an OSError on the second used to leave the first
    raise on disk with nothing referencing it -- exactly the window 629a177 closed
    for the host patch. Recording early over-records rather than under-records,
    which is the safe direction: _lower_limit refuses to touch a value that is not
    the one we wrote, so a record for a write that never landed declines instead of
    clobbering.
    """
    meta_dir = metadata_dir(src)
    existing = (meta.get("memory_limit") or {}).get("targets") or {}
    # Seeded from the carried-forward record, not empty. A --plugin-only rerun
    # visits only config.yaml, and rebuilding the map from just the targets THIS
    # run touched deleted the host file's entry -- the same un-rollback-able host
    # 208ab6c fixed, reached by dropping the target instead of overwriting its
    # value. A target this run does not visit keeps whatever an earlier run
    # recorded; a target it does visit is overwritten below as before.
    targets: dict[str, dict] = dict(existing)
    # Published into meta before the first write, and mutated in place afterwards,
    # so what the pre-write persist below puts on disk is this same record.
    meta["memory_limit"] = {"floor": MEMORY_LIMIT_FLOOR, "targets": targets}
    raised_here: list[str] = []

    def apply_to(path: Path, label: str) -> str:
        prior = existing.get(str(path))
        keep_prior = isinstance(prior, dict) and prior.get("status") == "raised"

        def record_before_writing(previous: int) -> None:
            # The only moment this can be done safely: the decision to write is
            # made, the write has not happened. If the write then fails, we have
            # over-recorded, and _lower_limit declines a value it did not write.
            targets[str(path)] = {"status": "raised", "previous": previous}
            write_metadata(meta_dir, meta)

        status, previous = _raise_limit(
            path,
            floor=MEMORY_LIMIT_FLOOR,
            # An earlier install's record already names the original number and is
            # already on disk, so there is nothing to record before this write.
            before_write=None if keep_prior else record_before_writing,
        )
        if status == "raised":
            raised_here.append(str(path))
        if keep_prior:
            # An earlier run of this installer did the raise. That record is the
            # only thing that knows the original number, so it wins and this
            # run's "already 4400" is not allowed to overwrite it.
            targets[str(path)] = prior
            say(f"  {label}: {status} (raise recorded by an earlier install)")
            return status
        targets[str(path)] = {"status": status, "previous": previous}
        say(f"  {label}: {status}")
        return status

    apply_to(hermes_home_dir(src) / "config.yaml", str(hermes_home_dir(src) / "config.yaml"))
    if include_host:
        apply_to(src / CONFIG_DEFAULTS_REL, CONFIG_DEFAULTS_REL)
    else:
        # Said out loud: --plugin-only promises no writes into the host checkout,
        # and a silently skipped half leaves a future reader wondering why a
        # config generated later is still below the floor.
        say(
            f"  {CONFIG_DEFAULTS_REL}: skipped (--plugin-only writes nothing into "
            "the host checkout; a config generated later will still say "
            f"{MEMORY_LIMIT_STOCK_DEFAULT})"
        )
    # What THIS run raised, not what the record contains: the record is seeded from
    # earlier runs, so reading the message off it would announce a raise on a rerun
    # that changed nothing.
    if raised_here:
        say(
            f"Memory budget raised to {MEMORY_LIMIT_FLOOR} chars. Takes effect on "
            "the next gateway restart."
        )


def restore_memory_limit(meta: dict) -> None:
    """Undo the raise by putting each file's own previous number back.

    Deliberately NOT a file restore. config.yaml is a live file the user edits;
    putting a pre-install copy back would silently destroy every unrelated change
    made since. Reversing one integer cannot.

    Each target restores the value IT had, not a global default: a user who ran the
    install at 3000 gets 3000 back. And a value that is no longer the one this
    install wrote is left alone -- they changed it afterwards, and their number
    outranks our undo.
    """
    record = meta.get("memory_limit") or {}
    targets = record.get("targets") or {}
    floor = record.get("floor")
    if not isinstance(floor, int):
        return
    for path_str, entry in sorted(targets.items()):
        if not isinstance(entry, dict) or entry.get("status") != "raised":
            # We did not change this file, so rollback does not touch it.
            continue
        previous = entry.get("previous")
        if not isinstance(previous, int) or previous <= 0:
            say(f"  memory limit {path_str}: no previous value recorded; left alone")
            continue
        status = _lower_limit(Path(path_str), expect=floor, restore=previous)
        say(f"  memory limit {path_str}: {status}")


def write_metadata(meta_dir: Path, meta: dict) -> Path:
    meta_dir.mkdir(parents=True, exist_ok=True)
    path = meta_dir / METADATA_NAME
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_metadata(meta_dir: Path) -> dict | None:
    path = meta_dir / METADATA_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def metadata_dir(src: Path) -> Path:
    return src / ".refine-install"


def metadata_or_refuse(meta_dir: Path, *, purpose: str) -> dict | None:
    """Parsed metadata, or a refusal when the file exists and does not parse.

    load_metadata() returns None both for "no install yet" and for "the file is
    corrupt", and every caller used to treat the two the same. Both directions of
    that conflation lose the host: an install writes a fresh document over the
    corrupt one, stranding the backup zip it was the only pointer to, and a
    rollback reports "nothing to roll back" on a host that is still patched.
    Refuse, name the backups that are sitting there, and change nothing.
    """
    path = meta_dir / METADATA_NAME
    meta = load_metadata(meta_dir)
    if meta is None and path.is_file():
        zips = sorted(p.name for p in meta_dir.glob("host-backup-*.zip"))
        fail(
            f"{path} exists but does not parse as JSON, so {purpose} cannot proceed "
            "safely. It is the only record of the backup that restores this host "
            f"(backups present: {zips or 'none'}). Restore or move it aside "
            "deliberately -- accepting the loss of rollback -- and re-run."
        )
    return meta


def previous_metadata(meta_dir: Path) -> dict:
    """Metadata an install carries forward, refusing rather than overwriting junk."""
    return metadata_or_refuse(meta_dir, purpose="an install") or {}


def new_metadata(src: Path, previous: dict, *, mode: str) -> dict:
    """Fresh install metadata. One builder so the modes cannot drift apart.

    Everything that says how to undo the install is carried forward, not just the
    host block: a --patch-only run after a --plugin-only run used to drop
    plugin_dest, and then --rollback removed the host patch, said "no plugin
    destination in metadata", deleted the metadata, and left an installed plugin
    that nothing could remove or find again.

    ``mode`` is informational -- it records which path wrote the file so a later
    diagnosis is not guesswork. It deliberately drives no behaviour: a full
    install on an already-patched host also leaves the host block empty, so mode
    cannot be used to decide whether the host was ever ours. Only the host block
    and the markers on disk answer that.
    """
    meta = {
        "installer_version": 2,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "hermes_src": str(src),
        "base_head": git_head_short(src),
        "mode": mode,
        # Preserve the FIRST backup across repeat installs: it restores the
        # true pre-install state; later runs never mutated the host further.
        "host": previous.get("host", {}),
    }
    # Same reasoning, and it was missed once. The memory-limit record is the only
    # thing that says which number each file had BEFORE the raise. A second
    # install sees the value already at the floor, records "already 4400", and if
    # that overwrites the first record then rollback has nothing marked "raised"
    # and silently leaves the raise in place. Measured on a real clean host:
    # install, re-install, --rollback -> the route patch came out and the limit
    # stayed at 4400 with config_defaults.py left modified. raise_memory_limit()
    # merges into this rather than replacing it.
    if previous.get("memory_limit"):
        meta["memory_limit"] = previous["memory_limit"]
    # Only while the recorded tree is still there: carrying a stale destination
    # forward (HERMES_HOME changed between runs, or someone removed the tree by
    # hand) makes --rollback announce a removal of something already gone.
    if previous.get("plugin_dest") and Path(previous["plugin_dest"]).is_dir():
        meta["plugin_dest"] = previous["plugin_dest"]
        if previous.get("plugin_files"):
            meta["plugin_files"] = previous["plugin_files"]
    return meta


def do_status(args) -> None:
    src = find_hermes_src(args.hermes_src)
    state, detail = classify_host(src)
    say(f"Hermes checkout : {src}")
    say(f"State           : {state} — {detail}")
    mdir = metadata_dir(src)
    meta = load_metadata(mdir)
    if meta:
        say(f"Last install    : {meta.get('installed_at')} (installer {meta.get('installer_version')}, mode {meta.get('mode') or 'unknown'})")
        say(f"Backup          : {meta.get('host', {}).get('backup')}")
    elif (mdir / METADATA_NAME).is_file():
        # --status must not read as "never installed" when the record is simply
        # unreadable; that is the state where a patched host has no rollback.
        zips = sorted(p.name for p in mdir.glob("host-backup-*.zip"))
        say(f"Last install    : UNREADABLE metadata at {mdir / METADATA_NAME}")
        say(f"Backup          : not recoverable from metadata; zips present: {zips or 'none'}")
    plugin_dest = plugin_dest_for(src)
    say(f"Plugin installed: {(plugin_dest / 'plugin.yaml').is_file()} ({plugin_dest})")


def do_rollback(args) -> None:
    src = find_hermes_src(args.hermes_src)
    mdir = metadata_dir(src)
    meta = metadata_or_refuse(mdir, purpose="rollback")
    if not meta:
        fail("No rollback metadata found; nothing to roll back.")

    host = meta.get("host") or {}
    backup_value = host.get("backup")
    moved_on = False
    recorded_head = current_head = None
    if backup_value:
        backup = Path(backup_value)
        if not backup.is_file():
            fail(f"Backup zip missing: {backup}")
        # The head recorded WITH the backup; the top-level base_head is refreshed
        # by every run and says nothing about when the zip was taken. Older
        # records have no host-level head, so fall back to it for them.
        recorded_head = host.get("base_head") or meta.get("base_head")
        current_head = git_head_short(src)
        moved_on = bool(recorded_head and current_head and recorded_head != current_head)
        if moved_on:
            # Restoring pre-install content over a checkout that has since moved
            # on is not a restore, it is a downgrade of eight files. Say so
            # before doing it, and do not call the result byte-for-byte after.
            say(
                f"WARNING: this backup was taken at {recorded_head} but the checkout is "
                f"now at {current_head}. Restoring it overwrites the current versions of "
                f"{len(PATCH_FILES)} host files with pre-install content. If the host was "
                "upgraded since, `git apply -R` against the route patch is the safer undo."
            )
        with tempfile.TemporaryDirectory(prefix="refine-rollback-") as td:
            with zipfile.ZipFile(backup) as z:
                z.extractall(td)
            td_root = Path(td)
            for rel in PATCH_FILES:
                restored = td_root / rel
                target = src / rel
                if restored.is_file():
                    shutil.copy2(restored, target)
            for rel in host.get("created_files", []):
                target = src / rel
                if target.is_file():
                    target.unlink()
                    say(f"Removed installer-created file: {rel}")
    else:
        # Say only what the metadata proves: this installer applied no host
        # patch. Whether the host carries the route from somewhere else
        # (install.sh, a manual git apply) is not ours to claim or undo.
        say("No host backup in metadata; no Hermes host files were changed by this installer.")
        orphans = sorted(p.name for p in mdir.glob("host-backup-*.zip"))
        if orphans:
            say(
                f"WARNING: {orphans} exist in {mdir} but no metadata references them. "
                "Nothing was restored from them and they are left in place. Do not "
                "unpack one blindly: a zip can predate a host that has moved on, or "
                "come from a run whose patch never applied. `git apply -R` against "
                "the route patch, or `git checkout`, reverts a known-good base."
            )
        marked = applied_patch_files(src)
        if marked:
            say(
                f"Note: {len(marked)}/{len(PATCH_FILES)} host files still carry the "
                f"route marker (recorded install mode: {meta.get('mode') or 'unknown'}). "
                "The host is left exactly as it is."
            )

    # plugin removal (diagnostic-only mode keeps files, removes tool registration)
    mode = args.plugin_mode or "remove"
    plugin_dest_value = meta.get("plugin_dest")
    plugin_dest = Path(plugin_dest_value) if plugin_dest_value else None
    keep_record = False
    def _would_delete_this_checkout(dest: Path) -> bool:
        """Is rmtree(dest) going to take this checkout with it?

        Both sides resolved and case-folded. Comparing a resolved dest against
        the raw module global was enough on Linux and wrong on Windows, where a
        path can arrive in 8.3 form (C:\\Users\\RUNNER~1\\...) and come back long:
        the guard then returned False and the rmtree went ahead. Equality covers
        the documented layout (the checkout IS the destination); containment
        covers a checkout nested under it, which rmtree removes just as
        thoroughly. Unknowable paths count as "yes": the one guard that keeps
        Refine from deleting the user's work fails closed.
        """
        def _key(path: Path) -> str:
            return os.path.normcase(str(path))

        try:
            resolved = dest.resolve()
            here = PLUGIN_DIR.resolve()
        except OSError:
            return True
        if _key(resolved) == _key(here):
            return True
        try:
            return _key(here).startswith(_key(resolved) + os.sep)
        except (TypeError, ValueError):
            return True

    if (
        mode == "remove"
        and plugin_dest is not None
        and _would_delete_this_checkout(plugin_dest)
    ):
        # The documented live layout has the git checkout AT the install
        # destination (~/.hermes/plugins/refine), which install_plugin already
        # recognises by copying nothing. Deleting it here would erase the
        # repository, .git and any uncommitted work included, while printing
        # "Plugin removed" and exiting 0. Refine may never delete the user's work.
        say(
            f"Plugin destination is this checkout ({plugin_dest}); keeping it. "
            "Rollback removes copies it made, never the source repository. Delete "
            "it yourself if that is what you want."
        )
        keep_record = True
    elif mode == "remove" and plugin_dest is not None and plugin_dest.is_dir():
        recorded_files = meta.get("plugin_files")
        if not recorded_files:
            # No record: a copy that died before publishing the list, or a record
            # from before the list existed. Fall back to the manifest of what an
            # install COPIES -- names the installer owns by construction -- rather
            # than to the directory, which it does not. A module dropped between
            # versions is missed by this and reported below; that is the cost of
            # not guessing, and it is cheaper than deleting the journal.
            recorded_files = [str(plugin_dest / rel) for rel in plugin_files()]
            say(
                f"No plugin_files recorded for {plugin_dest}; removing only the files "
                "this installer's manifest names, not the directory."
            )
        removed, kept = remove_recorded_plugin_files(plugin_dest, recorded_files)
        if kept:
            # Deleting the record here would leave files on disk that no later
            # --rollback can find.
            say(
                f"WARNING: {len(kept)} recorded plugin file(s) could not be removed "
                f"from {plugin_dest}: {kept[:5]}"
            )
            keep_record = True
        elif plugin_dest.is_dir():
            say(
                f"Plugin files removed ({len(removed)}) from {plugin_dest}; the "
                "directory holds content this installer did not create and is "
                "left in place."
            )
        else:
            say(f"Plugin removed: {plugin_dest}")
    elif plugin_dest is not None:
        say(f"Plugin kept in place ({mode}): {plugin_dest}")
        keep_record = plugin_dest.is_dir()
    else:
        say("No plugin destination in metadata; no plugin files were removed.")

    # Before the metadata is unlinked: this is the only record of which files the
    # install raised, and once it is gone the raise is no longer undoable.
    restore_memory_limit(meta)

    if keep_record:
        say(
            f"Keeping {mdir / METADATA_NAME}: plugin files are still on disk, and the "
            "record is what a later --rollback needs to find them."
        )
    else:
        (mdir / METADATA_NAME).unlink(missing_ok=True)
    if backup_value and moved_on:
        say(
            f"Rollback complete: host files restored from the backup taken at "
            f"{recorded_head}, which is NOT the state the checkout was in ({current_head})."
        )
    elif backup_value:
        say("Rollback complete: host restored byte-for-byte from backup.")
    else:
        say("Rollback complete.")


def do_install(args) -> None:
    if args.patch_only and args.plugin_only:
        fail("--patch-only and --plugin-only cannot be used together")

    src = find_hermes_src(args.hermes_src)
    if args.plugin_only:
        mdir = metadata_dir(src)
        meta: dict = new_metadata(src, previous_metadata(mdir), mode="plugin-only")
        say("--plugin-only: skipping host classification and host patching.")
        say("Installing Refine plugin…")
        # Recorded before the first copy, not after the last one: install_plugin
        # can die mid-copy (permissions, a full disk) and a tree no metadata names
        # is one --rollback cannot remove, while the gateway still tries to load
        # it. The destination is known without copying anything.
        meta["plugin_dest"] = str(plugin_dest_for(src))
        write_metadata(mdir, meta)
        dest = install_plugin(meta, src)
        say(f"Plugin files → {dest} ({len(meta.get('plugin_files') or [])} files)")
        # Again after the copy: plugin_files is only known once it has happened,
        # and verification below can fail() on a tree that does not parse.
        write_metadata(mdir, meta)
        say(f"Metadata → {mdir / METADATA_NAME}")
        verified = verify_plugin_imports(Path(dest), python_of(src), src)
        # Report the host across every patch target, not just plugins.py: a
        # partially patched host has the marker there and still cannot route,
        # and staying silent about it is the bug this flag work set out to end.
        applied = applied_patch_files(src)
        if len(applied) == len(PATCH_FILES):
            say(f"Host capability present on all {len(PATCH_FILES)} patch targets; left untouched.")
        else:
            say(
                f"Host capability is incomplete ({len(applied)}/{len(PATCH_FILES)} patch "
                "targets carry the marker): refine_run will return "
                "llm_invocation_unavailable until --patch-only is run."
            )
        say("Memory budget:")
        raise_memory_limit(src, meta, include_host=False)
        write_metadata(mdir, meta)
        if verified == "unclassified":
            say("Done (--plugin-only), but the plugin import was NOT verified (see the NOTE above).")
        else:
            say("Done (--plugin-only).")
        return

    state, detail = classify_host(src)
    say(f"Hermes checkout : {src}")
    say(f"Host state      : {state} — {detail}")

    if args.patch_only and state == "patched":
        say("Host capability already present — nothing to do.")
        return
    if state == "incompatible":
        fail(f"Incompatible host: {detail}")

    # Built before anything is allowed to touch the host: previous_metadata()
    # refuses on an unreadable record, and that refusal is only worth having if it
    # happens while the host is still untouched.
    mdir = metadata_dir(src)
    meta: dict = new_metadata(
        src, previous_metadata(mdir), mode="patch-only" if args.patch_only else "full"
    )
    host_backup_recorded = False

    if state == "partial":
        # Record before reversing: this reverse is a host mutation like any
        # other, and "backup before edit" has no exception for the tidy-up path.
        record_host_backup(src, mdir, meta)
        host_backup_recorded = True
        say(f"Pre-reverse backup recorded: {meta['host']['backup']}")
        # Which bundled patch was half-applied is not known here, so reverse the
        # one that reverse-checks clean. A stale global would reverse a patch the
        # host does not carry.
        reverse = select_reverse_patch(src)
        if reverse is None:
            fail(
                "partial host state, but no bundled patch reverses cleanly out of it; "
                "cannot safely reach stock. `python install.py --rollback` if this "
                "installer applied it, otherwise `git checkout` the patch targets."
            )
        rb = run_git(src, "apply", "-R", str(reverse))
        say(
            f"Partial patch state detected; attempted reverse of {reverse.name} to "
            f"reach stock (rc={rb.returncode})."
        )
        state, detail = classify_host(src)
        if state not in ("stock", "patched"):
            fail(f"After reverse the host is still not installable: {state} — {detail}")
        if state == "patched":
            say("Reverse reached fully-patched state; nothing left to apply.")
            return
    if state == "dirty":
        fail(
            "Patch-target files have user modifications. Commit or stash them, "
            "then re-run. Refusing to overwrite user work."
        )

    if state == "stock":
        # classify_host already proved a patch applies; pick it again here so the
        # backup, --check, apply and metadata all name the same one.
        chosen = select_patch(src)
        if chosen is None:
            fail(f"no bundled route patch applies to this host: {detail}")
        say(f"Applying host patch {chosen.name} (with pre-mutation backup)…")
        # Ahead of the backup: a refusal should leave nothing behind at all.
        check_patch_applies(src, chosen)
        if not host_backup_recorded:
            # The partial branch already recorded the true pre-run state; keep
            # that one. A second backup here would capture the reversed tree and
            # rollback would restore the installer's own intermediate state.
            record_host_backup(src, mdir, meta)
        # Record which patch was chosen: the rollback path restores from the
        # backup zip and does not need it, but a metadata file that cannot say
        # which patch was applied makes the next diagnosis guesswork.
        meta.setdefault("host", {})["patch"] = chosen.name
        write_metadata(mdir, meta)
        apply_patch(src, mdir, chosen)
        compile_all(src)
        say("Host patch applied and compiled.")
    elif state == "patched":
        say("Host capability already present; leaving host untouched.")

    if args.patch_only:
        write_metadata(mdir, meta)
        say("Done (--patch-only). Restart the gateway to load the new core.")
        return

    say("Installing Refine plugin…")
    # Before the copy starts, so a copy that dies mid-way is still removable.
    meta["plugin_dest"] = str(plugin_dest_for(src))
    write_metadata(mdir, meta)
    dest = install_plugin(meta, src)
    say(f"Plugin files → {dest} ({len(meta.get('plugin_files') or [])} files)")
    # And again: the file list is only known after the copy, and everything below
    # here -- import verification, capability verification -- can still fail().
    write_metadata(mdir, meta)
    say(f"Metadata → {mdir / METADATA_NAME}")
    verified = verify_plugin_imports(Path(dest), python_of(src), src)

    # Capability verification: inside a fresh interpreter, ctx.llm must be bound
    # when a scope is active, and fail-closed when not.
    ver = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "sys.path.insert(0, r'%s')\n" % (str(PLUGIN_DIR.parent), str(src))
    ) + r'''
import hermes_cli.plugins as hp
assert hasattr(hp, "plugin_invocation_scope"), "host marker missing"
import agent.auxiliary_client as ac
assert hasattr(ac, "RouteLockedCallError"), "route-locked call path missing"
with hp.plugin_invocation_scope_for_agent(type("A", (), {})()):
    pass  # scope machinery functional (fail-closed binding exercised in route tests)
print("CAPABILITY_OK")
'''
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix="_capver.py", delete=False, encoding="utf-8") as tf:
        tf.write(ver)
        cap_script = tf.name
    try:
        r = subprocess.run(
            [python_of(src), cap_script], capture_output=True, text=True, timeout=120,
            # Pass through what the user set; do not invent a home. Forcing
            # ~/.hermes here told the subprocess a different home than the one
            # the plugin was just installed into, and created that directory on
            # a Windows box whose real home is %LOCALAPPDATA%\hermes.
            # No bytecode: this imports host modules and would litter the
            # checkout with __pycache__ entries the user did not ask for.
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if "CAPABILITY_OK" not in (r.stdout or ""):
            fail(f"capability verification failed:\n{r.stdout}\n{r.stderr}")
    finally:
        os.unlink(cap_script)
    # Says what the probe checked, not what the feature does: the probe asserts
    # the host markers are importable and that entering the scope works. The
    # fail-closed binding itself is exercised by the host route tests.
    say("Capability verified: host markers importable and the invocation scope entered.")

    say("Memory budget:")
    raise_memory_limit(src, meta, include_host=True)
    write_metadata(mdir, meta)

    if verified != "verified":
        # The banner is the only line most people read, so it must not read the
        # same after an unverified import as after a verified one. "host-unresolved"
        # counts as unverified HERE specifically: the capability check just above
        # proved this interpreter resolves the host, so a plugin import that failed
        # on a host module is a contradiction, not an explanation. Exit stays 0 --
        # the copy may well be fine, and refusing would block installs whose
        # environment simply cannot import Hermes.
        say(f"\nINSTALLED, PLUGIN IMPORT NOT VERIFIED ({verified}). Next steps:")
    else:
        say("\nSUCCESS. Next steps:")
    say("  1. Restart the gateway OUTSIDE its own process:")
    say("     sudo systemd-run --unit=refine-gw-restart --collect -- systemctl restart hermes-gateway")
    say("  2. Verify: /refine-cycle status  (or refine_run in a turn)")
    say("  Rollback: python install.py --rollback")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hermes-src", help="path to the Hermes checkout")
    ap.add_argument("--patch-only", action="store_true", help="only ensure the host capability")
    ap.add_argument("--plugin-only", action="store_true", help="install only plugin files (no host patch)")
    ap.add_argument("--rollback", action="store_true", help="restore host from backup; remove plugin")
    ap.add_argument("--status", action="store_true", help="report detected state without changes")
    ap.add_argument("--plugin-mode", choices=["remove", "keep"], default="remove",
                    help="what to do with plugin files during rollback")
    args = ap.parse_args(argv)

    # --rollback undoes whatever the recorded install changed; there is no
    # host-only or plugin-only rollback. Accepting the flags silently would let
    # `--plugin-only --rollback` restore the host from the backup zip -- an
    # un-patch of the live core, asked for by a user who said "plugin only".
    if args.rollback and (args.patch_only or args.plugin_only):
        fail(
            "--rollback cannot be combined with --patch-only or --plugin-only: "
            "rollback reverses whatever the recorded install did. Use --plugin-mode "
            "keep to leave the plugin files in place."
        )

    if args.status:
        do_status(args)
    elif args.rollback:
        do_rollback(args)
    else:
        do_install(args)


if __name__ == "__main__":
    main(sys.argv[1:])
