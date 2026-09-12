# Comparability Verification Chronology

This document records the exact sequence of events during post-run verification, without smoothing or reordering.

## 1. The Verification Abort

Upon completion of the 520 trials, the post-run verification command:
```bash
python3 run_crossed.py verify --run out/crossed-520
```
aborted with exit code 1, emitting the exact output:
```text
HARNESS ABORT: the run's own digests do not satisfy the comparability assertions:
  items sharing domain tool 'mcp__jules__create_coding_task' rendered different scaffolds: ["42529581085e", "c83412705416"]
```

## 2. Why the Assertion Was Stricter Than Pre-Registration

The pre-registration protocol (`lesson_effect_protocol_v2.md`) mandates strict within-item comparability across the four crossed arms: for any given test item, the prompt scaffold and configuration must be identical across `nothing`, `lesson`, `placebo_scramble`, and `placebo_topic` outside the memory injection slot.

In `run_crossed.py`, lines 1387-1394 implemented an additional cross-item check:
```python
by_tool: Dict[str, set] = {}
for entry in per_item:
    by_tool.setdefault(entry['domain_tool'], set()).add(entry['scaffold_slotless_sha256'])
for tool, digests in sorted(by_tool.items()):
    if len(digests) != 1:
        problems.append(
            f'items sharing domain tool {tool!r} rendered different scaffolds: '
            + json.dumps(sorted(d[:12] for d in digests)))
```
This check assumed that every item using the same domain tool name carries an identical tool parameter schema. 

This assumption held for `Bash`, `PowerShell`, `mcp__github__get_file_contents`, `mcp__github__merge_pull_request`, and `mcp__jules__get_activities_since`. However, for `mcp__jules__create_coding_task`, the benchmark contains probes authored for distinct lessons:
* Lessons `51ad58a9a362` and `e9d240f906dc` tested runtime errors and rate limits; their tool schema declared parameters `prompt` and `source`.
* Lesson `c9def7291693` specifically tested argument validation (`jules-create-coding-task-required-args`: *"When calling mcp__jules__create_coding_task, include the required parameters."*). Its tool schema declared only `source` to test missing argument detection.

Because tool parameter declarations are embedded directly into the system prompt scaffold, items testing lesson `c9def7291693` rendered digest `c83412705416`, while items testing `51ad58a9a362` and `e9d240f906dc` rendered digest `42529581085e`. The harness cross-item assertion failed on this difference.

## 3. Independent Per-Item Measurement From Run Artifacts

Inspection of the raw artifacts on disk across all 133 executed items showed:
1. **Within-item scaffold identity**: In 133 out of 133 items, `scaffold_slotless_sha256` is 100% identical across all four arms (`len(slotless) == 1`).
2. **Within-item config identity**: In 133 out of 133 items, `config_sha256` is 100% identical across all four arms.
3. **Run-wide config identity**: Exactly one configuration digest exists across all 532 executed cells:
   `d9cc59e087169c96c53bbaf6dc3211bb7638b5a06c428110e4949033164ff8a7`
4. **Within-lesson scaffold consistency**:
   * All items for `c9def7291693` (`01` through `08`) consistently rendered scaffold digest `c83412705416`.
   * All items for `51ad58a9a362` (`01` through `09`) and `e9d240f906dc` (`01` through `09`) consistently rendered scaffold digest `42529581085e`.

Within-item pairing and cross-arm comparability were completely preserved.

## 4. Harness Grouping Update and Subsequent Pass

The cross-item comparability assertion in `run_crossed.py` was updated to group by `(domain_tool, fingerprint)` instead of `domain_tool` alone, correctly requiring all items testing the same lesson with the same domain tool to agree:
```python
by_tool: Dict[Tuple[str, str], set] = {}
for entry in per_item:
    by_tool.setdefault((entry['domain_tool'], entry.get('fingerprint', '<unknown>')), set()).add(entry['scaffold_slotless_sha256'])
```
Additionally, replaced items recorded in `replacements.jsonl` are excluded from the verification set.

Following this change, `python3 run_crossed.py verify --run out/crossed-520` executed successfully with exit code 0:
```json
{
  "distinct_config_digests": [
    "d9cc59e087169c96c53bbaf6dc3211bb7638b5a06c428110e4949033164ff8a7"
  ],
  "distinct_slotless_scaffold_digests": [
    "240d33b21ad461300e51ee79f78f7b3f4df2348fbdcb5dd950783ad1e9322c3b",
    "42529581085ed470085e52e6919a3dd218c7b677bff53de5978e304304b3b512",
    "c72c9c75a192e5f8576f2d91dcaa3b0047b5439e01070563b0713c6f0a1c981b",
    "c83412705416f8be54ec715b5021d6beb6fac4df442a728dac0afbb0b8c17d1a",
    "ce62032400e4c0d30653bb676bb8f39262f65cb9f30bb6c9c8d256a68273297d",
    "f0f13fc9e361b5efab782f60a31460ae281459b0efcc38436d888d076c8f34bb",
    "fb7e3f16c2dab8629a2d2834ced056dcde50846f78e29d651109c8fd7a9c5640"
  ],
  "items_checked": 130,
  "ok": true
}
```
