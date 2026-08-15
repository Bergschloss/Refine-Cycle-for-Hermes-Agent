# Current Invocation Model — Capability Block Report

## Outcome

**Blocked: no Refine implementation was made.**

The required behavior is to execute Refine with the exact host-resolved LLM route of the session that invoked `/refine` or `refine_run`. The available runtime does not meet the plan's Stage 0 identity or public-API requirements. A Refine-only fallback would silently route through a gateway/process default and would be incorrect.

## Runtime identity inspected

| Field | Observed value |
|---|---|
| Python interpreter | `C:\Users\relig\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe` |
| Hermes distribution | `hermes-agent 0.16.0` |
| Expected target for this plan | exact gateway-imported Hermes `0.17.x` runtime |
| `PluginContext` source | `C:\Users\relig\AppData\Local\hermes\hermes-agent\hermes_cli\plugins.py` |
| `PluginLlm` source | `C:\Users\relig\AppData\Local\hermes\hermes-agent\agent\plugin_llm.py` |
| Gateway dispatch source | `C:\Users\relig\AppData\Local\hermes\hermes-agent\gateway\run.py` |
| Tool registry source | `C:\Users\relig\AppData\Local\hermes\hermes-agent\tools\registry.py` |

The accessible runtime is `0.16.0`, not the exact target `0.17.x` runtime required by the plan. The target server's imported runtime was not available from this workspace, so it was not guessed or substituted.

## Public API evidence

The inspected public signatures are:

```python
PluginContext.register_command(
    self, name: str, handler: Callable, description: str = "", args_hint: str = ""
) -> None

PluginContext.register_tool(
    self, name: str, toolset: str, schema: dict, handler: Callable,
    check_fn: Callable | None = None, requires_env: list | None = None,
    is_async: bool = False, description: str = "", emoji: str = "",
    override: bool = False
) -> None

PluginLlm.complete(
    self, messages: List[Dict[str, Any]], *, provider: Optional[str] = None,
    model: Optional[str] = None, temperature: Optional[float] = None,
    max_tokens: Optional[int] = None, timeout: Optional[float] = None,
    agent_id: Optional[str] = None, profile: Optional[str] = None,
    purpose: Optional[str] = None
) -> PluginLlmCompleteResult
```

These registration APIs do not establish an invocation-bound command/tool context or opaque host route. `PluginLlm.complete` accepts explicit strings but no session/invocation route handle.

Prior read-only inspection of this same local runtime established that command callbacks receive only raw arguments; ordinary tool handlers receive tool arguments plus task metadata, not an exact resolved route/client. The plugin-scoped default LLM path uses gateway/process runtime-main metadata, while complete session model/provider bundles are private gateway state.

## Why Refine cannot safely implement the feature here

The missing public capability is an opaque, non-secret, invocation-bound LLM facade that:

1. is passed to command, model-tool, and supported automatic-hook callbacks;
2. preserves the host's resolved provider/model/endpoint/API-mode/profile/client route;
3. supports the structured completion behavior Refine needs; and
4. returns authoritative non-secret provider/model metadata from the actual call.

Provider/model strings alone cannot represent the exact route. They omit host-managed endpoint, profile, credentials, API mode, client behavior, and actual fallback/alias attribution. Reading private gateway fields, `state.db`, journal history, environment guesses, or process-global runtime-main state would be stale, private, unsafe under concurrency, or the wrong scope.

## Mandatory Branch B action taken

- No Refine source code was edited.
- No Hermes source code was edited.
- No global/default model configuration or daily budget was changed.
- No provider/model call was made.
- No state database, private configuration, credentials, trajectory, live journal, or ledger was read.
- No rollback was run and no historical attribution was changed.

## Required Hermes prerequisite

A separately approved Hermes host change must provide a public, task-local invocation context with an opaque host-owned LLM facade, conceptually:

```python
@dataclass(frozen=True)
class PluginInvocationContext:
    session_id: str | None
    session_key: str | None
    platform: str | None
    provider: str              # display metadata only
    model: str                 # display metadata only
    llm: InvocationBoundPluginLlm
```

The facade must be bound before plugin command/tool dispatch, remain correct for concurrent sessions, keep credentials host-owned, support the required completion options, and report the actual provider/model used after aliases, retries, or fallback. Older or unsupported contexts must expose no fabricated default route.

After a compatible Hermes release is installed, rerun Stage 0 against the exact gateway-imported runtime. Only then may the Refine tests and implementation stages begin.
