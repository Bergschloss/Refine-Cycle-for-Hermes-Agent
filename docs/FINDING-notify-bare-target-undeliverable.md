# Finding — a bare `notify_target` never delivers, and used to do so silently

Status: **resolved.** Code fix landed in `40ede51`; the remaining half is host configuration,
not code. Measured on the live server, not argued.

## What was wrong

Every A2 notification had been failing on the reference host. Not intermittently — all of them.

`config.notify_target()` defaults to `"telegram"`, a bare platform name. Hermes can only route a
bare platform name when that platform has a home channel configured. `TELEGRAM_HOME_CHANNEL` was
commented out in `~/.hermes/.env`, so `send_message_tool` returned:

```
No home channel set for telegram to determine where to send the message.
```

`cmd_send` exited 1, `notify()` returned False, and nothing above `debug` was logged. `cmd_send`'s
own explanation never reached the log either, because `_build_send_namespace` sets `quiet=True`
and `_emit_result` suppresses the reason in that mode.

The full suite was green throughout. This is the same silent-inertness class as the hardcoded
`~/.hermes` path this plugin shipped once before, and the same
"invisible on synthetic input, obvious on real data" trap as the 398-hit bogus pattern.

## What was verified, and how

Against the real `hermes_cli` on v2026.8.31, in-process on the server:

- `cmd_send` reads exactly seven attributes, all via `getattr(args, ...)`:
  `list_targets`, `message`, `json`, `to`, `file`, `subject`, `quiet`. `_build_send_namespace`
  sets exactly those seven. The transport code was never wrong.
- `cmd_send` calls `sys.exit()` on success as well as failure, so the exit code is the only
  success signal — `notify._send` is right to key on it.
- With an explicit channel target, delivery succeeds immediately: `notify() -> True`, message
  received.
- `core._notify_lesson` renders `/refine-cycle rollback <id>` once `register()` has installed the
  command-name provider, matching what the live gateway actually registered. Without `register()`
  it falls back to `/refine`, which is the documented offline default, not a defect.

## Resolution

Two halves, and only the first is a repo concern.

**Code (`40ede51`).** A failed send is reported once per process at `WARNING`, naming the target,
the exit code and the remedy; a broken coupling and a timeout report the same way. Latched,
because a misconfigured target fails on every applied edit and a warning per edit trains the
operator to filter the log. `_send` carries the exit code out instead of collapsing it into the
bool. `cmd_send`'s own message is deliberately **not** captured: that would require redirecting
process-wide `sys.stdout` from a worker thread while the gateway is writing to it.

**Host configuration.** Point the target at a concrete channel:

```yaml
plugins:
  entries:
    refine:
      notify_target: telegram:Taras
```

`hermes send --list` shows the available targets. Setting `TELEGRAM_HOME_CHANNEL` instead would
also work and would fix the bare name for the rest of Hermes, but it changes cron delivery
routing too, so the refine-scoped key is the narrower change.

## Still open, deliberately

`install.sh` (A1) sends to `${REFINE_NOTIFY_TARGET:-telegram}` — the same bare name, so the
install message does not arrive on a host with no home channel. It is non-fatal by design, so the
install itself still succeeds. Pass the target explicitly when it matters:

```
REFINE_NOTIFY_TARGET=telegram:Taras ./install.sh
```

Left as-is because the spec pins that text and mechanism, and teaching a shell script to parse
the plugin's YAML config to discover a fallback would cost more than the cosmetic message is
worth.
