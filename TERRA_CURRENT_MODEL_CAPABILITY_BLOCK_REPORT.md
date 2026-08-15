# Current Invocation Model — Local Stage 0 Block Report

## Outcome

**Blocked locally: the exact target runtime was unavailable, so no Refine implementation was attempted.**

This is an environment/identity block, not a final capability verdict for the target server. The required behavior is to execute Refine with the exact host-resolved LLM route of the session that invoked `/refine` or `refine_run`. The only runtime accessible from this workspace was Hermes `0.16.0`; the expected gateway-imported target is `0.17.x` and remains unverified.

Stopping was the correct action. Substituting the local runtime for the target, or implementing against gateway/process defaults, would be an unsupported approximation.

## Runtime identity inspected

| Field | Observed value |
|---|---|
| Python interpreter | `C:\Users\relig\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe` |
| Hermes distribution | `hermes-agent 0.16.0` |
| Required target inspection | exact gateway-imported Hermes `0.17.x` runtime |
| `PluginContext` source | `C:\Users\relig\AppData\Local\hermes\hermes-agent\hermes_cli\plugins.py` |
| `PluginLlm` source | `C:\Users\relig\AppData\Local\hermes\hermes-agent\agent\plugin_llm.py` |
| Gateway dispatch source | `C:\Users\relig\AppData\Local\hermes\hermes-agent\gateway\run.py` |
| Tool registry source | `C:\Users\relig\AppData\Local\hermes\hermes-agent\tools\registry.py` |

The target server's interpreter, imported distribution/build, and module paths were not available from this workspace. Therefore:

- local `0.16.0` capability: required public facade not found;
- target `0.17.x` capability: **unverified**;
- Refine implementation: correctly not attempted.

## Local public API evidence

The inspected local signatures are:

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

These local registration APIs do not establish an invocation-bound command/tool context or opaque host route. `PluginLlm.complete` accepts explicit strings but no session/invocation route handle.

Read-only local source inspection also established that command callbacks receive only raw arguments; ordinary tool handlers receive tool arguments plus task metadata, not an exact resolved route/client. The plugin-scoped default LLM path uses gateway/process runtime-main metadata, while complete session route bundles are private gateway state.

This evidence must not be presented as proof of the target `0.17.x` contract.

## Why no Refine-only approximation was made

The required capability is an opaque, non-secret, invocation-bound LLM facade that:

1. is passed to command, model-tool, and supported automatic-hook callbacks;
2. preserves the host's resolved provider/model/endpoint/API-mode/profile/client route;
3. supports Refine's structured completion behavior; and
4. returns authoritative non-secret provider/model metadata from the actual call.

Provider/model strings alone omit endpoint, profile, credentials, API mode, client behavior, and actual fallback/alias attribution. Private gateway fields, `state.db`, journal history, environment guesses, hook caches, or process-global runtime-main state would be stale, private, concurrency-unsafe, or the wrong scope.

## Safe action taken

- No Refine or Hermes source code was edited.
- No global/default model configuration or daily budget was changed.
- No provider/model call was made.
- No state database, private configuration, credentials, trajectory, live journal, or ledger was read.
- No rollback was run and no historical attribution was changed.

## Next mandatory target-server gate

Inspect the exact runtime imported by the running target gateway. Record its interpreter, distribution/build, module paths, command/tool/hook callback invocation sites, and public completion/result contract.

Classify exactly one result:

1. `environment_block`: exact gateway runtime identity cannot be proven; target capability remains unverified;
2. `capability_block`: exact target is proven and lacks the required public route; no Refine implementation;
3. `capability_pass`: exact target exposes a supported opaque invocation-bound facade with authoritative actual-call metadata; only then begin Refine tests and implementation.

If result 2 is confirmed, a separately approved Hermes host change should provide a public task-local invocation context, conceptually:

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

The facade must be bound before plugin command/tool dispatch, remain correct for concurrent sessions, keep credentials host-owned, support the required completion options, and report the actual provider/model used after aliases, retries, or fallback. Unsupported contexts must expose no fabricated default route.
