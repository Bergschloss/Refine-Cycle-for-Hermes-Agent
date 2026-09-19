# Rollback

Split out of the README.

## Rollback

A successful mutation returns a rollback command only when its journal record is
actually reversible:

```
/refine rollback <journal_id>
```

Create rollback deletes a skill only if current content still exactly matches
the refine proposal. Patch rollback refuses to overwrite a later change before
restoring its pre-edit content. Memory rollback removes only the exact appended
entry and preserves unrelated later entries. Prompt-note rollback removes only
its exact unchanged note and preserves later notes; a changed or missing note is
a conflict and is left untouched.

### Where the restored content comes from

A skill patch records its pre-edit content twice: as a `snapshot` inside the
journal record, and as a `.bak` file under `journal_dir/backups`. Both come from
one host read, so they cannot disagree. Rollback prefers the snapshot, so losing
the backup file no longer costs the rollback.

A snapshot holds the pre-edit text as it was. This needed two layers of care
while the plugin still redacted credentials from everything it wrote, including
snapshots: the proposal path refused to patch a skill whose body the filter would
change, and a digest beside the snapshot caught a snapshot that no longer matched
the real content. The filter is gone, so the first layer went with it and a
credential in a skill body no longer costs the patch or the snapshot.

The digest stays, because it answers a different question: a SHA-256 of the real
pre-edit content stored beside the snapshot. If the stored text no longer matches
it, the snapshot is refused and the raw `.bak` file is used instead, so tampered
or truncated text is never written over a skill.

`is_reversible` asks the restore path the same question rollback does, so an
entry is never advertised as reversible when neither source survives. In that
case rollback refuses with an explicit error and changes nothing, and a staged
rollback whose state cannot be proven stays `pending_rollback` rather than being
declared rejected.

Records written before snapshots existed carry only `backup_path` and keep
rolling back from it unchanged.

### Rolling back a transaction

Each edit of a multi-edit transaction owns its own journal record and its own
rollback ID; there is no transaction-level undo. Recovery IDs are listed
**newest first**, which is the order to follow: memory recovery is positional, so
undoing an earlier append before a later one shifts the later entry and its
rollback fails closed as a conflict.

If mutation succeeded but journal finalization failed, the returned recovery ID
points to the durable `prepared` record. Pending forward approvals consume budget
but are not advertised as reversible until the target exactly matches the
proposal. Rollback intent is journaled before its side effect; a staged rollback
returns a pending ID and is not called rolled back until the target change is
confirmed. A rejected rollback returns the entry to `applied`, so it can be
retried.

---

