# The 0.21.0 invocation route: built and verified, not yet installable

**Status:** capability written and tested; patch verified to apply; **installer
metadata not yet migrated, so the patch is deliberately not selectable.**

The patch lives at `assets/pending/invocation-route-v0.21.0.patch`, outside the
`assets/invocation-route-*.patch` glob that `patch_candidates()` walks. That is the
whole point of the location: dropping it into `assets/` would make it selectable
immediately, and the installer would then mis-diagnose every host it patched. The
remaining work is one bounded refactor, described below with the tables already
worked out.

---

## What is done

Written against host `693641aa8b` (Hermes 0.21.0) in a throwaway clone; the real
checkout was never modified and is still clean at that commit.

| Check | Result |
|---|---|
| `git apply --check` against the real 0.21.0 checkout | passes (exit 0) |
| New host tests (`tests/agent/test_plugin_invocation_route.py`) | 36 passed |
| Host's own plugin-LLM tests | 57 passed |
| Targeted host regression set (19 files) | 489 passed, 17 failed |
| Same set on the pristine tree | 489 passed, **the same 17** failed |
| Regressions attributable to the patch | **none** |

The 17 failures are pre-existing on this Windows box (missing binaries, absent
optional modules); the comparison was run by stashing the patch and re-running the
identical file set. Five unrelated files also fail *collection* identically on both
trees.

## The required semantics, and where each one lives

| Guarantee | Where it is enforced |
|---|---|
| exact active provider/model/client | `PluginInvocationRoute.from_agent` captures the agent's own client object rather than rebuilding one |
| immutable binding | `@dataclass(frozen=True)`; a mid-turn `/model` switch cannot mutate a route already handed out |
| no ambient-config fallback | `validated()` refuses an under-specified route; `_route_locked_request` refuses a missing client |
| no provider/model override | `_reject_bound_overrides`, called at the top of `_gate` before the trust gate |
| one physical request per attempt | `call_llm_route_locked` calls `_relay_sync_completion` once |
| no same-provider retry, no failover | the locked path never enters `call_llm`, which owns the transient-retry loop and the recovery ladder |
| fail closed on an incomplete route | scopes open with `error_code="incomplete_route"`; `bind_invocation_error` returns a bound facade that refuses to call |
| task/thread context propagation | `contextvars.copy_context()` around the `hermes-plugin-command-await` thread |

### Why a separate entry point rather than flags on `call_llm`

The 8.x patch threaded `bound_client=` / `single_attempt=True` through `call_llm`
and its ladder. On 0.21.0 that path was redesigned: `_plan_aux_call` →
`_prepare_aux_request` → primary attempt → transient-retry loop → `_drive_ladder`
with parameter-strip, credential and provider-fallback rungs. Every rung can move a
call somewhere the caller did not choose, so a "do not recover" flag would leave the
single-route guarantee one refactor away from silently lapsing. `call_llm_route_locked`
is a self-contained path that cannot grow a recovery rung by accident, and it reuses
`_build_call_kwargs` / `_relay_sync_completion` / `_validate_llm_response` so a
locked request is not a subtly different request.

### Known limitation, stated rather than hidden

Only OpenAI-shaped chat transports are supported. `api_mode="anthropic_messages"`
fails closed with `unsupported_api_mode`, which the plugin already renders as
`llm_transport_unsupported`. Coercing an Anthropic client through
`client.chat.completions.create` would be a silent transport substitution, which is
the failure this path exists to prevent. Anthropic-mode support needs its own
verified transport and is out of scope here.

---

## What remains: per-patch installer metadata

`install.py` models the host topology as **one** shared list. 0.21.0 moved the
targets, so that assumption is now wrong.

| Old (`v2026.8.16`, `v2026.8.31`) | New (`v0.21.0`) |
|---|---|
| `agent/auxiliary_client.py` | `agent/auxiliary_client.py` |
| `agent/plugin_llm.py` | `agent/plugin_llm.py` |
| `agent/turn_context.py` | `agent/turn_context.py` |
| `cli.py` | `cli.py` |
| `hermes_cli/plugins.py` | `hermes_cli/plugins.py` |
| `tui_gateway/methods_tools.py` | `tui_gateway/methods_tools.py` |
| `gateway/run.py` | **`gateway/run_inbound.py`** |
| `run_agent.py` | **`agent/turn_facade.py`** |

Both are eight files, which is a coincidence and not a reason to keep one list.

### The concrete failure if this is skipped

On a 0.21.0 host correctly patched by `v0.21.0`, `applied_patch_files()` walks the
legacy list and finds markers in five of eight: `gateway/run.py` and `run_agent.py`
are untouched by this topology, and `agent/auxiliary_client.py` carries
`call_llm_route_locked` rather than the legacy `_call_route_locked_once`. Five of
eight is `0 < n < 8`, so `classify_host` returns **`partial`**, and `do_install`
then tries to *reverse* a correctly patched host. `--status` reports `partial`
forever, which is simply false.

### Marker table

The `agent/auxiliary_client.py` marker differs per patch; every other shared file
keeps its marker, so markers must be keyed per patch, not per file alone.

```python
_LEGACY_MARKERS = {
    "agent/auxiliary_client.py":    "_call_route_locked_once",
    "agent/plugin_llm.py":          "bind_invocation",
    "agent/turn_context.py":        "set_plugin_invocation_agent",
    "cli.py":                       "plugin_invocation_scope_for_agent",
    "gateway/run.py":               "plugin_invocation_scope",
    "hermes_cli/plugins.py":        "plugin_invocation_scope",
    "run_agent.py":                 "plugin_invocation_scope",
    "tui_gateway/methods_tools.py": "plugin_invocation_scope_for_agent",
}
_V021_MARKERS = {
    "agent/auxiliary_client.py":    "call_llm_route_locked",
    "agent/plugin_llm.py":          "bind_invocation",
    "agent/turn_context.py":        "set_plugin_invocation_agent",
    "agent/turn_facade.py":         "plugin_invocation_scope",
    "cli.py":                       "plugin_invocation_scope_for_agent",
    "gateway/run_inbound.py":       "plugin_invocation_scope_for_agent",
    "hermes_cli/plugins.py":        "plugin_invocation_scope",
    "tui_gateway/methods_tools.py": "plugin_invocation_scope_for_agent",
}
PATCH_MARKERS = {
    "invocation-route-v2026.8.16.patch": _LEGACY_MARKERS,
    "invocation-route-v2026.8.31.patch": _LEGACY_MARKERS,
    "invocation-route-v0.21.0.patch":    _V021_MARKERS,
}
```

### Call sites to migrate

24 references in `install.py` (`PATCH_FILES`, `FILE_MARKERS`, `ALL_PATCH_CONTENT`,
`PATCH_TEST_FILE`, `applied_patch_files`) and 16 in `tests/run_tests.py`. The shape:

1. `applied_patch_files(src, patch_name)` — per-patch, not global.
2. `classify_host` — evaluate each candidate topology; `patched` when **any** is
   complete, `partial` only when one is genuinely mid-application. Keep the
   existing ordering rule: not-a-checkout → patched → partial → marker-outside →
   dirty → applicability → refuse.
3. Backup: back up the union of all topologies (a superset is safe). Restore stays
   limited to what the zip actually contains, which `do_rollback` already does.
4. Rollback: prefer the topology named by `meta["host"]["patch"]`.
5. Messages that say `N/8` must read the selected topology's own length.
6. `plugin_files()` must ship the new patch once it moves into `assets/`.

### Tests the migration needs

- a 0.21.0-shaped host patched with `_V021_MARKERS` classifies **`patched`**, not
  `partial` (this is the regression that motivates the work);
- an 8.x-shaped host still classifies `patched` with the legacy markers;
- a host with `gateway/run.py` marked and nothing else stays `partial`;
- `--status` never prints `N/8` from a topology it did not select;
- rollback restores only files the recorded patch touched.

---

## Then, and only then

Move the patch to `assets/invocation-route-v0.21.0.patch`, confirm
`install.py --status` reports `stock` (not `incompatible`) on 693641aa8b, apply it in
a disposable profile, and confirm a proposal run reaches the model. Until that is
done the README's version matrix must keep saying proposals do not work on 0.21.0,
because they do not.

`_patch_sort_key` extracts digits, so `invocation-route-v0.21.0.patch` sorts as
`(0, 21, 0)` — *below* `(2026, 8, 31)`. That is harmless (the 8.x patches simply
fail `--check` on a 0.21.0 host and it falls through to this one) but it means the
"newest base first" comment no longer describes the ordering. Either rename the file
to the host's date-shaped version or make the sort key aware of the scheme.
