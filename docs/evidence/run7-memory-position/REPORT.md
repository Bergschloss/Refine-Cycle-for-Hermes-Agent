# Step 4 Memory Position Experiment Report

**Execution Timestamp UTC**: 2026-09-12T13:21:09.396520+00:00
**Frozen Checker SHA-256**: `d2834b94bf95a15a9dde118387ab348a64deeeabd4e1e2683d0b3f73d33ac7bc`
**Pre-Registration SHA-256**: `96dbce793488faaf3d677dd66bec70667a62d386b890a5c2d0e7c4864819135b`
**Configured Host Memory Char Limit**: `20000` chars (confirmed no truncation)

## 1. L0 Validity Gate

* **L0 Gate (`L0 >= 10/12`)**: PASSED (observed 11/12 passes)
* **Reproduced Previous Run**: NO — 11/12 vs 12/12
* **Anchor Status**: PASSED — anchor confirmed, proceeding to position analysis

## 2. Per-Level Results

| Level | Target Chars | Actual Length | Before Target | After Target | Imbalance | Trials | Misfires | Scored | Passes | Pass Rate | Band |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `L0` | 150 | 161 | 0c | 0c | 0c | 12 | 1 | 11 | 11 | 100.0% | **holds** |
| `L1` | 1100 | 1119 | 482c | 476c | 6c | 12 | 1 | 11 | 11 | 100.0% | **holds** |
| `L2` | 2200 | 2246 | 1037c | 1048c | 11c | 12 | 1 | 11 | 10 | 90.9% | **holds** |
| `L3` | 4400 | 4432 | 2178c | 2093c | 85c | 12 | 1 | 11 | 11 | 100.0% | **holds** |
| `L4` | 8800 | 8811 | 4365c | 4285c | 80c | 12 | 1 | 11 | 10 | 90.9% | **holds** |

**Balance Note**:
* `L1`: target is at offset 482c before / 476c after (imbalance 6c, 0.5% of total).
* `L2`: target is at offset 1037c before / 1048c after (imbalance 11c, 0.5% of total).
* `L3`: target is at offset 2178c before / 2093c after (imbalance 85c, 1.9% of total).
* `L4`: target is at offset 4365c before / 4285c after (imbalance 80c, 0.9% of total).

## 3. Per-Probe Outcome Matrix

| Probe ID | `L0` (161c) | `L1` (1119c) | `L2` (2246c) | `L3` (4432c) | `L4` (8811c) |
|---|:---:|:---:|:---:|:---:|:---:|
| `probe_51ad58a9a362_01` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_02` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_03` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_04` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_05` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_06` | PASS | PASS | FAIL | PASS | PASS |
| `probe_51ad58a9a362_07` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_08` | PASS | PASS | PASS | PASS | FAIL |
| `probe_51ad58a9a362_09` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_10` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_11` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_12` | MISFIRE | MISFIRE | MISFIRE | MISFIRE | MISFIRE |

## 4. Side-by-Side: Middle Position vs First Position (`step3-memory-load`)

| Level | Actual Chars | Step 3 (First Position) | Step 4 (Middle Position) | Difference |
|---|:---:|:---:|:---:|:---:|
| `L0` | 161 | 12/12 (100.0%) | 11/12 (91.7%) | -1 |
| `L1` | 1119 | 12/12 (100.0%) | 11/12 (91.7%) | -1 |
| `L2` | 2246 | 12/12 (100.0%) | 10/12 (83.3%) | -2 |
| `L3` | 4432 | 12/12 (100.0%) | 11/12 (91.7%) | -1 |
| `L4` | 8811 | 12/12 (100.0%) | 10/12 (83.3%) | -2 |

## 5. Degradation Limit

No level degraded up to 8800 characters in middle position (all levels scored >= 7/12).

## 6. What Each Outcome Licenses

Pre-registered outcome case that applies (quoted verbatim from `PRE_REGISTRATION.md`):

```
  holds at every level, as in the first-position run
      -> position does not matter at these sizes, and the two runs together
         support raising the limit. The reason we have been giving users for
         keeping it at 4400 is wrong and should be corrected publicly.
```
