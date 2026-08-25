#!/usr/bin/env bash
# install.sh — install the Refine-Cycle plugin AND its Hermes core patch
# ("Bind plugin LLM calls to the active invocation route").
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
#   ./install.sh              # install plugin + apply core patch (with backup)
#   ./install.sh --patch-only # only apply/verify the core patch
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
# ---------------------------------------------------------------------------
HERMES_SRC="${HERMES_SRC:-}"
if [ -z "$HERMES_SRC" ]; then
    for cand in \
        "$(systemctl show hermes-gateway -p ExecStart --value 2>/dev/null | grep -oE '/[^ ]*releases[^ ]*/' | head -1)" \
        "$HOME/releases/hermes-agent-v2026.8.16-clean" \
        "$HOME/hermes-agent"; do
        if [ -n "$cand" ] && [ -d "$cand" ] && [ -f "$cand/agent/plugin_llm.py" ]; then
            HERMES_SRC="$(cd "$cand" && pwd)"
            break
        fi
    done
fi
[ -n "$HERMES_SRC" ] && [ -d "$HERMES_SRC" ] || fail "cannot locate a Hermes checkout; set HERMES_SRC=/path/to/hermes-agent"
say "Hermes checkout: $HERMES_SRC"

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
[ -f "$PATCH_FILE" ] || fail "patch file missing: $PATCH_FILE"
mapfile -t TOUCHED_FILES < <(grep -E "^diff --git " "$PATCH_FILE" | sed -E 's#^diff --git a/([^ ]+) b/.*#\1#')
[ "${#TOUCHED_FILES[@]}" -gt 0 ] || fail "could not parse touched files from $PATCH_FILE"

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
$APPLY_FAILURES"
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
VENV_PY=""
if [ -x "$HERMES_SRC/venv/bin/python" ]; then
    VENV_PY="$HERMES_SRC/venv/bin/python"
elif [ -x "$HERMES_SRC/venv/Scripts/python.exe" ]; then
    VENV_PY="$HERMES_SRC/venv/Scripts/python.exe"
fi
if [ -n "$VENV_PY" ]; then
    if PYTHONPATH="$HERMES_SRC" "$VENV_PY" -c "import hermes_cli.plugins" >/dev/null 2>&1; then
        say "import check: hermes_cli.plugins imports cleanly."
    else
        verify_fail "hermes_cli.plugins failed to import after applying the patch"
    fi
else
    say "import check skipped (no venv interpreter at $HERMES_SRC/venv; symbol + compile checks passed)."
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
