# Replacement Path Architecture and Regression Fix

## 1. Intact Pairing in the Crossed-520 Run

Audit of all 133 directories on disk (`cells/*`) establishes that **pairing was 100% intact throughout the crossed-520 run**:
* Exactly **0 hybrid pairs** exist.
* When a cell misfired, the harness called `run_item(out, r_item, route, design, seed, 'reserve', ...)` on the reserve item.
* `run_item()` always executes all four arms (`nothing`, `lesson`, `placebo_scramble`, `placebo_topic`).
* The three reserve items (`probe_08d70bc36553_10`, `_11`, `_12`) and the three partially misfired items (`probe_751789fa6c33_01`, `_06`, and `probe_f0db28a1c990_06`) were all executed as complete 4-arm sets in their own distinct directories.

## 2. Defects in the Previous Implementation

Two structural flaws existed in `run_crossed.py`:
1. **Per-cell replacement loop**: The replacement loop iterated over misfired cells:
   ```python
   for row in [r for r in rows if r['harness_misfire']]:
       r_item = reserve_queue.pop(0)
       r_rows = run_item(out, r_item, ...)
   ```
   If multiple arms in the same item had misfired, multiple reserve items would have been drawn for a single failed item.
2. **Inclusion of replaced items in build_csv_rows**: `build_csv_rows()` globbed all `cell.json` files on disk without consulting `replacements.jsonl`. Consequently, both the replaced item (with its remaining non-misfired arms) and the replacement item were emitted to `results.csv`, yielding 133 items instead of the 130 crossed items verified by Gate 0b.

## 3. Code Modifications Applied to run_crossed.py

1. **Item-level replacement trigger**:
   ```python
   misfired = [r for r in rows if r['harness_misfire']]
   if misfired:
       if not reserve_queue:
           append(replacements_path, {
               'replaced': {
                   'item_id': item['item_id'],
                   'misfired_arms': [r['arm'] for r in misfired],
               },
               'status': 'reserve_pool_exhausted',
               'misfire_reasons': [r['misfire_reasons'] for r in misfired],
           })
       else:
           r_item = reserve_queue.pop(0)
           reserve_used.append(r_item['item_id'])
           append(replacements_path, {
               'replaced': {
                   'item_id': item['item_id'],
                   'misfired_arms': [r['arm'] for r in misfired],
                   'misfire_reasons': [r['misfire_reasons'] for r in misfired],
               },
               'replacement': {'item_id': r_item['item_id']},
               'status': 'drawn',
           })
           r_rows = run_item(out, r_item, route, design, seed, 'reserve',
                             max_char_delta, args.timeout, run_config_digests)
           for r_row in r_rows:
               append(ledger_path, r_row)
               budget.record(r_row)
   ```
2. **Exclusion of replaced items in build_csv_rows**:
   ```python
   replacements_path = run_dir / 'replacements.jsonl'
   replaced_item_ids = set()
   if replacements_path.exists():
       for line in replacements_path.read_text(encoding='utf-8').splitlines():
           if line.strip():
               try:
                   rep = json.loads(line)
                   if rep.get('status') == 'drawn' and 'replaced' in rep:
                       replaced_item_ids.add(rep['replaced']['item_id'])
               except Exception:
                   pass

   for cell_file in sorted(run_dir.glob('cells/*/*/cell.json')):
       cell = json.loads(cell_file.read_text(encoding='utf-8'))
       if cell['item_id'] in replaced_item_ids:
           continue
   ```

## 4. Automated Regression Test

Test created at `tests/test_crossed_replacement.py`.

The test simulates a run directory containing a replaced core item (`item_A` with a misfire in `lesson`) and its replacement item (`item_B` drawn from reserve with 4 completed arms).

* **Execution on previous code**:
  `AssertionError: 'item_A' unexpectedly found in {'item_A', 'item_B'} : Replaced items must not appear in the scored CSV` (FAILED)
* **Execution on updated code**:
  `Ran 1 test in 0.003s — OK` (PASSED)

This fix protects future runs; it does not alter the historical raw artifacts of the crossed-520 run.
