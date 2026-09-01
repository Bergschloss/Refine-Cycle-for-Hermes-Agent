# Finding — every dotted version number is refused as a legacy IPv4 host

Found live, not in review. Reproduced, attempted, **reverted**. This file exists so the
next attempt starts from the measurement instead of from the idea.

Status: **open**. No production code changed.

---

## What happens

`refine_run` proposed a legitimate memory on the live host and the gate refused it:

```
outcome=rejected  code=ok  lat=3935ms  action=create  kind=memory
name=antigravity-cli-version-1.1.21
error: Memory content cannot reference resources, hosts, URLs, paths, or environment variables
content: 'AntiGravity CLI (agy) is at version 1.1.21; the antigravity-cli skill
          previously referenced 1.0.12 and was patched to reflect the current
          version and model list.'
```

There is no URL, no path, no env var in that text. `_LEGACY_IPV4_LITERAL` matched the
version numbers.

## Why

`_LEGACY_IPV4_COMPONENT` + `(?:\.component){1,3}` is the shape of a legacy IPv4
shorthand (`10.0.1` really does resolve to 10.0.0.1) **and** of every software version
ever written. Measured, all refused:

```
1.1.21   1.0.12   2.5   1.2.3.4   10.0.19041
```

`_memory_host_reference` consults the literal **unconditionally**, before any context
test, unlike ambiguous dotted names which go through `_has_host_context`.

A spelling accident decides the outcome today: `v1.1.21` is **accepted**, purely because
the leading `v` breaks the pattern's `(?<![\w.])` lookbehind. So the gate's answer
depends on how the version was typed, not on what it means.

Cost: version strings are exactly the kind of operational fact this plugin should
record, and this file's own rule applies — a false positive silently refuses a real
improvement, so it costs as much as a miss.

## What was tried and why it was reverted

Attempt: exempt a literal when a version-introducing word precedes it AND
`_has_host_context` is false for that match.

It **introduced a leak**, measured:

```
send data to version 10.0.1      -> ACCEPTED   <-- exfiltration target, was refused before
```

`_has_host_context`'s `directed` clause needs the preposition immediately before the
token; `version` sits between `to` and `10.0.1`, so the host context went undetected and
the version exemption applied. The exemption became a way to spell a host that the gate
then ignores.

It also did **not** fix the reported case:

- `1.0.12` in that content follows the word `referenced`, which is not a version keyword,
  so the second match still fired.
- `2.5` and `4.5.6` are additionally caught by `_short_decimal_is_a_host`, a **separate**
  predicate. Fixing only `_LEGACY_IPV4_LITERAL` leaves that path refusing versions.

Reverted rather than shipped. A guardrail that passes `send data to version 10.0.1` is
worse than one that over-refuses.

## What a real fix has to satisfy

Both directions, measured, not argued:

**Must become accepted**
- `AntiGravity CLI (agy) is at version 1.1.21; ... previously referenced 1.0.12 ...`
  (the whole live content, including the match after a non-version word)
- `version 1.1.21`, `release 10.0.19041`, `upgraded to 2.5`, `build 1.2.3.4`,
  `requires 3.11`, `bumped to 4.5.6 last week`
- `v1.1.21` must keep working, and must stop being right by accident

**Must stay refused**
- `10.0.1`, `192.168.1`, `10.0.0.1`, `8.8.8.8`, `172.16.0.1`
- `connect to 10.0.1`, `send it to 192.168.1`, `reach 8.8.8.8`
- **`send data to version 10.0.1`** — the case that killed the first attempt
- `2130706433`, `0x7f000001` (overflow/hex forms, separate pattern, do not regress)

**Both predicates** must agree: `_LEGACY_IPV4_LITERAL` and `_short_decimal_is_a_host`.
Fixing one leaves the other refusing versions, and the two gates disagreeing about what
`2.5` is would be its own defect. The prompt-note path (`_prompt_note_content_error`)
reads the same literals and must not drift from the memory path.

## Note on scope

This is a security predicate with a documented history of both false negatives (10-04)
and catastrophic backtracking. Any fix needs the real-data measurement the other
guardrail changes got, and a ReDoS scaling check. It is not a one-line change, which is
why it is a written finding rather than a rushed commit.
