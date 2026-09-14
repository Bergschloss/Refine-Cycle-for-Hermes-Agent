# The host route patch

Why this plugin needs a patch at all, what the patch adds, and how to rebase it when it stops applying.

## The problem it solves

A Hermes plugin can call `ctx.llm`. It cannot ask for the invocation the current turn is already bound to — the same provider, model, base URL, API mode and client object the host is using right now. There is no API for that.

Refine needs it. A plugin that proposes edits to the agent's own memory using some *other* model is proposing with a model the user did not choose for the job, possibly on a key they did not intend to spend. So the plugin refuses to run unbound: without the patch, `refine_run` stops at `llm_invocation_unavailable` and does nothing.

This cannot be solved plugin-side. The invocation lives in the agent object that the host builds per turn, and nothing hands it to a plugin.

## Shape

`assets/invocation-route-v2026.9.10.patch`. Verified applying to base `f364c19775` (the current release channel) and `cbd03e6e4c`.

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

Bundled patches so far: `v0.21.0`, `v2026.8.16`, `v2026.8.31`, `v2026.9.10`.

## Verifying a live host

```bash
python install.py --status
```

Reports the base commit, which patch applies, whether all 8 targets carry markers, and whether the plugin is installed. It changes nothing.

A host where the markers are missing will run the plugin, register its hooks and answer `/refine status` — and fail every proposal with `llm_invocation_unavailable`. The plugin warns about this at registration, but the warning is easy to miss in a busy log, so check the status output rather than trusting the absence of errors.

## Upstreaming

Filed as [NousResearch/hermes-agent#109499](https://github.com/NousResearch/hermes-agent/issues/109499). If the host ever exposes the turn-bound invocation itself, every patch in `assets/` can be deleted and this document with them.
