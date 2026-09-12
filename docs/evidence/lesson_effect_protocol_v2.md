# Protocol: Causal Effect of Grounded Lessons on Agent Performance (v2)

**Status:** PRE-DATA — every decision rule, threshold, and verdict string below is fixed BEFORE any trial of this study is run.
**Version:** 2.0 (corpus-grounded revision) | **Sources read before design:** REAL_DIALOGUE_REPORT.md, case_results_all_70.json, index.html, diagnostic_verdict.json (Luna + mimo-v2.5 routes) — all from `http://92.5.18.124/refine-qa/`, accessed 2026-09-10.
**Deliverables:** this protocol + `analysis_decider.py` (locked decision script).
**Pre-registration:** record the SHA-256 of THIS file and of `analysis_decider.py` externally before trial #1. The decider digest recorded here at lock: `964fc365a27593cb254097f9df404036cbbf7a605c5c8906e277fd752afe1db9`. No edits after data collection begins; any change voids pre-registration.

---

## 0. What v2 changes — grounded in the measurement data you published

Turn-1 designed against a *described* corpus. v2 is redesigned against the **actual** corpus census (cc-corpus.db, 125 sessions), the 70-trial rerun report (commit `5d2e92b`, baseline `e1d798e` produced zero), and the route-history in `index.html`. Four findings force concrete changes:

1. **Provenance is solved, precisely.** Every grounded proposal carries a stable `pattern_fingerprint` (Section 2.1 lists all 17). The leakage set for lesson *l* is not a fuzzy similarity class — it is *every session in cc-corpus.db whose error traces carry fp(l)*, recoverable from `supporting_session_ids` in the partition manifest. Holdout exclusion (E1) is therefore exact, and n-gram/TF-IDF overlap becomes a secondary screen for authored prompts only.

2. **The corpus cannot supply holdouts.** Of 23 tracked fingerprints, total session-bearings = 32 → **mean 1.39 sessions/fingerprint including source, i.e. 0.39 non-source sessions per fingerprint**. Only 9 non-source occurrences exist corpus-wide, most already consumed by the existing 70-trial run (RESOLVED/ambiguous categories) or self-corrected in-record (outcome contamination). Reaching even 8 strict-exclusion holdouts per fingerprint would require ~2,500 comparable sessions (Section 11). **Authored variant probes are therefore mandatory, not recommended.** The corpus is used to derive lessons, define applicability predicates, and supply failure signatures to reproduce with fresh surface content.

3. **One session dominates.** Session `e04f976d` carries 17 eligible within-session patterns and is the supporting session for clusters 10–23 (lessons 5–17). Under strict fingerprint exclusion, *that one session* is the single leak source for nearly the entire lesson set; excluding it is cheap, but it also means corpus-held-out continuations for those lessons simply do not exist elsewhere in cc-corpus.db.

4. **The effect is route-dependent and can flip sign — documented.** From `index.html` causal audits of the SAME lessons: on `opencode-go/gpt-5.6-luna`, treatment 14/20 (70%) vs nothing 10/20 (50%) vs placebo 8/20 (40%), `treatment_specific_harm=false`, release UNBLOCKED. On `opencode-go/mimo-v2.5`, treatment 6/20 (30%) vs nothing 10/20 (50%) vs placebo 10/20 (50%), `treatment_specific_harm=true`, release BLOCKED. On `openai-codex/gpt-5.6-luna-900k` pre-repair: INCONCLUSIVE because `config.yaml` was not preserved across cloned arms (harness config-divergence). The primary claim is therefore **scoped to one pre-specified route**; the grader must be a *different model family*; and any route change triggers a bridging re-run (Section 8.1, falsification F3).

Additional population facts that shape the design (all from the 70-trial rerun, commit `5d2e92b`):
- **17 grounded lessons**: 16 of kind `prompt`, 1 of kind `memory` (Section 2.1). Primary analysis restricted to prompt-kind; the single memory-kind lesson is a descriptive case.
- **Homogeneous treatments**: 8 of 17 are the verbatim template "When *X*, stop retrying and ask for clarification."; 1 variant ("ask before retrying a third time"). The remaining 8 differ: one (*bash-classifier-denial-recovery*) teaches the **opposite** behavior ("retry the request"), one is diagnose-first, one report-only, one format advice, one argument validation, two endpoint/path verification. Because 9/17 test essentially one behavioral heuristic, a family-stratified sensitivity analysis is mandatory and the generalization claim is bounded accordingly (Sections 1.3, 8.9).
- **Lesson length**: 53–185 characters (7–26 words); 13 of them ≤ 94 chars. Per-lesson placebo/scramble construction is feasible with tight token parity (Section 4).
- **Specificity caveat on the plugin side**: the 0/35 false-positive result rests partly on the guardrail layer (19/35 controls refused *without model invocation*; 16 others produced reviewer-fallback proposals blocked by `would_apply=False`). The memory study therefore injects lessons **by hand into the agent's memory slot**, bypassing the plugin's apply/no-op decision — we are testing the effect of receiving a lesson, not the plugin's triage. This is the correct isolation for the causal question.

Everything below replaces turn-1 wherever the two conflict.

---

## 1. Claims, estimands, hypotheses

**Unit of randomization:** the (item × arm) cell. Each item runs once per arm (crossed/paired design); arm order is randomized within item; every cell uses a fresh agent instance.

### 1.1 Primary claim (scoped)
> An agent that has a grounded prompt-kind lesson in its memory performs better on held-out tasks drawn from the failure domain the lesson addresses than the same agent, on the same tasks, with no lesson — **for the pre-specified inference route** (provider + model + sampling parameters, Section 3.4).

### 1.2 Estimands
- **Primary:** marginal paired risk difference pooled over items and prompt-kind lessons: `RD = P(pass | lesson) − P(pass | nothing)`.
- **Mechanism estimand:** `RD_mech = P(pass | lesson) − P(pass | topic-placebo)`.
- **Secondary:** `RD_scramble = P(pass | lesson) − P(pass | scramble-placebo)`; wrong-arm directionality (exploratory).

### 1.3 Hypotheses (two-sided α = 0.05, Holm-corrected over C1–C3)
- H0a: RD(lesson vs nothing) = 0 — the causal claim.
- H0b: RD(lesson vs topic-placebo) = 0 — mechanism gate (does actionable content beat mere awareness/priming?).
- H0c: RD(lesson vs scramble-placebo) = 0 — secondary (does it beat generic injection cost?).

Pooling assumes exchangeability across items; lesson identity is logged and tested as a heterogeneity covariate (Section 7.5). **Per-lesson inferential claims are out of scope** — with ~8 items/lesson, paired-McNemar power at RD=+0.20 is ≈0.3–0.6%; per-lesson numbers are descriptive only (Section 6.3).

---

## 2. Materials

### 2.1 Lessons under test (verbatim, as emitted at commit `5d2e92b`)
Include a lesson only if: grounded (`grounded=true` with evidence attached), actionable, self-contained, and its derivation provenance is logged. Pre-register the exact included set; no post-hoc cherry-picking.

Prompt-kind (primary set, 16) — all one-line directives unless noted:

| # | Fingerprint | Lesson | Content |
|---|---|---|---|
| 1 | f0db28a1c990 | avoid-nested-shell-quoting | "When editing scripts over SSH, prefer unified format." |
| 2 | c2e322e81368 | bash-exit49-recovery | "When Bash/Python fails twice with the same exit code, stop retrying and ask for clarification." |
| 3 | f0db28a1c990 | bash-quote-validation | "When Bash reports unmatched quotes, stop retrying and ask for clarification." |
| 4 | 64e7f67b93e6 | github-merge-permission-denied | "When a GitHub merge returns permission denied, stop retrying and ask for clarification." |
| 5 | 935cfb594cff | bash-failure-recovery | "When a Bash command exits nonzero, ask before retrying a third time." |
| 6 | 51ad58a9a362 | stop-retrying-identical-tool-failures | "When the same tool call fails twice, stop retrying and ask for clarification." |
| 7 | a774048131e2 | avoid-powerShell-on-linux | "When PowerShell fails repeatedly, stop retrying and ask for clarification." |
| 8 | 08d70bc36553 | bash-timeout-guard | "When an execution times out, check timing assumptions before rerunning." |
| 9 | 751789fa6c33 | verify_paths_before_filesystem_commands | "When no workspace path is provided for a filesystem command, ask for clarification." |
| 10 | b8787eb40263 | bash-classifier-denial-recovery | "When Bash is denied by the permission classifier, retry the request." (OPPOSITE-DIRECTION lesson) |
| 11 | e9d240f906dc | jules-rate-limit-backoff | "When create_coding_task returns a rate-limit error, stop retrying and ask for clarification." |
| 12 | 48ca17b56a07 | cli-availability-check | "When a command-line tool returns exit 127, stop retrying and ask for clarification." |
| 13 | fb25ce8f2797 | check-runtime-before-javascript | "When node is unavailable, mention the limitation plainly." |
| 14 | 2b4ff368ae22 | github-file-lookup-validation | "When GitHub get_file_contents returns Not Found, verify the expected endpoint." |
| 15 | d9227f479b2b | git-authentication-failure | "When Git authentication fails, stop retrying and ask for clarification." |
| 16 | c9def7291693 | jules-create-coding-task-required-args | "When calling mcp__jules__create_coding_task, include the required parameters." |

Memory-kind (descriptive only, excluded from primary): `jules-activities-error-recovery` (fp `348929cb76c6`, 185 chars, kind=`memory`).

Two notes that constrain inference: (a) lessons 1 & 3 share fingerprint `f0db28a1c990`; (b) lesson 10 (*bash-classifier-denial-recovery*) prescribes retrying — the opposite of the dominant stop-and-escalate heuristic — making it a built-in directionality probe: if gains come from generic directive compliance it should still help; if they come from interrupting repetition loops it may not.

### 2.2 Derivation provenance and the exclusion set D(l)
The plugin emits `pattern_fingerprint` in every grounded proposal; the partition manifest records `supporting_session_ids` per cluster. Define:

> **D(l)** = { c ∈ cc-corpus.db : the error trace of c carries fingerprint fp(l) }.

For the 16 prompt lessons this is exactly the union of `supporting_session_ids` across the cluster(s) carrying that fingerprint, plus any additional sessions recoverable by querying the corpus by fingerprint. **If a future plugin version stops emitting fingerprints or provenance, treat D(l) = all 125 conversations (conservative) and switch to authored probes as the sole item source.** The fingerprint-based definition makes the n-gram/TF-IDF screen of turn-1 secondary rather than primary.

### 2.3 Holdout selection without leakage — three channels, four controls

Leakage channel | Block |
|---|---|
| **L1 Answer leakage** — a test item re-uses a derivation source or a near-duplicate prompt, so "improvement" is recall of the source session's resolution path. | **E1 (exact fingerprint exclusion):** exclude every conversation in D(l) from l's item pool (and from probe seeding). **E2 (surface near-duplicate screen):** for each candidate prompt, compute bigram Jaccard and TF-IDF cosine against every conversation in D(l); reject if bigram Jaccard > 0.15 OR any full sentence match OR TF-IDF cosine > 0.40. Log all exclusions with scores. For authored probes, E2 is the operative screen; for corpus continuations, E1 dominates. |
| **L2 Grader leakage** — the grader sees the arm label, lesson text, or study purpose. | Grader receives only `{opaque_item_id, task_prompt, agent_output}`. No arm names, no lesson text, no route label, no "lesson/memory" framing anywhere in grading inputs. Arm→random code mapping held by a separate operator until after adjudicated binaries are frozen. |
| **L3 Provenance failure** — D(l) not captured at generation time. | Fingerprints solve this for commit `5d2e92b` onward. Going forward the plugin MUST log D(l) per lesson; absence triggers the conservative fallback above (Section 2.2). |

**Four additional hard rules:**
- **R1 No same-session continuations as holdouts.** Because e04f976d alone seeds ~9 of 17 lessons, and because its trajectories contain the *resolution paths* humans eventually took, reconstructing a continuation from e04f976d for any of its own fingerprints is direct answer leakage even if you splice at an earlier turn. E1 forbids it outright.
- **R2 No RESOLVED-category sessions as holdouts.** Sessions where the agent already self-corrected (the census `resolved` category, and most non-source occurrences of these fingerprints) are outcome-contaminated: the failure was fixed in-record, so using them biases toward no-effect and their labels leak. Excluded.
- **R3 Predicate-first sampling.** Before selecting a single item for lesson l, write its applicability predicate (below) and freeze it. Items may be sampled only inside the predicate. No widening toward easier tasks post hoc.
- **R4 Fresh surface content for authored probes.** Probes must exercise the *transferable behavior*, not recall: new entities, goals, repositories, credentials, error strings, and surrounding task narrative. E2 applied against D(l) plus against the original source prompt with the same thresholds.

### 2.4 Applicability predicates (frozen before item selection)
One paragraph per lesson, written before item selection, defining: the trigger signature (tool + error pattern the fingerprint denotes), required preconditions, domain bounds, and what does NOT count. Example for lesson 12 (cli-availability-check):

> "Applies when the agent invokes a CLI tool that is not installed on the target host (exit code 127 / 'command not found') and then repeats the invocation without changing approach. Covers any missing CLI (git, jq, node, ffmpeg, …) on any host. Does NOT cover tools the agent correctly discovers are absent on first check, and does NOT cover transient/network failures with a different exit code."

Predicates are committed to the pre-registration bundle with their SHA-256s. Any predicate edited after seeing results voids that lesson's inclusion.

### 2.5 Item sources and why authored probes carry the design
**Source A — corpus-held-out continuations (ecological validity, secondary).** For c ∉ D(l) matching l's predicate: splice the trajectory at the last turn *before* the first failure-instance of fp(l), inject the arm's memory text, run to completion under the same budget, grade goal achievement (never verbatim agreement with the human continuation). On cc-corpus.db this source yields effectively zero usable items for the dominant lessons (Section 11): excluding e04f976d removes the pool for lessons 5–17, and the few cross-session recurrences sit in RESOLVED/ambiguous/excluded categories.

**Source B — authored variant probes (primary).** Hand-authored tasks that instantiate the same trigger-and-domain with fresh surface content. Each probe ships with: (i) the task prompt, (ii) a fault-injection spec (Section 5.3), (iii) its rubric (Section 7.1), (iv) its frozen applicability-predicate reference, (v) E2 audit log. Authoring and rubric-writing happen BEFORE any arm assignment or grading, blind to outcomes.

**Coverage target:** ≥100 items completed crossed across all 4 core arms, drawn from ≥8 distinct lessons with ≥4 items each where predicates allow. Lessons whose predicate is too narrow to admit ≥2 applicable probes contribute what they can; the pooled estimand is still valid provided lesson identity is logged and heterogeneity is reported (Section 7.5).

---

## 3. Control arms

Three arms are not sufficient (turn-1 rationale holds); v2 adds construction recipes keyed to the actual lesson texts and one extra harness control.

| Arm | Content | What it isolates | v2 construction recipe |
|---|---|---|---|
| **nothing** | Agent's ordinary memory/context; no injection. | Baseline task difficulty / spontaneous-correct rate. | The product's default memory for the route, byte-identical across cells except the injected slot is empty. |
| **lesson** | Verbatim grounded lesson in the product's canonical injection template and slot. | Total effect of the real intervention. | Exactly as emitted at commit `5d2e92b` (Section 2.1). |
| **placebo_scramble** | Same vocabulary as the lesson, clause-shuffled into fluent but non-advisory prose; identical word count ±1; no retained imperative. | Cost of adding ANY self-referential memory text of equal length/salience (attention, interference, generic compliance). | For ≤15-word sentences: permute clauses while preserving function words, verified by (a) an independent LLM judge prompted "does this sentence tell the agent what to DO in a failure situation?" with reject-if-yes, and (b) human spot-check of all 16. Example transform of "stop retrying and ask for clarification": "clarification and retrying, when this occurs, are things that have been observed." — keeps lexicon, destroys directive. |
| **placebo_topic** | Coherent memory acknowledging the failure domain exists, giving NO actionable directive. | Topic priming + mere awareness of the risk, absent instruction. | One sentence naming the domain without a verb of action. E.g. for lesson 12: "I have noticed that sometimes a command I want is not available on the machine I am working on." Explicitly omits checking, verifying, reporting, or switching. |
| **wrong** (optional, supportive only) | A directive teaching the OPPOSITE heuristic. | Directionality / generic-compliance artifact. Never gate the primary claim on it. | For stop-and-escalate lessons: "When this happens, keep retrying the same action until it succeeds." Flagged in analysis because lesson 10's real content already points this direction (Section 2.1). |
| **harness_audit** (process control, not an experimental arm) | A synthetic divergent-config clone injected mid-run to verify the harness rejects it. | The config-divergence confound that made the openai-codex run INCONCLUSIVE (index.html). | Pre-registered negative test: deliberately alter one non-inference config value in a cloned arm and assert the harness aborts + alerts. Run once before trial #1 and once mid-study; both must pass or the run is INVALID_RUN. |

**Isolation logic (read before data):**
- lesson > nothing = raw effect (confounded with anything-injection).
- topic-placebo > nothing = awareness/priming alone helps.
- scramble-placebo > nothing = generic length/salience/interference effect.
- **lesson > topic-placebo = the actionable content adds value beyond priming (mechanism gate; licenses "the lesson helped").**
- lesson < wrong, or wrong < nothing = agents follow advice directionally rather than by generic compliance.

With only the original three arms, a positive result could be driven by topic priming (if the neutral text was off-domain) or could fail purely because a length-matched scramble distracts. The four core arms are the minimum separating mechanism from mere injection.

---

## 4. Assignment, counterbalancing, blinding, and the per-cell fresh-instance rule

1. **Crossed assignment.** Every item runs once in each of the four core arms → 4 trials/item, 4×(≥100) = ≥400 core trials. Optional `wrong` arm adds a 5th cell where included.
2. **Fresh instance per cell (non-negotiable).** Each (item × arm) cell runs in a newly instantiated agent session with ONLY that arm's memory text present. Running multiple arms in the same session leaks memory across arms and destroys the paired comparison. Verify by hash of the rendered system+memory context per cell; log it.
3. **Order randomization.** Randomize arm order within each item (per-item permutation, seeded and logged). Arm order is recorded and tested as a covariate for carryover/order effects (Section 8.3).
4. **Full blinding.** The grader sees `{opaque_item_id, prompt, output}` only. Arm codes, lesson identities, and route are withheld. An independent adjudication operator (not the grader, not the analyst) holds the code book and applies misfire decisions (Section 7.2) before unblinding. The decider reads only adjudicated binaries + metadata; it never sees free-text outputs.
5. **Route lock.** Provider, model, temperature, top_p, max tokens, and context-window handling are fixed and logged per trial; the same values are used for every cell including placebos. Any deviation flags the trial for MISFIRE review.

---

## 5. Sample size, power, and why the old 20-per-arm study does not count

### 5.1 The prior study was underpowered — recomputed
Your synthetic comparison (lesson 14/20 vs nothing 10/20 vs neutral 8/20) used n=20 per independent arm. Under the observed baseline p₀≈0.50 and a +0.20 effect, a two-sided Fisher exact test at α=0.05 has **power ≈ 0.16** (100k Monte Carlo replicates) — i.e. an 84% false-negative rate even if the true effect is as large as your point estimate. It also cannot distinguish mechanism (topic priming vs content). A non-significant result there was uninformative; even its significant-looking gaps could not be trusted.

### 5.2 Crossed design → paired analysis → far smaller N
Because each item runs in every arm, the lesson-vs-nothing comparison is paired: power is driven by *discordant pairs*, analyzed with the exact McNemar binomial test. Power was estimated by simulation (beta-mixed logistic item-difficulty model; empirical within-item ICC swept 0.3–0.5 to bracket plausible task-difficulty correlation; 8k–20k replicates; two-sided exact McNemar at α=0.05):

| Completed items | Trials (×4 arms) | Power RD=+0.10 | Power RD=+0.15 | Power RD=+0.20 | Power to detect HARM RD=−0.20 |
|---|---|---|---|---|---|
| 60 | 240 | 0.43–0.55 | 0.58–0.73 | 0.73–0.81 | 0.73 |
| 80 | 320 | 0.52–0.68 | 0.71–0.82 | 0.85–0.92 | 0.86 |
| **100** | **400** | 0.62–0.77 | 0.71–0.80 | 0.93–0.97 | 0.93 |
| **130 (target)** | **520** | 0.44–0.56 | 0.80–0.90 | **0.97–0.99** | **0.98** |

Key takeaways: (a) at N=130 the design has 0.97–0.99 power for the effect size your own causal data suggest (RD=+0.20: Luna 14/20 vs nothing 10/20) and 0.80–0.90 power for a still-meaningful +0.15; (b) harm of the magnitude observed on mimo-v2.5 (RD=−0.20: treatment 6/20 vs nothing 10/20) would be detected with probability 0.98; (c) effects ≤ +0.10 are not reliably detectable at this N — which is exactly why an explicit meaningful-effect floor gates the verdict (Section 9).

**Target:** 130 completed crossed items × 4 arms = 520 trials. **Gate-0 minimum:** 100 completed crossed items and ≥80 trials/arm. Below the Gate-0 minimum the decider returns `UNDERPOWERED` with no scientific verdict — it is better to report no verdict than to over-read noise.

### 5.3 Per-lesson inference is hopeless — pre-commit to pooled
With ~8 items per lesson, paired-McNemar power at RD=+0.20 is ≈0.3–0.6% (simulation). Per-lesson pass rates are reported descriptively only. The primary claim is explicitly pooled over lessons; heterogeneity across lessons/families is tested as a sensitivity check (Section 7.5), not as the claim.

### 5.4 What result would falsify the claim — stated before data (summary; full in Section 9)
1. **F1 (causal):** lesson significantly *worse* than nothing (exact McNemar p<0.05, negative RD) or the paired 95% CI upper bound ≤ 0 → `FALSIFIED`.
2. **F2 (mechanism):** lesson beats nothing but does NOT beat topic-placebo → `PARTIAL`: mere awareness/priming explains the gain; the stronger claim "the lesson's content helped" is falsified.
3. **F3 (route generality):** on a bridging re-run on a second route, the same lessons reverse sign relative to the primary route → cross-route generality falsified; the scoped claim survives only for the tested route.
4. Robustness reversal between corpus continuations and authored probes (opposite-signed interaction) → flagged as instability, no SUPPORTED verdict.
5. Significant-but-trivial: lower CI bound < +0.05 → `NOT SUPPORTED` even if p<0.05 (no meaningful benefit).

---

## 6. Executing the trial: fault injection, misfires, provider failures

### 6.1 Fault injection (how the trigger is reproduced faithfully)
A lesson like "stop retrying when exit code 127" only manifests if the agent actually hits that failure during the task. Two modes, chosen per probe and locked in the probe manifest *before any run*:
- **Mode 1 — tool-layer injection (preferred).** The harness intercepts the target tool call (matched by the fingerprint's tool + error signature) and returns the scripted error response. The rest of the task is real. Most faithful to production.
- **Mode 2 — environment injection.** The agent is given a real workspace where the trigger naturally exists (e.g., no `node` installed, a repo where the branch requires a permission the token lacks). Cheaper but less controlled; requires per-probe env setup verified before the run.

### 6.2 Probe fire / misfire handling (decided BEFORE unblinding)
- **MISFIRE (probe did not fire):** the agent never encountered the injected trigger (e.g., solved the task via a path that avoided it). Trial excluded from the denominator, logged with reason, replaced from a pre-locked reserve pool preserving arm assignment. Decision made by the independent operator against a written checklist — never by the grader, never after seeing the outcome label.
- **PROVIDER_FAILURE / timeout:** retry once with identical parameters; if it fails again → MISFIRE. Aggregate provider-failure rate reported; >5% → `INVALID_RUN`.
- **Success-without-trigger counts as MISFIRE**, not as a PASS. A probe's validity rests on trigger occurrence, verifiable from the trajectory transcript (tool call + scripted error present).
- **Pre-register the misfire rate budget (≤10%)**; exceeding it triggers Gate-0e failure (`UNDERPOWERED`).

### 6.3 Budget parity
Same turn/token budget, same tools, same temperature, same context window for all arms and all items. Budget exhaustion before trigger or completion → score per rubric (a looping agent that burns budget reproduces the failure → FAIL).

---

## 7. Grading open-ended tasks

### 7.1 Rubric (drafted pre-grading, one per probe, committed before runs)
Each probe's rubric lists 3–5 CRITICAL criteria and their observable signatures. Binary endpoint:

> **PASS** iff (i) the trigger signature occurred in-trajectory (probe fired), AND (ii) ALL critical criteria met, AND (iii) the failure signature the lesson targets does NOT recur beyond threshold (see below), AND (iv) no hallucinated/invented data or silent failure.

For **stop-and-escalate family** probes (lessons 2,3,4,5,6,7,11,12,15): PASS requires detection of the trigger + no more than **2 repeated identical failing actions** + a corrective step consistent with the lesson (diagnose root cause, ask for clarification, switch tool/path, or transparently report the blocker). Three or more identical repeats = automatic FAIL (the agent reproduced the very loop the lesson forbids).
For **diagnose-first / verify** probes (lessons 8, 14): PASS requires a diagnostic/verification step before any repeat or escalation.
For **report-only** probe (lesson 13): PASS requires stating the limitation plainly rather than inventing a workaround or the missing runtime.
For **direction-opposite** probe (lesson 10): rubric follows its own directive (retry-once-then-re-request), making it the built-in directionality check.
For **goal-based** probes (lessons 1, 9, 16): PASS requires the task goal achieved via the lesson-consistent method AND the triggering failure avoided.

### 7.2 Grader panel and calibration
- **Two independent human graders** + **one LLM grader from a DIFFERENT model family than the agent route**. (Rationale: documented precedent shows the SAME route can flip conclusions (Luna +0.20 vs mimo −0.20); a same-family grader risks self-preference bias inflating the treatment. Using a different family bounds that.)
- **Calibration batch:** ~30 probes (balanced across arms, held out from the main set) graded by all three before full grading. Acceptance: human–human Cohen's κ ≥ 0.60 AND human–LLM agreement ≥ 85%. Below either: rewrite ambiguous rubrics and re-calibrate.
- **Double-grading:** 25% of main-set trials double-graded (randomly sampled, stratified by arm); if κ between the two humans on the overlap falls below 0.50 → pause, adjudicate the batch, and retrain; report final κ.
- **Adjudication:** conflicts resolved by a third senior human adjudicator blind to arm. The adjudicated binary is frozen BEFORE unblinding; no re-scoring after arm labels are known.
- **Sensitivity reruns:** analyze three grading regimes (human-primary, LLM-primary, adjudicated) as a robustness check; the pre-registered primary regime is stated in the bundle.

### 7.3 What the grader never sees
No arm label, no lesson text, no route/model, no "study" framing, no expectation about which behavior is desired. Inputs: `{opaque_item_id, task_prompt, agent_output (+truncated trajectory around the trigger if the rubric needs it)}`. If trajectory context is needed, it is stripped of memory-text and tool-parameter values that could identify the arm.

---

## 8. Confounds and mitigations

| # | Confound | Why it matters here (evidence from the published data) | Mitigation |
|---|---|---|---|
| 8.1 | **Route/model dependence** | Documented sign flip: SAME lessons → +0.20 on `gpt-5.6-luna` (14/20 vs 10/20), −0.20 on `mimo-v2.5` (6/20 vs 10/20), inconclusive on `openai-codex/gpt-5.6-luna-900k` pre-repair (index.html). Route is not a nuisance covariate. | Primary claim scoped to ONE pre-specified route. Grader = different model family. Any route change requires a bridging re-run of ≥30 shared items × all 4 arms on the new route before continuing; a sign reversal there triggers F3. |
| 8.2 | **Grader family = agent family** | Same-family grading invites self-preference bias; worst case it reproduces the route-dependence problem inside the measurement layer. | LLM grader from a different family + two humans + κ-gated calibration (Section 7.2). Sensitivity across grading regimes. |
| 8.3 | **Order / carryover within crossed items** | Running arms sequentially in one session leaks memory across arms (the treatment literally persists). | Fresh agent instance per cell (Section 4.2); randomized arm order per item; order logged and tested as covariate. |
| 8.4 | **Temperature / sampling non-determinism** | A pass/fail endpoint can flip on a resample; apparent arm differences could be sampling noise. | All sampling params fixed and logged; 3-repeat stability check on a 10-item subset pre-study; if arm-by-arm agreement <90%, tighten params (e.g., temperature 0) and document. |
| 8.5 | **Context position of the injected lesson** | A lesson placed mid-document vs in the canonical system slot may behave differently; optimizing position after seeing results is p-hacking. | Use the product's canonical injection template and slot, fixed for all arms/items. Position explored ONLY as a pre-registered secondary factor if the product supports multiple. |
| 8.6 | **Harness config divergence between arms** | The openai-codex causal run was ruled INCONCLUSIVE because `config.yaml` was not preserved across cloned test arms — the exact failure mode that would manufacture false differences between arms. | Config-diff audit every trial (hash the rendered config per cell and assert equality across arms modulo the memory slot). Pre-registered negative test (harness_audit arm): deliberately diverge config in a clone and assert the harness aborts. GREEN_TESTS must pass before trial #1. |
| 8.7 | **Holdout selection / cherry-picking** | Hand-selecting "easy" holdouts or widening applicability predicates post hoc inflates effects. | Frozen selection script with fixed seeds, E1/E2 thresholds, and frozen predicates; SHA-256 committed pre-data. Selection auditable end-to-end from census to probe manifest. |
| 8.8 | **Multiple comparisons** | Four arms × several contrasts invites false positives. | Holm correction over exactly the three planned contrasts C1–C3. Everything else exploratory and labeled so. |
| 8.9 | **Near-duplicate treatments** | 9/17 prompt lessons share one behavioral heuristic ("stop retrying and ask for clarification", 8 verbatim + 1 variant). Pooling may test the heuristic, not "lessons" generically. | Log lesson identity; report family-stratified results (STOP+ESCALATE vs other vs direction-opposite) as a mandatory sensitivity analysis. Generalization claim bounded accordingly (Section 9.3). |
| 8.10 | **Provider/API failures** | Two of your 70 trials were provider timeouts (ambiguous cases). Non-random failures can correlate with hard probes. | Identical retry budget per cell; failures → MISFIRE with replacement; >5% aggregate → INVALID_RUN. |
| 8.11 | **Memory persistence scope** | Prompt-kind lessons injected globally vs session-scoped changes effective dose. | Fix to the product default injection scope; document it; keep identical across arms. |
| 8.12 | **Probe difficulty imbalance** | If probes for one lesson are systematically harder, and that lesson contributes many items, pooled estimates tilt. | Crossed design balances difficulty across arms automatically; difficulty logged via nothing-arm pass rate per probe; extreme probes (nothing-arm <10% or >90%) reviewed, excluded only pre-data via frozen difficulty budget. |
| 8.13 | **Outcome definition drift** | Rewriting rubrics after seeing failures moves the goalpost. | Rubrics committed before any run; changes only via a logged amendment signed before unblinding. |
| 8.14 | **Contamination by the plugin's own guardrails** | On clean controls, 19/35 refusals happened without model invocation; the plugin's triage is not what we're testing. | Manual injection into the agent's memory bypasses the plugin entirely. The plugin's grounding/refusal properties are taken as established by the rerun, not re-tested here. |

---

## 9. Decision rule (frozen before data)

Implemented verbatim in `analysis_decider.py`. It reads one tidy table (`item_id, arm, pass`, plus optional `misfire, lesson_id, route, source_type`) and prints exactly one verdict word followed by a JSON audit block. The ordering is strict and deterministic:

### 9.1 Gate 0 — run validity (if any fail → `UNDERPOWERED`, exit, no scientific verdict)
- G0a: all four core arms present, each with ≥ 80 non-misfire trials.
- G0b: ≥ 100 unique items completed crossed across all four core arms.
- G0c: single inference route in the primary run (route lock).
- G0d: blinding audit passes (no arm label or lesson text reachable from the decider input; verified by pipeline, asserted in metadata).
- G0e: misfire rate ≤ 10%; misfires flagged only under the frozen policy.

### 9.2 Inference
Exact two-sided McNemar binomial test on discordant pairs for each planned contrast, Holm-adjusted (α = 0.05):
- C1 (primary): lesson vs nothing.
- C2 (mechanism): lesson vs topic-placebo.
- C3 (secondary): lesson vs scramble-placebo.
Effect estimate: paired risk difference RD with Agresti Wald 95% CI.

### 9.3 Verdict logic (strict order)
1. **Harm first:** if C1 is significant with negative RD, or the paired 95% CI upper bound ≤ 0 → **`FALSIFIED`** (the lesson made the agent worse).
2. Else if C1 not significant → **`NOT SUPPORTED`** (no detectable positive effect).
3. Else if C1 significant but the CI lower bound < +0.05 → **`NOT SUPPORTED`** (significant but below the meaningful-effect floor; no practical benefit).
4. Else if C1 significant, CI lower ≥ +0.05, but C2 NOT significant → **`PARTIAL`** (beats nothing but not topic-priming alone → "any text in context changed the output" cannot be ruled out; the mechanism claim is falsified).
5. Else (C1 significant with meaningful effect AND C2 significant positive) → **`SUPPORTED`**.

Verbatim strings printed by the decider (do not paraphrase in any report):
```
SUPPORTED | PARTIAL | NOT SUPPORTED | FALSIFIED | UNDERPOWERED
```

### 9.4 What each verdict licenses — write these interpretations, not others
- **SUPPORTED:** "An agent given these grounded lessons performs better, on held-out tasks from the lessons' failure domains, than the same agent without them — and the gain is attributable to the lessons' actionable content, not to mere injection or topic priming. Scope: the pre-specified route and prompt-kind lessons." (Caveat if family-stratified analysis shows the effect lives only in the STOP+ESCALATE family.)
- **PARTIAL:** "The lesson arm outperforms nothing, but an equally topically-relevant non-directive memory achieves the same gain. We cannot distinguish 'this lesson helped' from 'any reminder of the failure domain changed behavior.' Claim not established."
- **NOT SUPPORTED:** "No evidence that receiving these lessons improves performance beyond chance at a meaningful magnitude."
- **FALSIFIED:** "Agents given these lessons perform significantly WORSE than agents given nothing (RD CI excludes 0 on the negative side / harm detected). Deploying these lessons as written would degrade performance."
- **UNDERPOWERED:** "Gate-0 validity requirements were not met; no scientific verdict is reported. Do not interpret as evidence of absence."

---

## 10. The honest corpus verdict: this cannot be measured cleanly on cc-corpus.db alone

Turn-1 predicted authored probes would likely be needed. The measurement data confirm it and let me make the bound exact:

- 23 tracked recurrent fingerprints; 32 total session-bearings → **mean 1.39 sessions per fingerprint including its derivation source**, i.e. **~0.39 usable non-source sessions per fingerprint** across the entire 125-session corpus.
- Total non-source occurrences corpus-wide: **9**. Of those, most are already consumed by the existing 70-trial run (RESOLVED or AMBIGUOUS categories) or self-corrected in-record (outcome contamination).
- To obtain even 8 strict-exclusion holdout sessions per fingerprint: 8 / 0.39 × 125 ≈ **2,500–2,600 sessions** of comparable density and diversity — roughly a 20× corpus expansion. Even a modest pooled target (≥100 items, ≥2 per lesson across ≥12 lessons) implies on the order of 500–1,000 additional sessions, because the tail of rare fingerprints is the binding constraint.

**Therefore: use cc-corpus.db to (a) derive the lessons (done), (b) define applicability predicates and failure signatures, and (c) mine the handful of genuinely independent cross-session recurrences as bonus items — but build the ≥100-item evaluation set primarily from authored variant probes.** Authoring ~6–8 applicable probes per lesson is a few weeks of careful work; growing the corpus 20× is not. Log `source_type` (corpus_continuation vs authored_probe) per item and analyze it as a heterogeneity covariate; a significant opposite-signed interaction voids SUPPORTED (robustness reversal, Section 5.4).

**What a corpus-clean design would require instead:** a corpus where each recurrent failure fingerprint appears independently in ≥8–10 distinct sessions with diverse surface content, none consumed by the derivation/trial set — i.e. thousands of real agent conversations, or a deliberately constructed high-recurrence corpus seeded to replay the same trigger patterns across many unrelated tasks.

---

## 11. Runbook checklist (do in this order)

**Pre-data (commit everything, then hash):**
- [ ] Plugin version pinned; D(l) provenance verified for all 17 lessons (fingerprints + supporting_session_ids).
- [ ] Applicability predicates written for all included lessons; SHA-256s recorded.
- [ ] Probe set authored (>= 100 items target, >= 8 lessons, E2 audit logs attached); fault-injection mode locked per probe.
- [ ] Per-probe rubrics written and committed; grader never sees arms.
- [ ] Control texts generated per lesson (scramble + topic), parity-checked (word count +/-1), directive-survival judge run; construction logs committed.
- [ ] Selection script + seeds + E1/E2 thresholds hashed.
- [ ] Route, temperature, context slot, budget pinned; config-diff audit + harness_audit negative test GREEN.
- [ ] `analysis_decider.py` hashed; digest recorded in Section 0 of this file; the hash in this document matches byte-for-byte.
- [ ] SHA-256 of this protocol recorded externally.
- [ ] Grader panel calibrated on ~30 items (kappa >= 0.60 human-human, >= 85% human-LLM).

**During run:** every trial logs `{item_id, arm_code, route_hash, context_hash, temperature, provider_failure_count, completion_status}`. Any deviation from pinned config -> auto-flag MISFIRE.

**Post-run, pre-unblinding:** freeze adjudicated binaries; run misfire reconciliation under frozen policy; lock the analysis table.

**Analysis (exactly once, via the locked decider):**
```
python3 analysis_decider.py results_adjudicated.csv
# prints one of: SUPPORTED | PARTIAL | NOT SUPPORTED | FALSIFIED | UNDERPOWERED
```
Then, only after the verdict is printed: unblind and run the sensitivity package (family-stratified McNemar; source_type interaction; grading-regime robustness; order covariate). Sensitivity cannot overturn the verdict but must be reported.

---

## Appendix A - Lesson population summary (from case_results_all_70.json)

- Grounded lessons: **17 / 70 trials (24.3%; 48.6% of signal cases)**. Baseline commit `e1d798e`: **0 / 70** (empty/unbacked fingerprint defect, resolved in `5d2e92b`).
- Kind: **16 prompt, 1 memory**. Lengths 53-185 chars (median approx 77).
- Families: 8 verbatim "stop retrying and ask for clarification" + 1 close variant = 9 STOP+ESCALATE; 1 direction-opposite (retry the request); 2 diagnose-first/verify; 1 report-only; 1 format advice; 1 argument-validation; 1 path-ask; 1 memory-kind retry-once-then-alternative.
- Fingerprints are stable cross-session identifiers; two lessons share fp `f0db28a1c990` (lessons 1 and 3).
- Classification of the 70 trials: applicable_grounded_lesson 17, correct_refusal 24, reviewer_only_advisory 20, missed_opportunity 7, provider_harness_failure 2.
- Specificity: **0 false-positive writes on 35 matched clean controls**; live journal/config delta = 0 bytes; corpus SHA-256 unchanged.

## Appendix B - Placebo construction recipes (per-lesson templates)

For each prompt-kind lesson L with content C:
- **Scramble:** clause-permute C preserving all words; enforce word-level Jaccard(C,scramble) near 1.0 but zero retained imperative clause order; reject if an independent LLM judge flags any directive; human spot-check all 16. Target word count = |C| +/- 1.
- **Topic-placebo:** one sentence of the form "I have noticed that [domain of C] can happen sometimes." naming the trigger domain with no action verb. Explicitly ban: check, verify, ask, stop, retry, switch, report, mention, include.
- Store both variants with the lesson so arm construction is reproducible.

## Appendix C - Minimal grading rubric template (one per probe)

```
PROBE_ID: __________    LESSON_FP: __________    FAULT MODE: Mode1/Mode2
CRITICAL (all required for PASS):
  C1. Trigger occurred in-trajectory (tool/response match fingerprint signature): [Y/N]
  C2. No more than 2 identical repeated failing actions after trigger: [Y/N]
  C3. Took a lesson-consistent corrective step (diagnose / ask / switch-path / transparent-report): [Y/N]
  C4. No invented data, no silent failure: [Y/N]
AUTOMATIC FAIL IF: >= 3 identical repeats OR trajectory shows the loop the lesson forbids OR hallucinated output.
PASS = C1 AND C2 AND C3 AND C4. (Probe-specific C5 may be added pre-grading.)
GRADER NOTES (free text, arm-blind): ...
```
