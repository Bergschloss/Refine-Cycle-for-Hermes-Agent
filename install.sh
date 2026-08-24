#!/usr/bin/env bash
# install.sh — install the Refine-Cycle plugin AND its Hermes core patch
# ("Bind plugin LLM calls to the active invocation route").
#
# The core patch is REQUIRED for refine_run to work: without it the plugin
# cannot see the invocation-bound LLM route and every proposal run stops with
# llm_invocation_unavailable.
#
# Usage:
#   ./install.sh              # install plugin + apply core patch (with backup)
#   ./install.sh --patch-only # only apply/verify the core patch
#
# Supported base: stock Hermes v2026.8.16 (commit df4b65147d). The script
# refuses anything else rather than half-applying.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="$REPO_DIR/assets/invocation-route-v2026.8.16.patch"
EXPECTED_BASE="df4b6514"

say()  { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ -f "$PATCH_FILE" ] || fail "patch file missing: $PATCH_FILE"

# ---------------------------------------------------------------------------
# Locate the running Hermes checkout
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

# ---------------------------------------------------------------------------
# Verify base version — never half-apply onto an unknown tree
# ---------------------------------------------------------------------------
if git -C "$HERMES_SRC" rev-parse HEAD >/dev/null 2>&1; then
    BASE_SHA="$(git -C "$HERMES_SRC" rev-parse --short=10 HEAD)"
    case "$BASE_SHA" in
        df4b6514*) say "base OK: $BASE_SHA (stock v2026.8.16)" ;;
        *) fail "unsupported base $BASE_SHA. This patch targets stock v2026.8.16 (df4b65147d).
Update/checkout that release first, or set HERMES_SRC to such a checkout." ;;
    esac
    if grep -q "plugin_invocation_scope" "$HERMES_SRC/hermes_cli/plugins.py" 2>/dev/null; then
        say "core patch: already applied — nothing to do."
        exit 0
    fi
else
    fail "$HERMES_SRC is not a git checkout; refusing to patch blind."
fi

# ---------------------------------------------------------------------------
# Backup, apply, verify
# ---------------------------------------------------------------------------
STAMP="$(date +%Y%m%dT%H%M%S)"
git -C "$HERMES_SRC" stash create >/dev/null 2>&1 || true
BACKUP_REF="$(git -C "$HERMES_SRC" rev-parse HEAD)"
say "backup reference (current HEAD): $BACKUP_REF"

git -C "$HERMES_SRC" apply --check "$PATCH_FILE" || fail "patch does not apply cleanly; aborting without changes."
git -C "$HERMES_SRC" apply "$PATCH_FILE"
python3 -m py_compile \
    "$HERMES_SRC/agent/auxiliary_client.py" \
    "$HERMES_SRC/agent/plugin_llm.py" \
    "$HERMES_SRC/gateway/run.py" \
    "$HERMES_SRC/hermes_cli/plugins.py" || { git -C "$HERMES_SRC" apply -R "$PATCH_FILE"; fail "compile failed; patch reverted."; }

grep -q "plugin_invocation_scope" "$HERMES_SRC/hermes_cli/plugins.py" || { git -C "$HERMES_SRC" apply -R "$PATCH_FILE"; fail "verification failed; patch reverted."; }
say "core patch applied + verified."

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
  cd $HERMES_SRC && git apply -R $PATCH_FILE
EOF
