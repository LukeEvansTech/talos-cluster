# Open WebUI

## Purpose

`open-webui` is the chat UI for the AI stack, talking to LiteLLM's OpenAI-compatible endpoint.
See the [AI / LLM stack](../architecture/ai-llm-stack.md) page for how it fits the wider stack.

## Deploy gotchas

- `TIMER_POLL_INTERVAL` is left at its default of 1. From v0.11.0 the always-on scheduler loop
  polled for due timers with three `JSON_EXTRACT` predicates over `chat.meta` that no index could
  serve, so SQLite full-scanned the chat table every second; this app ran with 30 as a
  workaround. open-webui#27663 (first released in v0.11.1) moved the poll onto an indexed
  `chat.timer_at` column, and the override was dropped on v0.13.0.
- `v0.11.0` renamed the web search settings from `RAG_WEB_SEARCH_*` / `ENABLE_RAG_WEB_SEARCH` to
  `WEB_SEARCH_*` / `ENABLE_WEB_SEARCH` (`backend/open_webui/config.py:1129,1138,1147,1164,1185`);
  the old names are not read by this version.
- `ENABLE_WEB_SEARCH` is a `PersistentConfig` value: it only seeds the SQLite
  `web.search.enable` row on that row's first-ever write, and is ignored once the row exists
  (`models/config.py`'s `seed_defaults()` docstring: "Existing DB values take precedence over
  defaults"). This app previously ran with web search off, so the row is already seeded false
  on the live PVC, and flipping this env var does not retroactively flip it. After deploying,
  enable web search by hand in Admin Settings → Web Search.
