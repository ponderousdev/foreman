# Foreman shepherd — adjudicate review findings

Review bots left unresolved threads on your unit's PR (%%PR_URL%%, branch
`%%BRANCH%%`, unit `#%%UNIT_NUMBER%%`). Adjudicate EVERY thread listed below.
You know the spec; bots don't always — blanket-accepting is prohibited, and
so is blanket-dismissing.

Rules: work only in this worktree on `%%BRANCH%%`; never merge; never push
(foreman pushes); never write to GitHub — your token is read-only, and
foreman posts every reply and resolves every thread from your recorded
dispositions; never edit or delete anyone else's comments; never touch
`.github/workflows/**`.

For each thread, exactly one disposition:

- **Apply** — the finding is right: fix it, commit (Conventional Commit
  referencing `#%%UNIT_NUMBER%%`), and record disposition `applied` with a
  one-line note naming the commit (`applied in <short-sha>`).
- **Decline with reasoning** — the finding is wrong or out of scope: record
  disposition `declined` with brief technical reasoning as the note.
  Deterministic facts beat bot speculation — cite the passing typecheck, the
  spec text, the API docs.

Record every disposition in `%%ADJUDICATION_FILE%%` — foreman posts each
note as the thread's reply, then resolves the thread:

```json
{
  "schema": 1,
  "dispositions": [
    {"thread_id": "<id from the list below>", "disposition": "applied", "note": "applied in abc1234"},
    {"thread_id": "<id>", "disposition": "declined", "note": "why the finding is wrong"}
  ]
}
```

After all threads: if you made commits, run `%%VERIFY_COMMAND%%` until green.
Leave the worktree clean and exit 0. Foreman verifies thread completeness
deterministically afterwards — an undispositioned thread means this run
failed.

## Unresolved threads

%%THREADS%%
