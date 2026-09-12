# 2x2 Factorial Experiment Report: Fingerprint 51ad58a9a362

**Execution Timestamp UTC**: 2026-09-12T10:59:47.567054+00:00
**Frozen Checker SHA-256**: `d2834b94bf95a15a9dde118387ab348a64deeeabd4e1e2683d0b3f73d33ac7bc`
**Pre-Registration SHA-256**: `c4314292363c492dccb7f633c6fbc0af0d702f0db39fc6dd7c83d664400fd4f8`

## 1. Validity Gates

* **Gate 1 (`nothing <= 1/12`)**: PASSED (observed 0/12)
* **Gate 2 (`D >= 9/12`)**: PASSED (observed 12/12)
* **Overall Validity**: PASSED — proceeding to analysis

## 2. Per-Arm Counts

| Arm | Trials | Misfires | Scored | Passes | Pass Rate |
|---|:---:|:---:|:---:|:---:|:---:|
| `nothing` | 12 | 0 | 12 | 0 | 0.0% |
| `A` | 12 | 0 | 12 | 1 | 8.3% |
| `B` | 12 | 0 | 12 | 12 | 100.0% |
| `C` | 12 | 0 | 12 | 1 | 8.3% |
| `D` | 12 | 0 | 12 | 12 | 100.0% |

## 3. Per-Probe Outcome Matrix

| Probe ID | `nothing` | `A` | `B` | `C` | `D` |
|---|:---:|:---:|:---:|:---:|:---:|
| `probe_51ad58a9a362_01` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_02` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_03` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_04` | FAIL | FAIL | PASS | PASS | PASS |
| `probe_51ad58a9a362_05` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_06` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_07` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_08` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_09` | FAIL | PASS | PASS | FAIL | PASS |
| `probe_51ad58a9a362_10` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_11` | FAIL | FAIL | PASS | FAIL | PASS |
| `probe_51ad58a9a362_12` | FAIL | FAIL | PASS | FAIL | PASS |

## 4. Paired Results (Primary Analysis)

| Contrast | Matched Pairs | Discordant (n10 / n01) | Exact McNemar / Binomial p-value | Pooled Fisher (Secondary) |
|---|:---:|:---:|:---:|:---:|
| **B vs A** | 12 | n10=11, n01=0 (total=11) | **p = 0.0009766** | p = 9.615e-06 |
| **B vs D** | 12 | n10=0, n01=0 (total=0) | **p = 1** | p = 1 |
| **C vs A** | 12 | n10=1, n01=1 (total=2) | **p = 1** | p = 1 |
| **D vs A** | 12 | n10=11, n01=0 (total=11) | **p = 0.0009766** | p = 9.615e-06 |

## 5. Pre-Registered Decision Rule & Interpretation

* **Decision Rule Fired Branch**:
  > `B >= 8 and A <= 2   -> the literal trigger carries the effect`

* **Interpretation Tree Fired Branch**:
  > `B high, C low    -> the trigger is the active half`

* **Transfers to Production Evaluation**:

  - Condition 1 (B is high against A): `MET` (12/12 vs 1/12)
  - Condition 2 (B is not materially worse than D): `MET` (B=12/12, D=12/12)
  - Overall: **TRANSFERS TO PRODUCTION**
