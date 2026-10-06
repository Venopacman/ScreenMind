# Backlog

Parked work from Claude sessions, so a new session can pick it up later.

## Rules

- One file per topic or session: `docs/backlog/<topic>.md`. Don't edit another session's file. Separate files avoid merge conflicts when several sessions write at once.
- Each item has:
  - a short title
  - one or two sentences: what is wrong or missing, and why it matters
  - references: files, functions, commits, DB ids, branches
  - a status: `open`, `blocked` (say on what) or `idea`
- Keep items small. If an item needs a design, link to a plan in `docs/plans/`.
- When an item is done, delete it and mention the commit in the commit message.

## How to add a file

Work from a worktree branch based on `custom`, then fast-forward `custom`:

```bash
git fetch origin
git switch -c docs/backlog-<topic> custom
# write docs/backlog/<topic>.md, commit
git -C /Users/pavel/projects/ScreenMind merge --ff-only docs/backlog-<topic>
git -C /Users/pavel/projects/ScreenMind push origin custom
```

If `custom` moved in the meantime, rebase your branch on it and try again. Never force-push `custom`.
