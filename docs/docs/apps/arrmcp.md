# arrmcp

## Purpose

`arrmcp` runs [arr-mcp](https://github.com/bardesss/arr-mcp) in the `ai` namespace, an MCP
server for the media stack (Radarr, Sonarr, Bazarr, Prowlarr, Seerr, SABnzbd, qBittorrent,
Plex). ToolHive registers it as a backend in the `mcp-tools` group. See the
[AI / LLM stack](../architecture/ai-llm-stack.md) page for how that group fits together.

## Design decisions

- Config keys are validated by a zod *strict* object per service, so an unknown or misspelled
  key at service level fails at startup. Use `destructive`, not `unsafe_write`: arr-mcp's
  README names the latter, the code names the former (`src/config/schema.ts`
  `PermissionsSchema`), and because that inner object is non-strict, the README's key would
  have been silently dropped rather than rejected.
- `/config` stays writable even though `config.yaml` inside it is mounted read-only:
  `LogStore.open()` and `WriteAudit.open()` both open a database inside the config directory,
  and `WriteAudit` throws if it cannot. The PVC is deliberately not VolSync-backed: nothing in
  it is irreplaceable, and keeping it out of the backups is what keeps the media stack's API
  keys out of them too.
