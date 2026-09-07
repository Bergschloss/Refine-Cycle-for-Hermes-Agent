# Hermes 0.21.0 invocation route — delivered and verified

**Status:** delivered in `assets/invocation-route-v0.21.0.patch`; selectable by
both installers; classified, backed up, verified, and rolled back through
patch-specific metadata.

The implementation targets stock Hermes 0.21.0 commit
`693641aa8b4359c602283bdbbc14041e03bc47bc`. All mutation tests used disposable
clones and `HERMES_HOME` directories. The real checkout was not patched or
restarted.

## Acceptance evidence

| Check | Result |
|---|---|
| Real Hermes scanner on the tracked plugin tree | `caution`, 153 findings, 0 critical |
| `git apply --check` on stock 0.21.0 | passes |
| `install.py --status` before install | `stock — clean base 693641aa8b; invocation-route-v0.21.0.patch applies` |
| Full disposable install | `stock` → `patched` |
| Patched status | all 8 route markers present |
| Host route tests | 37 passed |
| Invocation-bound proposer smoke | installed proposer reached exactly once |
| Full plugin suite | 1,203 passed, 11 skipped |
| Rollback | patch-created host test removed; tracked host diff empty |

The 11 plugin-suite skips are the expected Windows skips for Bash-based
`install.sh` tests. Linux CI exercises those paths.

## Route guarantees

| Guarantee | Implementation |
|---|---|
| exact active provider, model, and client | `PluginInvocationRoute.from_agent` captures the active agent client instead of rebuilding it |
| immutable binding | the route is a frozen dataclass |
| no ambient-config fallback | incomplete routes fail closed before dispatch |
| no provider/model override | bound overrides are rejected before the trust gate |
| one physical request | each route-locked attempt invokes the captured client once |
| no hidden retry or provider failover | the route-locked path bypasses the normal recovery ladder |
| sync and async identity | async dispatch runs the same captured sync client through `asyncio.to_thread` |
| context propagation | plugin command threads receive a copied context |

The verified transport modes are OpenAI-shaped chat, `anthropic_messages`, and
`codex_responses`. Each mode uses the client already captured from the active
invocation; none reconstructs a client from ambient provider configuration.

## Patch-specific installer metadata

Hermes 0.21.0 moved two route targets:

| Legacy topology | Hermes 0.21.0 topology |
|---|---|
| `gateway/run.py` | `gateway/run_inbound.py` |
| `run_agent.py` | `agent/turn_facade.py` |

`PATCH_MARKERS` now maps each patch to its own marker table. Classification
checks every candidate topology and reports `patched` when one complete topology
matches. It reports `partial` only for a genuinely incomplete known topology.
Messages use the selected topology's own marker count.

Backup, dirty-tree checks, compilation, and rollback scope are derived from the
selected patch headers, including the patch-created
`tests/agent/test_plugin_invocation_route.py`. `ensure_patch_transaction`
prevents an existing backup from being reused with a different patch or target
topology. Every applied edit therefore retains the backup required for exact
rollback.

## Clean application only

Both installers select the 0.21.0 patch and require a clean `git apply` result.
They do not use `git apply -3`, reduced context, or another semantic merge. A
merge that merely compiles could silently violate the exact-client,
one-request, or no-fallback contract, so an unsupported host is refused rather
than approximated.

After application, the installer verifies markers and conflict absence, compiles
every touched Python file, imports the route code, and runs the synthetic
invocation-bound proposer smoke in a disposable `HERMES_HOME`. A failure restores
the recorded pre-install state.

## Privacy and production boundaries

The proposer smoke uses synthetic fixtures only. No real session database or
trajectory content is sent to a model or external service. The real Hermes
checkout and gateway remain untouched; a live gateway restart was neither needed
nor performed for this verification.
