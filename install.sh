#!/usr/bin/env bash
# install.sh — apply the Refine-Cycle Hermes core patch
# ("Bind plugin LLM calls to the active invocation route").
#
# This script patches the HOST ONLY. It does not copy the plugin anywhere; see
# "Next steps" at the end of a successful run, or use install.py, which does
# both. The header used to claim it installed the plugin too.
#
# The core patch is REQUIRED for refine_run to work: without it the plugin
# cannot see the invocation-bound LLM route and every proposal run stops with
# llm_invocation_unavailable.
#
# Behaviour (v2 — verify the RESULT, not the INPUT):
#   1. DETECT.   If the route is already present, exit success and change
#                nothing (repeated runs are a no-op, never a double apply).
#   2. APPLY     `git apply --check`, then `-3` (three-way merge), then
#      TOLERANTLY `-3 -C1`, then `-3 -C0` — decreasing context — before giving
#                up. A hunk that still matches on a nearby commit applies.
#   3. VERIFY    The route symbol exists, no conflict markers, every touched
#      BY OUTCOME file compiles, and the core module still imports. If any
#                check fails, the pre-patch state is restored byte-for-byte.
#   4. REFUSE    On genuine failure, name the host version, the patch base,
#      HONESTLY  and every attempt that failed. "Cannot patch this core" is a
#                fine outcome; "refused without trying" is not.
#
# Usage:
#   ./install.sh                       # apply/verify the core patch (with backup)
#   HERMES_SRC=/path/to/checkout ./install.sh
#
# The checkout is otherwise taken from the running hermes-gateway unit. Arguments
# are not parsed: there is only one mode. `--patch-only` was documented and never
# implemented; it was silently ignored, which read as if it had been honoured.
#
# Patch base: the patch was built against stock Hermes v2026.8.16 (commit
# df4b65147d). The script does not refuse other commits — it tries, and only
# fails when it cannot produce a working route. On failure the host is
# restored exactly as it was.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="$REPO_DIR/assets/invocation-route-v2026.8.16.patch"
PATCH_BASE_LONG="df4b65147d"   # informational; the pin is on the OUTCOME now
ROUTE_SYMBOL="plugin_invocation_scope"

say()  { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Locate the Hermes checkout
#
# The checkout is found by walking UP from the binary the gateway actually
# executes, never by pattern-matching its path. ExecStart points at the venv
# launcher (.../releases/<name>/.venv/bin/hermes) — the venv is `.venv` on some
# installs and `venv` on others, and the release directory carries no fixed
# name — so only an ancestor walk that tests for the source layout can identify
# the root.
#
# This is not a tidy-up. Matching a `releases/` fragment returned
# `.../<name>/.venv/bin/`, which is not a checkout and was rejected; the search
# then fell through to a hardcoded older release that still existed on disk.
# install.sh patched that dead tree, verified it, printed success, and left the
# running gateway unpatched — refine_run kept returning
# llm_invocation_unavailable with nothing anywhere saying why. A version-pinned
# fallback path cannot be a fallback for "which Hermes is running".
# ---------------------------------------------------------------------------
hermes_root_from() {
    local dir="${1:-}" prev=""
    [ -n "$dir" ] || return 1
    while [ -n "$dir" ] && [ "$dir" != "$prev" ]; do
        if [ -f "$dir/agent/plugin_llm.py" ] && [ -f "$dir/hermes_cli/plugins.py" ]; then
            (cd "$dir" && pwd)
            return 0
        fi
        prev="$dir"
        dir="$(dirname "$dir")"
    done
    return 1
}

# The executable from `ExecStart={ path=/... ; argv[]=... }`, parsed as a field
# rather than scraped, so a path containing `releases` or spaces cannot skew it.
gateway_exec_path() {
    systemctl show hermes-gateway -p ExecStart --value 2>/dev/null \
        | sed -nE 's/.*[{[:space:]]path=([^;]+);.*/\1/p' \
        | sed -E 's/[[:space:]]+$//' \
        | head -1
}

GATEWAY_SRC="$(hermes_root_from "$(gateway_exec_path)" || true)"

HERMES_SRC="${HERMES_SRC:-}"
if [ -n "$HERMES_SRC" ]; then
    HERMES_SRC="$(hermes_root_from "$HERMES_SRC" || true)"
    [ -n "$HERMES_SRC" ] || fail "HERMES_SRC is set but no Hermes checkout was found at or above it
  (looked for agent/plugin_llm.py + hermes_cli/plugins.py)"
else
    for cand in "$GATEWAY_SRC" "$HOME/hermes-agent"; do
        HERMES_SRC="$(hermes_root_from "$cand" || true)"
        [ -n "$HERMES_SRC" ] && break
    done
fi
[ -n "$HERMES_SRC" ] && [ -d "$HERMES_SRC" ] || fail "cannot locate a Hermes checkout; set HERMES_SRC=/path/to/hermes-agent"
say "Hermes checkout: $HERMES_SRC"

# Patching a checkout the gateway does not run from is the failure this script
# used to produce silently. Say it out loud instead.
if [ -n "$GATEWAY_SRC" ] && [ "$GATEWAY_SRC" != "$HERMES_SRC" ]; then
    say "WARNING: the running hermes-gateway executes from $GATEWAY_SRC,"
    say "         not from $HERMES_SRC. Patching the latter will not affect the"
    say "         live gateway. Re-run with HERMES_SRC=$GATEWAY_SRC if that was"
    say "         not deliberate."
fi

HOST_DESC="$(git -C "$HERMES_SRC" rev-parse --short=10 HEAD 2>/dev/null || echo "unknown")"

# Interpreter for compile/import checks: $PYTHON, else python3, else python.
PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then
            PYTHON_BIN="$(command -v "$cand")"
            break
        fi
    done
fi
[ -n "$PYTHON_BIN" ] || fail "no python3/python on PATH; set PYTHON=/path/to/python"

# ---------------------------------------------------------------------------
# 1. DETECT — already applied? Never patch twice, never re-patch.
# ---------------------------------------------------------------------------
if grep -q "$ROUTE_SYMBOL" "$HERMES_SRC/hermes_cli/plugins.py" 2>/dev/null; then
    say "core patch: already applied (route symbol present) — nothing to do."
    exit 0
fi

git -C "$HERMES_SRC" rev-parse HEAD >/dev/null 2>&1 || fail "$HERMES_SRC is not a git checkout; refusing to patch blind."

# ---------------------------------------------------------------------------
# Patch inventory — parsed from the patch's own header, so the script never
# hardcodes a file list that drifts from the patch. (Only needed below DETECT:
# a host that already carries the route is done before this point.)
# ---------------------------------------------------------------------------
# Which bundled patch fits THIS host is decided by trying them, not by comparing
# version strings. Hermes moved 72 commits across these files between v2026.8.16
# and v2026.8.31, yet the 8.16 patch still lands 39 of its 40 hunks on 8.31 — so a
# version test would refuse hosts the patch fits and accept hosts it does not.
# `git apply --check` answers the only question that matters, and changes nothing.
select_patch() {
    local candidate
    for candidate in "$@"; do
        [ -f "$candidate" ] || continue
        if git -C "$HERMES_SRC" apply --check "$candidate" >/dev/null 2>&1; then
            PATCH_FILE="$candidate"
            say "Route patch    : $(basename "$candidate")"
            return 0
        fi
    done
    return 1
}

if ! select_patch \
        "$REPO_DIR/assets/invocation-route-v2026.8.31.patch" \
        "$REPO_DIR/assets/invocation-route-v2026.8.16.patch"; then
    fail "every bundled route patch does not apply to this Hermes checkout.
  Host HEAD   : $HOST_DESC
  Patch base  : $PATCH_BASE_LONG (v2026.8.16) and v2026.8.31
  Tried       : invocation-route-v2026.8.31.patch, invocation-route-v2026.8.16.patch
  Nothing was modified. The patch needs rebasing onto this host's version."
fi

[ -f "$PATCH_FILE" ] || fail "patch file missing: $PATCH_FILE"
# Read the file list from the `+++` headers, the same lines `git apply` itself
# uses, because not every patch here is a git patch. The 8.31 patch was produced
# with `diff -ruN --strip-trailing-cr orig/ work/`, so it carries no
# `diff --git` line at all: the previous parse found zero files and install.sh
# aborted immediately after selecting that patch. `git apply --check` had always
# passed on it, so the patch looked ready while the install path could never run.
# `+++ b/path<TAB>timestamp` (diff -ruN) and `+++ b/path` (git) both parse; a
# `+++ /dev/null` deletion falls back to the `---` side, and this patch set
# deletes nothing.
patch_touched_files() {
    awk '
        /^--- / { minus = $2 }
        /^\+\+\+ / {
            path = ($2 == "/dev/null") ? minus : $2
            sub(/^[ab]\//, "", path)
            if (path != "" && path != "/dev/null" && !(path in seen)) {
                seen[path] = 1
                print path
            }
        }
    ' "$1"
}

mapfile -t TOUCHED_FILES < <(patch_touched_files "$PATCH_FILE")
[ "${#TOUCHED_FILES[@]}" -gt 0 ] || fail "could not parse touched files from $PATCH_FILE"
say "Touched files  : ${#TOUCHED_FILES[@]}"

# ---------------------------------------------------------------------------
# Backup every touched file before writing; restore() is the single undo step
# ---------------------------------------------------------------------------
BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/refine-route-patch.XXXXXX")"
restore() {
    local f ok=1
    for f in "${TOUCHED_FILES[@]}"; do
        if [ -f "$BACKUP_DIR/$f" ]; then
            cp -p "$BACKUP_DIR/$f" "$HERMES_SRC/$f" || ok=0
        fi
    done
    if [ "$ok" -eq 1 ]; then
        say "pre-patch state fully restored from $BACKUP_DIR."
    else
        say "RESTORE FAILED; recovery copy remains at $BACKUP_DIR" >&2
    fi
}
trap 'rm -rf "$BACKUP_DIR"' EXIT

for f in "${TOUCHED_FILES[@]}"; do
    if [ -f "$HERMES_SRC/$f" ]; then
        mkdir -p "$BACKUP_DIR/$(dirname "$f")"
        cp -p "$HERMES_SRC/$f" "$BACKUP_DIR/$f"
    else
        say "note: $f does not exist in this checkout (the patch will create it)."
    fi
done

# ---------------------------------------------------------------------------
# 2. APPLY TOLERANTLY — clean first, then three-way with decreasing context
# ---------------------------------------------------------------------------
APPLY_FAILURES=""
apply_attempt() {
    local err
    if err="$(git -C "$HERMES_SRC" apply "$@" "$PATCH_FILE" 2>&1)"; then
        return 0
    fi
    APPLY_FAILURES="${APPLY_FAILURES}  [git apply $*] $err
"
    return 1
}

APPLIED_FROM=""
if apply_attempt "--check" && apply_attempt; then
    APPLIED_FROM="git apply (clean)"
elif apply_attempt "-3"; then
    APPLIED_FROM="git apply -3 (three-way merge)"
elif apply_attempt "-3" "-C1"; then
    APPLIED_FROM="git apply -3 -C1 (three-way, reduced context)"
elif apply_attempt "-3" "-C0"; then
    APPLIED_FROM="git apply -3 -C0 (three-way, minimal context)"
else
    fail "patch does not apply to this host (nothing was modified).
  host HEAD:   $HOST_DESC
  patch base:  $PATCH_BASE_LONG (built against stock v2026.8.16)
  attempts:
$APPLY_FAILURES

  WHAT YOU GET WITHOUT THE PATCH (be clear about this):
  - refine_run falls back to the DEFAULT structured proposer for every
    proposal, ignoring the invocation-bound route: a run inside a session
    bound to provider X can still bill a provider Y configured gateway-wide.
  - every such run ends in outcome=llm_invocation_unavailable with
    'target_source: invocation_bound, primary_attempts: 0' in its llm_meta —
    that is the honest signal the route is missing, not a silent no_op.
  - the plugin's own features (detection, journal, audit, rollback) all work;
    only the route binding is absent.
  Options: upgrade the host checkout to v2026.8.16+, or apply the patch
  manually after resolving the drift (see assets/ header for the hunks)."
fi
say "core patch applied ($APPLIED_FROM)."

# ---------------------------------------------------------------------------
# 3. VERIFY BY OUTCOME — symbol, no conflict markers, compiles, imports
# ---------------------------------------------------------------------------
verify_fail() {
    restore
    fail "$1
  (pre-patch state restored; nothing remains applied.)"
}

for f in "${TOUCHED_FILES[@]}"; do
    [ -f "$HERMES_SRC/$f" ] || verify_fail "expected '$f' after apply — it is missing"
done

grep -q "$ROUTE_SYMBOL" "$HERMES_SRC/hermes_cli/plugins.py" \
    || verify_fail "route symbol '$ROUTE_SYMBOL' missing from hermes_cli/plugins.py after apply"

# Conflict markers mean a half-merged apply; that is a failed outcome. Use the
# line START of a real git conflict marker — `<<<<<<< HEAD` / `>>>>>>> branch`
# (both always carry a trailing space + ref label). Do not match a bare
# `=======` line: several core files open with a decorative banner of `====`
# characters (agent/plugin_llm.py, hermes_cli/plugins.py), which a loose regex
# would falsely flag as a conflict and wrongly trigger a restore after a clean
# apply.
if grep -qE '^(<<<<<<< |>>>>>>> )' "${TOUCHED_FILES[@]/#/$HERMES_SRC/}" 2>/dev/null; then
    verify_fail "conflict markers found in touched files after apply"
fi

if ! "$PYTHON_BIN" -m py_compile "${TOUCHED_FILES[@]/#/$HERMES_SRC/}"; then
    verify_fail "py_compile failed on a touched file"
fi

# Import smoke test with the checkout's own interpreter when one exists; a
# stock interpreter (which sees no third-party deps) cannot prove the import.
# `.venv` first: that is what the release layout the gateway runs from uses, and
# checking only `venv` skipped the import test on every such host — the one
# verification step that can prove the patched core still loads was reported as
# "skipped" and read as "passed".
VENV_PY=""
for cand in \
    "$HERMES_SRC/.venv/bin/python" \
    "$HERMES_SRC/venv/bin/python" \
    "$HERMES_SRC/.venv/Scripts/python.exe" \
    "$HERMES_SRC/venv/Scripts/python.exe"; do
    if [ -x "$cand" ]; then
        VENV_PY="$cand"
        break
    fi
done
if [ -n "$VENV_PY" ]; then
    if PYTHONPATH="$HERMES_SRC" "$VENV_PY" -c "import hermes_cli.plugins" >/dev/null 2>&1; then
        say "import check: hermes_cli.plugins imports cleanly."
    else
        verify_fail "hermes_cli.plugins failed to import after applying the patch"
    fi
else
    say "import check skipped (no venv interpreter under $HERMES_SRC/{.venv,venv}; symbol + compile checks passed)."
fi

say "core patch applied + verified (host HEAD $HOST_DESC)."

cat <<EOF

Next steps:
  1. Copy/sync this plugin into ~/.hermes/plugins/refine
     (or run: hermes plugins ... / restart the gateway so it reloads)
  2. Restart the gateway OUTSIDE its own process:
       sudo systemd-run --unit=refine-gw-restart --collect -- systemctl restart hermes-gateway
  3. Verify in ~/.hermes/logs/agent.log:
       grep "refine-cycle" agent.log | tail
  4. Test: send /refine-cycle status in a chat, or run refine_run via a turn.

To undo the core patch:
  git -C $HERMES_SRC apply -R $PATCH_FILE
EOF
