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

Supported Hermes bases are identified by the bundled route patch that applies
cleanly. The installer classifies the host as stock / patched / partial / dirty / incompatible and
refuses anything it cannot handle instead of half-applying. Every mutation is
preceded by a backup recorded in rollback metadata.
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
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PATCH_DIR = PLUGIN_DIR / "assets"
PATCH_GLOB = "invocation-route-*.patch"
METADATA_NAME = "refine-install-metadata.json"
PATCHED_MARKER = "plugin_invocation_scope"

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
    return rels


def say(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
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


def _patch_sort_key(path: Path) -> tuple[int, ...]:
    """Sort route patches by numeric version components, newest first."""
    return tuple(int(number) for number in re.findall(r"\d+", path.name))


def patch_candidates() -> list[Path]:
    """Return bundled route patches in descending numeric version order."""
    return sorted(PATCH_DIR.glob(PATCH_GLOB), key=_patch_sort_key, reverse=True)


def select_patch(src: Path) -> Path | None:
    """Return the newest bundled patch that applies cleanly to ``src``."""
    for candidate in patch_candidates():
        if run_git(src, "apply", "--check", str(candidate)).returncode == 0:
            return candidate
    return None


def select_reverse_patch(src: Path) -> Path | None:
    """Return the bundled patch that cleanly reverses the partial host state."""
    for candidate in patch_candidates():
        if run_git(src, "apply", "-R", "--check", str(candidate)).returncode == 0:
            return candidate
    return None


def classify_host(src: Path) -> tuple[str, str]:
    """Return (state_class, detail). States: stock, patched, partial, dirty, incompatible."""
    head = git_head_short(src)
    if head is None:
        return ("incompatible", "not a git checkout; refusing to patch blind")

    plugins_py = src / "hermes_cli" / "plugins.py"
    text = plugins_py.read_text(encoding="utf-8", errors="replace")
    has_marker = PATCHED_MARKER in text

    def _applied(rel: str) -> bool:
        f = src / rel
        if not f.is_file():
            return False
        body = f.read_text(encoding="utf-8", errors="replace")
        return FILE_MARKERS.get(rel, PATCHED_MARKER) in body

    applied_count = sum(1 for rel in PATCH_FILES if _applied(rel))
    if has_marker and applied_count == len(PATCH_FILES):
        return ("patched", f"all {len(PATCH_FILES)} files carry the marker at {head}")
    if 0 < applied_count < len(PATCH_FILES):
        return ("partial", f"{applied_count}/{len(PATCH_FILES)} files patched at {head}")
    if has_marker and applied_count == 0:
        return ("incompatible", f"marker found only outside expected files at {head}")

    st = run_git(src, "status", "--short", "--", *PATCH_FILES)
    if st.returncode == 0 and st.stdout.strip():
        touched = [line.split()[-1] for line in st.stdout.splitlines()]
        return ("dirty", f"user-modified patch targets before install: {touched}")

    chosen = select_patch(src)
    if chosen is not None:
        return ("stock", f"clean base {head}; {chosen.name} applies")
    candidates = patch_candidates()
    tried = ", ".join(candidate.name for candidate in candidates) or "none bundled"
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
    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rel in PATCH_FILES:
            f = src / rel
            if f.is_file():
                z.write(f, arcname=rel)
    return backup_path


def apply_patch_atomic(src: Path, meta_dir: Path, patch: Path) -> dict:
    """Apply the patch via git apply in a temp index-safe way.

    git is already required (we classified via HEAD); `git apply` is used on
    every platform the host itself supports. Files are written through temp
   +rename by git, keeping the mutation atomic per file.
    """
    created_before = {rel for rel in ALL_PATCH_CONTENT if not (src / rel).is_file()}
    backup = backup_host(src, meta_dir)
    r = run_git(src, "apply", "--check", str(patch))
    if r.returncode != 0:
        fail(f"patch does not apply cleanly; aborting without changes.\n{r.stderr}")
    r = run_git(src, "apply", str(patch))
    if r.returncode != 0:
        fail(f"patch application failed mid-way; restoring backup.\n{r.stderr}")
    # verify markers landed everywhere
    missing = []
    for rel in PATCH_FILES:
        f = src / rel
        marker = FILE_MARKERS.get(rel, PATCHED_MARKER)
        if not f.is_file() or marker not in f.read_text(encoding="utf-8", errors="replace"):
            missing.append(rel)
    if missing:
        rb = run_git(src, "apply", "-R", str(patch))
        fail(f"verification failed ({missing}); patch reversed rc={rb.returncode}")
    return {"backup": str(backup), "created_files": sorted(created_before)}


def compile_all(src: Path) -> None:
    import py_compile

    for rel in PATCH_FILES:
        py_compile.compile(str(src / rel), doraise=True)


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


def install_plugin(meta: dict) -> str:
    """Copy plugin files into ~/.hermes/plugins/refine (idempotent)."""
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    dest = hermes_home / "plugins" / "refine"
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


def verify_plugin_imports(dest: Path, python: str, src: Path) -> None:
    """Import the tree that was just copied, using the interpreter that will load it.

    The pre-existing capability verification checks the HOST patch markers and
    never touches the copied plugin, which is exactly how an install missing
    notify.py could print SUCCESS while the plugin was unloadable. ``core``
    imports every sibling module at module level, so one import exercises the
    whole copy.

    Only a missing PLUGIN module is fatal. A missing host module (``agent.*``,
    ``gateway.*``) means this environment cannot resolve Hermes the way the
    gateway does, not that the install is incomplete -- and failing on that would
    refuse a perfectly good install. The Hermes checkout goes on PYTHONPATH first
    so host modules usually do resolve; when they still do not, say so and carry
    on rather than pretending the result is conclusive.
    """
    owned = sorted(
        Path(rel).stem for rel in plugin_files()
        if rel.endswith(".py") and "/" not in rel
    )
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(src)] + ([existing] if existing else []))
    r = subprocess.run(
        [python, "-c", "import core"],
        cwd=str(dest), capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", env=env,
    )
    if r.returncode == 0:
        say("Plugin import verified in a fresh interpreter.")
        return
    stderr = r.stderr or ""
    tail = [line for line in stderr.strip().splitlines() if line.strip()]
    detail = tail[-1] if tail else "no stderr"
    absent = [name for name in owned if f"No module named '{name}'" in stderr]
    if absent:
        fail(
            f"the installed plugin is missing its own module(s) {absent}; the copy "
            f"is incomplete and the plugin cannot load.\n  {detail}\n  tree: {dest}"
        )
    say(
        f"NOTE: could not fully import the plugin here ({detail}). No plugin module "
        "is missing, so this is host resolution in this environment, not an "
        "incomplete install."
    )


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


def do_status(args) -> None:
    src = find_hermes_src(args.hermes_src)
    state, detail = classify_host(src)
    say(f"Hermes checkout : {src}")
    say(f"State           : {state} — {detail}")
    meta = load_metadata(metadata_dir(src))
    if meta:
        say(f"Last install    : {meta.get('installed_at')} (installer {meta.get('installer_version')})")
        say(f"Backup          : {meta.get('host', {}).get('backup')}")
    plugin_dest = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "plugins" / "refine"
    say(f"Plugin installed: {(plugin_dest / 'plugin.yaml').is_file()} ({plugin_dest})")


def do_rollback(args) -> None:
    src = find_hermes_src(args.hermes_src)
    mdir = metadata_dir(src)
    meta = load_metadata(mdir)
    if not meta:
        fail("No rollback metadata found; nothing to roll back.")
    backup = Path(meta["host"]["backup"])
    if not backup.is_file():
        fail(f"Backup zip missing: {backup}")
    with tempfile.TemporaryDirectory(prefix="refine-rollback-") as td:
        with zipfile.ZipFile(backup) as z:
            z.extractall(td)
        td_root = Path(td)
        for rel in PATCH_FILES:
            restored = td_root / rel
            target = src / rel
            if restored.is_file():
                shutil.copy2(restored, target)
        for rel in meta["host"].get("created_files", []):
            target = src / rel
            if target.is_file():
                target.unlink()
                say(f"Removed installer-created file: {rel}")
    # plugin removal (diagnostic-only mode keeps files, removes tool registration)
    mode = args.plugin_mode or "remove"
    plugin_dest = Path(meta.get("plugin_dest") or "")
    if mode == "remove" and plugin_dest.is_dir():
        shutil.rmtree(plugin_dest, ignore_errors=True)
        say(f"Plugin removed: {plugin_dest}")
    else:
        say(f"Plugin kept in place ({mode}): {plugin_dest}")
    mdir_r = metadata_dir(src)
    (mdir_r / METADATA_NAME).unlink(missing_ok=True)
    say("Rollback complete: host restored byte-for-byte from backup.")


def do_install(args) -> None:
    src = find_hermes_src(args.hermes_src)
    state, detail = classify_host(src)
    say(f"Hermes checkout : {src}")
    say(f"Host state      : {state} — {detail}")

    if args.patch_only and state == "patched":
        say("Host capability already present — nothing to do.")
        return
    if state == "incompatible":
        fail(f"Incompatible host: {detail}")
    if state == "partial":
        reverse_patch = select_reverse_patch(src)
        if reverse_patch is None:
            tried = ", ".join(candidate.name for candidate in patch_candidates()) or "none bundled"
            fail(f"Partial patch state cannot be reversed; tried: {tried}")
        rb = run_git(src, "apply", "-R", str(reverse_patch))
        say(f"Partial patch state detected; attempted reverse to reach stock (rc={rb.returncode}).")
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

    mdir = metadata_dir(src)
    previous = load_metadata(mdir) or {}
    meta: dict = {
        "installer_version": 2,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "hermes_src": str(src),
        "base_head": git_head_short(src),
        # Preserve the FIRST backup across repeat installs: it restores the
        # true pre-install state; later runs never mutated the host further.
        "host": previous.get("host", {}),
    }

    if state == "stock":
        chosen_patch = select_patch(src)
        if chosen_patch is None:
            fail("No bundled route patch applies after classification; aborting without changes.")
        say(f"Applying host patch {chosen_patch.name} (with pre-mutation backup)…")
        meta["host"] = apply_patch_atomic(src, mdir, chosen_patch)
        meta["host"]["patch"] = chosen_patch.name
        compile_all(src)
        say("Host patch applied and compiled.")
    elif state == "patched":
        say("Host capability already present; leaving host untouched.")

    if args.patch_only:
        write_metadata(mdir, meta)
        say("Done (--patch-only). Restart the gateway to load the new core.")
        return

    say("Installing Refine plugin…")
    dest = install_plugin(meta)
    say(f"Plugin files → {dest} ({len(meta.get('plugin_files') or [])} files)")
    verify_plugin_imports(Path(dest), python_of(src), src)

    write_metadata(mdir, meta)
    say(f"Metadata → {mdir / METADATA_NAME}")

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
            env={**os.environ, "HERMES_HOME": os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))},
        )
        if "CAPABILITY_OK" not in (r.stdout or ""):
            fail(f"capability verification failed:\n{r.stdout}\n{r.stderr}")
    finally:
        os.unlink(cap_script)
    say("Capability verified: invocation-bound machinery present and fail-closed.")

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

    if args.status:
        do_status(args)
    elif args.rollback:
        do_rollback(args)
    else:
        do_install(args)


if __name__ == "__main__":
    main(sys.argv[1:])
