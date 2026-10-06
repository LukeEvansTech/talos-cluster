#!/usr/bin/env bash
# Warns at session start when the main checkout is not on main or holds staged changes.
#
# Why: on 2026-09-22 a session staged a known-noise entry on a local branch and never committed
# it; the checkout sat on that branch for nine days while memory recorded the entry as landed.
# Work done in worktrees is unaffected: this looks only at the main checkout. Silent when clean.
set -u
[ "${CLAUDE_CODE_REMOTE:-}" = true ] && exit 0
dir=${CLAUDE_PROJECT_DIR:-.}
git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || exit 0
# A linked worktree has its own git dir; only the main checkout shares the common one.
[ "$(git -C "$dir" rev-parse --absolute-git-dir)" = "$(cd "$dir" && cd "$(git rev-parse --git-common-dir)" && pwd -P)" ] || exit 0

branch=$(git -C "$dir" branch --show-current)
staged=$(git -C "$dir" diff --cached --name-only | wc -l | tr -d ' ')
[ "$branch" = main ] && [ "$staged" = 0 ] && exit 0

msg="Checkout state: the main talos-cluster checkout is on '${branch:-detached HEAD}'"
if [ "$staged" != 0 ]; then
    # No age: git keeps no staging timestamp (index mtimes are cached file stats, and 0 after add -p).
    msg="$msg with $staged staged file(s)"
fi
if [ -n "$branch" ] && [ "$branch" != main ]; then
    # A local ref only, so no network at session start; it can lag a push from elsewhere.
    if git -C "$dir" rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null; then
        msg="$msg; origin/$branch exists locally"
    else
        msg="$msg; there is no origin/$branch ref here (not pushed, or not fetched)"
    fi
fi
echo "$msg. Not this session's work unless it says so: find out whose it is before treating it as landed, committing or discarding it, and keep the main checkout on main."
