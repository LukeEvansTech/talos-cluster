# KB-206: Talos Client-Cred Fallback for Claude Code Cloud Sessions

**Status:** Reference, interim. Delete `.claude/hooks/session-start-talos-creds.sh`, its entry in
`.claude/settings.json`, and the `!.claude/hooks/` exception in `.gitignore` once the equivalent
fix lands in `LukeEvansTech/claude-cloud-env`'s own `hooks/session-start.sh` (branch
`claude/talos-client-cred-fallback` there, unmerged as of this writing).

## Overview

Claude Code cloud sessions get cluster read access through the cloud environment's own
`session-start.sh` (not in this repository), which runs `just talos gen-config`, then talhelper,
then `talosctl kubeconfig`. That path reconstructs the full clusterconfig and machine configs from
the 1Password `talsecret` item, so it pulls in the entire mise toolchain.

In the sandbox, mise's own aqua lookups read the sandbox's placeholder `GITHUB_TOKEN`, get a 401,
and `gen-config` fails before touching a single credential. Observed 2026-08-30: the hook logged
that `op` was reachable and authorised, so the failure was mise/toolchain resolution, not
1Password access. A session that only wants to read from the cluster then has no access at all,
and answers get inferred instead of verified.

`session-start-talos-creds.sh` builds a client-only talosconfig directly from the `talos`
1Password item's `TALOS_CA`/`TALOS_CRT`/`TALOS_KEY` fields plus `talconfig.yaml`'s endpoints,
skipping talhelper and the mise toolchain entirely.

## Scope

Client config only: enough to reach the Talos API and pull a kubeconfig. It does not produce
machine configs, so `talosctl apply-config` and the rest of the cluster-mutating flow still need a
real `just talos gen-config`. Those commands fail loudly on the missing files rather than running
half-configured, which is the intended failure direction.

## Design notes

- Runs only when `CLAUDE_CODE_REMOTE=true`. A workstation already has its own talosconfig and no
  `OP_SERVICE_ACCOUNT_TOKEN`, so minting a second credential set there would be a surprise.
- The cloud-env hook runs first (first in `settings.json`'s `SessionStart` array) and writes the
  same kubeconfig path this script checks for. Once the upstream fix lands, this script exits at
  its "already present" guard without a double-fetch, so the two removals never need coordinating.
- Clears `GITHUB_TOKEN`/`GH_TOKEN` before any `mise` call: mise's own installs read the sandbox's
  placeholder token from the environment and hit the same 401 as `gen-config`. Clearing it lets
  aqua fall back to anonymous GitHub access; `mise.lock` still enforces checksum integrity.
- Endpoints list every control-plane node from `talconfig.yaml`, not just one, so a single
  cordoned or down node doesn't cost the session its cluster access.
- talosctl's gRPC client honours only `HTTPS_PROXY`. `no_proxy`/`NO_PROXY` must be cleared too,
  because the sandbox lists RFC1918 and CGNAT ranges there, which would route the dial direct (no
  route) for exactly the addresses that need the tailnet proxy.
- Secrets go straight from `op read` into files under a `umask 077` temp directory, never through
  stdout, a variable, or the process table, because this script's own output is echoed into the
  session transcript.

## References

- `.claude/hooks/session-start-talos-creds.sh`
- `LukeEvansTech/claude-cloud-env`, `hooks/session-start.sh` (the eventual permanent home)
