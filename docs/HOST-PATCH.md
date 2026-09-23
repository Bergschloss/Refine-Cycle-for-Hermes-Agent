# The host route patch

Why this plugin needs a patch at all, what the patch adds, and how to rebase it when it stops applying.

## The problem it solves

A Hermes plugin can call `ctx.llm`. It cannot ask for the invocation the current turn is already bound to — the same provider, model, base URL, API mode and client object the host is using right now. There is no API for that.

Refine needs it. A plugin that proposes edits to the agent's own memory using some *other* model is proposing with a model the user did not choose for the job, possibly on a key they did not intend to spend. So the plugin refuses to run unbound: without the patch, `refine_run` stops at `llm_invocation_unavailable` and does nothing.

This cannot be solved plugin-side. The invocation lives in the agent object that the host builds per turn, and nothing hands it to a plugin.

## Shape

`assets/invocation-route-v2026.9.23.patch`, for Hermes main from `2e1afdf0ec` (0.21.4): the 9.16 revision rebased onto a Hermes that now binds the session env around plugin commands, on the gateway and in the TUI, in the same spots the patch binds the invocation route. Both bindings are kept, the route nested inside. `assets/invocation-route-v2026.9.16.patch`, for Hermes main from `4e9d3c713a`: the 9.14 revision re-anchored, with identical added and removed lines. `assets/invocation-route-v2026.9.14.patch` covers Hermes v2026.9.14 (0.21.3) and main from `1c671beab2`. Hosts before that take `invocation-route-v2026.9.10.patch` (`f364c19775`, `cbd03e6e4c`) or `invocation-route-v0.21.0.patch` (the 0.21.1 release tag). The numbers below describe the 9.10 revision; the 9.14 one is the same change re-anchored, minus one hunk upstream now carries itself.

**+977 / −14.** Of the additions, 458 lines are a new test file. The core change is about 519 added lines and 13 rewritten call sites. It is close to purely additive: it introduces new symbols and wraps existing dispatch, rather than changing how anything already works.

| File | Added | Removed | What it adds |
|---|---|---|---|
| `agent/plugin_llm.py` | 187 | 1 | `PluginInvocationRoute`, `BoundPluginLlm`, `PluginLlmInvocationError`, `bind_invocation()` |
| `agent/auxiliary_client.py` | 144 | 0 | `call_llm_route_locked()`, `RouteLockedCallError`, `classify_route_locked_error()` |
| `hermes_cli/plugins.py` | 106 | 3 | `plugin_invocation_scope()`, `set_plugin_invocation_route()`, `set_plugin_invocation_agent()` |
| `gateway/run_inbound.py` | 37 | 4 | Opens the scope around gateway command dispatch |
| `agent/turn_context.py` | 15 | 0 | `_publish_plugin_invocation_agent()` |
| `tui_gateway/methods_tools.py` | 11 | 3 | Opens the scope around TUI command dispatch |
| `agent/turn_facade.py` | 10 | 1 | Opens the scope around the turn |
| `cli.py` | 9 | 2 | Opens the scope around CLI command dispatch |
| `tests/agent/test_plugin_invocation_route.py` | 458 | 0 | New file |

The four entry points each get the same treatment: wrap plugin dispatch in `plugin_invocation_scope(...)` so a plugin called from there can find the active route. That is what the 13 rewritten lines are.

## The contract

The added code is deliberately strict, and the test names state the rules:

- **A route that cannot name where it goes is refused.** Missing provider, model, base URL or API mode means no binding.
- **A provider of `auto` is not an explicit route.** Resolution has to have already happened.
- **A route is immutable after capture.** `PluginInvocationRoute` is a frozen dataclass.
- **Outside any scope the facade is not bound.** No ambient binding, no leakage between turns.
- **A bound facade refuses overrides.** `_reject_bound_overrides` blocks a caller trying to change provider or model after binding.

The plugin reads exactly one thing from this: `llm.invocation_bound`. Everything else is the host's.

## When it stops applying

Two different failures, often confused.

**The patch was overwritten.** `hermes update` replaces the eight files with clean copies. The patch still applies; it simply is not applied any more. Send `/refine update` in chat, then `/restart`, or run:

```bash
python install.py --patch-only
```

`/refine update` decides nothing itself. It reads `install.py --status --json` from the newest release it has, runs `--patch-only` when the state is anything but `patched` or `incompatible`, and reports what the installer said. The installer keeps its own refusals: a checkout with hand-edited patch targets, and one no bundled patch fits.

**The patch no longer applies.** Upstream moved the surrounding code far enough that the context no longer matches. `install.py --status` says so and names every bundled patch it tried. This needs a rebase.

## Rebasing

1. Find the new base: `git -C <hermes> rev-parse HEAD`.
2. Apply the newest bundled patch with `git apply --3way` and resolve what conflicts. Because the patch is nearly all additions, conflicts are usually in the four entry points, not in the new modules.
3. Run the patch's own test file against the patched host. It is written to fail loudly if the binding is wrong rather than silently returning an unbound facade.
4. Regenerate the patch, name it `invocation-route-v<version>.patch`, and add it to `assets/`. The installer picks by applicability, not by version number, so old patches stay and keep working for old hosts.
5. Verify: `python install.py --status` should report all 8 targets carrying markers.

Bundled patches so far: `v0.21.0`, `v2026.8.16`, `v2026.8.31`, `v2026.9.10`, `v2026.9.14`, `v2026.9.16`, `v2026.9.23`.

### A merge without conflicts is not a working patch

Rebasing onto v2026.9.14 produced one conflict and seven clean merges, and every file compiled. One of the clean merges was broken: Hermes had moved plugin-command dispatch into its own method, and the patch's session lookup still read a local variable only the old method defined. The gateway catches exceptions around that dispatch, so the `NameError` meant every plugin slash command on the gateway quietly stopped running, while the CLI and TUI kept working.

The same defect was already in the `v0.21.0` and `v2026.9.10` revisions: Hermes split that method in v2026.9.7, and both patches were re-anchored across the split without anything driving the gateway path. Both are fixed, and all three patches' test files now run the real gateway dispatcher, so the next rebase fails there instead of on a user's host.

Two lessons for the next rebase. Read every merged call site against the new host, not just the conflicts. And run the patch's own test file on the rebased tree before generating the patch.

### When two patches fit one host

The installer takes the newest patch that applies. The 9.14 revision would otherwise also apply to 9.10-era hosts, where it is missing the await-thread `copy_context` hunk that upstream only added later. So it carries a one-line comment next to upstream's own `copy_context` call: on a host without that call the hunk has no context, the patch does not apply, and the host gets the revision that brings the copy itself.

### Replacing a superseded revision

A host that already carries an earlier revision has every marker, so markers alone call it patched and a fixed patch could never reach it. When no bundled patch reverses out of a fully marked host, `--status` reports `outdated`. `--patch-only` (and `/refine update`, which runs it) records a backup, restores only that patch's files from the checkout's HEAD, removes the test file the patch created, and applies the current revision. The first backup stays the rollback target.

## Verifying a live host

```bash
python install.py --status
```

Reports the base commit, which patch applies, whether all 8 targets carry markers, and whether the plugin is installed. It changes nothing.

A host where the markers are missing will run the plugin, register its hooks and answer `/refine status` — and fail every proposal with `llm_invocation_unavailable`. The plugin warns about this at registration, but the warning is easy to miss in a busy log, so check the status output rather than trusting the absence of errors.

## Upstreaming

Filed as [NousResearch/hermes-agent#109499](https://github.com/NousResearch/hermes-agent/issues/109499). If the host ever exposes the turn-bound invocation itself, every patch in `assets/` can be deleted and this document with them.
