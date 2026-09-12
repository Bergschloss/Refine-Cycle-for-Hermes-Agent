# Step 2 Wording Rule Experiment Report

**Execution Timestamp UTC**: 2026-09-12T12:06:17.159363+00:00
**Frozen Checker SHA-256**: `d2834b94bf95a15a9dde118387ab348a64deeeabd4e1e2683d0b3f73d33ac7bc`
**Pre-Registration SHA-256**: `824cd021f640ae96e65fe15f3f152ef07274be65bcb25b174a4f3c3e47b00729`

# Fingerprint: `fb25ce8f2797` (Policy: `report_blocker`)

## 1. Validity Gates

* **Gate 1 (`nothing <= 1/12`)**: PASSED (observed 0/12)
* **Gate 2 (`explicit >= 9/12`)**: PASSED (observed 12/12)
* **Fingerprint Validity**: PASSED — proceeding to analysis

## 2. Per-Arm Counts

| Arm | Trials | Misfires | Scored | Passes | Pass Rate |
|---|:---:|:---:|:---:|:---:|:---:|
| `nothing` | 12 | 0 | 12 | 0 | 0.0% |
| `as_emitted` | 12 | 0 | 12 | 2 | 16.7% |
| `rewritten` | 12 | 0 | 12 | 0 | 0.0% |
| `explicit` | 12 | 0 | 12 | 12 | 100.0% |

## 3. Per-Probe Outcome Matrix

| Probe ID | `nothing` | `as_emitted` | `rewritten` | `explicit` |
|---|:---:|:---:|:---:|:---:|
| `probe_fb25ce8f2797_01` | FAIL | PASS | FAIL | PASS |
| `probe_fb25ce8f2797_02` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_03` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_04` | FAIL | PASS | FAIL | PASS |
| `probe_fb25ce8f2797_05` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_06` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_07` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_08` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_09` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_10` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_11` | FAIL | FAIL | FAIL | PASS |
| `probe_fb25ce8f2797_12` | FAIL | FAIL | FAIL | PASS |

## 4. Paired Results

| Contrast | Matched Pairs | Discordant (n10 / n01) | Exact McNemar / Binomial p-value | Pooled Fisher (Secondary) |
|---|:---:|:---:|:---:|:---:|
| **rewritten vs as_emitted** (Primary) | 12 | n10=0, n01=2 (total=2) | **p = 0.5** | p = 0.4783 |
| **rewritten vs explicit** (Secondary) | 12 | n10=0, n01=12 (total=12) | **p = 0.0004883** | p = 7.396e-07 |

## 5. Pre-Registered Decision Rule Fired Branch

```
  rewritten <= 4/12
      -> the rule does not hold for this policy
```

* Observed counts: `rewritten` = 0/12, `as_emitted` = 2/12, `explicit` = 12/12
* Note: `rewritten` is weaker than `explicit` by 12 passes (0/12 vs 12/12).

---

# Fingerprint: `2b4ff368ae22` (Policy: `verify_before_retry`)

## 1. Validity Gates

* **Gate 1 (`nothing <= 1/12`)**: PASSED (observed 0/12)
* **Gate 2 (`explicit >= 9/12`)**: PASSED (observed 12/12)
* **Fingerprint Validity**: PASSED — proceeding to analysis

## 2. Per-Arm Counts

| Arm | Trials | Misfires | Scored | Passes | Pass Rate |
|---|:---:|:---:|:---:|:---:|:---:|
| `nothing` | 12 | 0 | 12 | 0 | 0.0% |
| `as_emitted` | 12 | 0 | 12 | 0 | 0.0% |
| `rewritten` | 12 | 0 | 12 | 0 | 0.0% |
| `explicit` | 12 | 0 | 12 | 12 | 100.0% |

## 3. Per-Probe Outcome Matrix

| Probe ID | `nothing` | `as_emitted` | `rewritten` | `explicit` |
|---|:---:|:---:|:---:|:---:|
| `probe_2b4ff368ae22_01` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_02` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_03` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_04` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_05` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_06` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_07` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_08` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_09` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_10` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_11` | FAIL | FAIL | FAIL | PASS |
| `probe_2b4ff368ae22_12` | FAIL | FAIL | FAIL | PASS |

## 4. Paired Results

| Contrast | Matched Pairs | Discordant (n10 / n01) | Exact McNemar / Binomial p-value | Pooled Fisher (Secondary) |
|---|:---:|:---:|:---:|:---:|
| **rewritten vs as_emitted** (Primary) | 12 | n10=0, n01=0 (total=0) | **p = 1** | p = 1 |
| **rewritten vs explicit** (Secondary) | 12 | n10=0, n01=12 (total=12) | **p = 0.0004883** | p = 7.396e-07 |

## 5. Pre-Registered Decision Rule Fired Branch

```
  rewritten <= 4/12
      -> the rule does not hold for this policy
```

* Observed counts: `rewritten` = 0/12, `as_emitted` = 0/12, `explicit` = 12/12
* Note: `rewritten` is weaker than `explicit` by 12 passes (0/12 vs 12/12).

---

# Synthesis: What Each Outcome Licenses

Pre-registered outcome case that applies (quoted verbatim):

```
  neither confirms
      -> the 51ad58a9a362 result does not generalise, and the wording programme
         ends here. Publish that.
```
