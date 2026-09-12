# Step 3 Memory Load Experiment Report

**Execution Timestamp UTC**: 2026-09-12T12:46:04.784144+00:00
**Frozen Checker SHA-256**: `d2834b94bf95a15a9dde118387ab348a64deeeabd4e1e2683d0b3f73d33ac7bc`
**Pre-Registration SHA-256**: `a64fb71d0d339cd35a0cb1e45288aa38f3be20e7ba445c0a1e104b58cad95726`
**Configured Host Memory Char Limit**: `20000` chars (host truncation confirmed absent per level)

## 1. Validity Gate at L0

* **L0 Gate (`L0 >= 10/12`)**: PASSED (observed 12/12 passes)
* **Detector Status**: PASSED — detector is working, proceeding to load analysis

## 2. Per-Level Results

| Level | Target Chars | Actual Chars | Trials | Misfires | Scored | Passes | Pass Rate | Band |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `L0` | 150 | 161 | 12 | 0 | 12 | 12 | 100.0% | **holds** |
| `L1` | 1100 | 1119 | 12 | 0 | 12 | 12 | 100.0% | **holds** |
| `L2` | 2200 | 2246 | 12 | 0 | 12 | 12 | 100.0% | **holds** |
| `L3` | 4400 | 4432 | 12 | 0 | 12 | 12 | 100.0% | **holds** |
| `L4` | 8800 | 8811 | 12 | 0 | 12 | 12 | 100.0% | **holds** |

## 3. Per-Probe Outcome Matrix

| Probe ID | `L0` (161c) | `L1` (1119c) | `L2` (2246c) | `L3` (4432c) | `L4` (8811c) |
|---|:---:|:---:|:---:|:---:|:---:|
| `probe_51ad58a9a362_01` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_02` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_03` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_04` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_05` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_06` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_07` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_08` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_09` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_10` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_11` | PASS | PASS | PASS | PASS | PASS |
| `probe_51ad58a9a362_12` | PASS | PASS | PASS | PASS | PASS |

## 4. Degradation Limit

No level degraded up to 8800 characters (all levels scored >= 7/12).

## 5. What Each Outcome Licenses

Pre-registered outcome case that applies (quoted verbatim from `PRE_REGISTRATION.md`):

```
  holds through L4 (8800)
      -> the limit can be raised, and the reason we have been giving users for
         not raising it is wrong. Say so plainly.
```
