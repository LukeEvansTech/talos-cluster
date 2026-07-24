#!/usr/bin/env bash
# Reject world-visible text that leaks home-network internals or AI attribution.
#
# This repository is PUBLIC. AGENTS.md already forbids committing LAN IPs,
# internal hostnames and topology detail — but that rule reads as "in files",
# so the one surface nobody linted was prose: commit messages and PR bodies.
# GitHub's squash merge copies a PR body verbatim into the commit message on
# main, where it is permanent, which is how a LAN IP and the internal zone name
# ended up in history via #3803.
#
# Usage:
#   check-no-internals.sh [--strip-git-comments] <file>
#   check-no-internals.sh [--strip-git-comments] -        # read stdin
#
# Deliberate bypass: git commit --no-verify

set -euo pipefail

strip_comments=0
if [ "${1:-}" = "--strip-git-comments" ]; then
    strip_comments=1
    shift
fi

src="${1:-}"
if [ -z "$src" ]; then
    echo "usage: $(basename "$0") [--strip-git-comments] <file|->" >&2
    exit 2
fi

if [ "$src" = "-" ]; then
    raw=$(cat)
else
    raw=$(cat "$src")
fi

# In commit-msg mode drop the `#` comment block and everything below the
# `--- >8 ---` scissors line, so a `commit.verbose` diff and the branch hints
# are not mistaken for the author's own prose.
if [ "$strip_comments" -eq 1 ]; then
    text=$(printf '%s\n' "$raw" | awk '/^# *-+ >8 -+/ { exit } !/^#/')
else
    text=$raw
fi

fail=0

check() {
    local label=$1 hint=$2 regex=$3 hits
    hits=$(printf '%s\n' "$text" | grep -nEi -- "$regex" || true)
    [ -z "$hits" ] && return 0
    fail=1
    printf '\n  ✖ %s\n' "$label" >&2
    printf '%s\n' "$hits" | sed 's/^/      /' >&2
    printf '    → %s\n' "$hint" >&2
}

# SC2016: the ${...} in these hints are the literal placeholder names we want
# the author to type — expanding them would print empty strings as the advice.
# shellcheck disable=SC2016
check "RFC1918 address" \
    'Describe it generically ("the NAS endpoint") or use the ${SVC_*_ADDR} placeholder.' \
    '\b(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b'

# shellcheck disable=SC2016
check "real domain name" \
    'Write ${SECRET_DOMAIN} / ${SECRET_INTERNAL_DOMAIN} instead — that is why the placeholders exist.' \
    'codelooks\.com'

check "internal hostname suffix" \
    'Internal hostnames map the home network. Use a placeholder or omit it.' \
    '[a-z0-9-]+\.(lan|internal)\b'

check "AI-tooling attribution" \
    'Drop the session link / co-author trailer; it is world-visible and links a private session.' \
    'claude\.ai/code/session|co-authored-by:[[:space:]]*claude|generated with \[claude'

if [ "$fail" -ne 0 ]; then
    cat >&2 <<'EOF'

    This repository is PUBLIC — commit messages and PR bodies are world-visible,
    and a squash merge makes a PR body permanent history. See AGENTS.md.
    Deliberate exception: git commit --no-verify
EOF
    exit 1
fi
