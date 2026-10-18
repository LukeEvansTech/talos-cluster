# Suggestarr

Suggestarr (`kubernetes/apps/media/suggestarr`) watches what Plex users play, asks an LLM for
similar films and series, and files requests in Seerr. It replaced Recommendarr, archived in #6043
because its upstream had published no image since April 2025 while Suggestarr still ships regular
releases.

None of its settings are in Git: the app keeps them on its PVC and edits them through its own UI,
so the HelmRelease sets only the port and log level.

## Where the configuration lives

The `suggestarr` PVC is mounted at `/app/config`. The settings file is
`/app/config/config_files/config.yaml`, next to the SQLite database `requests.db`.

`config.yaml` holds API keys. Read single keys rather than the whole file, for example with a
`python3` one-liner run through `kubectl -n media exec deploy/suggestarr`, and confirm that a
secret is set by its length.

The integration settings are stored twice. The `integrations` table in `requests.db` holds the
Seerr, TMDB and LLM settings (plus Jellyfin, OMDb and Trakt when used), and on every load a
non-empty value from that table overrides the same key in `config.yaml`. At startup the table is
seeded from `config.yaml` only where a row is missing or invalid, so an edit to one of those keys
in the file alone never takes effect.

Copies of the earlier settings from the 2026-10-12 reconfiguration are kept beside the live file
as `config.yaml.bak-20261012` and `config.yaml.bak-20261012b`.

## Changing settings

Use the web UI. Its save (`POST /api/config/save`) writes `config.yaml` through
`api_service.config.config.save_env_vars` and then updates the `integrations` table, which is the
only path that keeps the two in step.

If a change has to be scripted, call the app's own functions inside the pod rather than editing
the YAML by hand:

- `save_env_vars` rewrites the whole file from the dict it is given, keeping only keys the app
  knows and dropping empty values. Start from `load_env_vars()` and change only the keys you mean
  to, or every other setting resets to its default.
- `save_env_vars` does not touch the `integrations` table. For a Seerr, TMDB or LLM key, also
  update its row with the database manager's `set_integration`.
- The server caches the merged settings in process, and a script run through `kubectl exec`
  clears only its own copy. Run `kubectl -n media rollout restart deploy/suggestarr` afterwards.

## Wiring

| Setting                    | Value                                          | Why                                                                     |
| -------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------- |
| `SELECTED_SERVICE`         | `plex`                                         | Watch history comes from Plex                                           |
| `PLEX_API_URL`             | `http://plex.media.svc.cluster.local:32400`    | In-cluster, so no route or TLS is needed                                |
| `SEER_API_URL`             | `http://seerr.media.svc.cluster.local`         | Seerr's Service listens on port 80                                      |
| `OPENAI_BASE_URL`          | `http://litellm.ai.svc.cluster.local:4000/v1`  | LiteLLM's OpenAI-compatible endpoint                                    |
| `LLM_MODEL`                | `self-hosted`                                  | LiteLLM falls back to `claude`, then `openrouter/auto`                  |
| `OPENAI_API_KEY`           | the `suggestarr` LiteLLM virtual key           | Scoped to those three models                                            |
| `TMDB_API_KEY`             | the TMDB key from the `kometa` 1Password item  | Reuses Kometa's key instead of a second TMDB account                    |
| `REQUIRE_REQUEST_APPROVAL` | `true`                                         | An LLM picks the titles, so a human approves each one before it reaches Seerr |
| `CRON_TIMES`               | `0 0 * * *`                                    | The app default, one run a day                                          |
| `PLEX_LIBRARIES`           | empty                                          | Empty means every library                                               |
| `SELECTED_USERS`           | empty                                          | Empty means every user                                                  |

Approval happens in Suggestarr, not Seerr. Each suggestion waits as `awaiting_approval` in
Suggestarr's own request queue (the `pending_requests` table) until its owner or an admin
approves it there, so a quiet Seerr does not mean the job is stalled. A job whose delivery mode
is `manual` or `automatic` overrides the global setting; the one job here, the daily
`recommendation` job, uses `inherit`.

The virtual key is declared in `kubernetes/apps/ai/litellm/app/virtualkeys/suggestarr.yaml`
(#6041), and its PushSecret mirrors it to the `litellm` 1Password item as
`LITELLM_SUGGESTARR_API_KEY`. The app takes it from there through the UI; no ExternalSecret
delivers it.
