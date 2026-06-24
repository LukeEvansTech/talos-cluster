# KB-017: `mise` + lefthook Symlink Race Blocks the First Commit After a Tool Bump

**Status:** Known race; one-line workaround. Hits once per tool-version bump.

## Symptom

The first `git commit` after a mise-pinned tool version changes (e.g. a Renovate bump to
`python`, `gh`, or any `.mise.toml` tool) fails in a pre-commit hook with:

```text
failed to rebuild runtime symlinks ... ln -sf ... File exists (os error 17)
```

(also seen as `Invalid argument (os error 22)`), which kills the hook and the commit.

## Cause

lefthook runs the YAML format hooks (`format-yaml` and `format-yaml-prettier`) **in parallel**.
Both invoke mise, and both try to install the missing tool **concurrently** — mise's runtime
symlink rebuild isn't concurrency-safe, so the loser errors on the `ln -sf`. It triggers
exactly **once per tool bump**; after the tool is installed the hooks are fast.

## Fix

Install the tool **once** (serial), which settles the symlinks, then retry the commit:

```bash
mise install
git commit ...     # retry — now passes
```

In a fresh worktree you may also need `mise trust` before the first commit.

Durable options if it gets annoying: set `parallel: false` for the two YAML hooks in
`.lefthook.toml`, or add a lefthook pre-hook that runs `mise install`.

## References

- mise: <https://mise.jdx.dev/> · lefthook: <https://lefthook.dev/>
