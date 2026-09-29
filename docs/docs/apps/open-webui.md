# Open WebUI

## Purpose

`open-webui` is the chat UI for the AI stack, talking to LiteLLM's OpenAI-compatible endpoint.
See the [AI / LLM stack](../architecture/ai-llm-stack.md) page for how it fits the wider stack.

## Deploy gotchas

- `TIMER_POLL_INTERVAL` is set to 30, not the default of 1. Since v0.11.0, open-webui starts its
  scheduler loop unconditionally and polls for due timers before checking whether automations
  are enabled at all. That poll query filters on three `JSON_EXTRACT` predicates over
  `chat.meta` that no index can serve, so SQLite full-scans the chat table every second, even
  with no timers in use here. The cost grows with chat history, not timer count. 30s trades
  timer latency for CPU; drop back to the default of 1 once the upstream fix
  (open-webui#27663, unreleased) ships.
- `v0.11.0` renamed the web search settings from `RAG_WEB_SEARCH_*` / `ENABLE_RAG_WEB_SEARCH` to
  `WEB_SEARCH_*` / `ENABLE_WEB_SEARCH` (`backend/open_webui/config.py:1129,1138,1147,1164,1185`);
  the old names are not read by this version.
- `ENABLE_WEB_SEARCH` is a `PersistentConfig` value: it only seeds the SQLite
  `web.search.enable` row on that row's first-ever write, and is ignored once the row exists
  (`models/config.py`'s `seed_defaults()` docstring: "Existing DB values take precedence over
  defaults"). This app previously ran with web search off, so the row is already seeded false
  on the live PVC, and flipping this env var does not retroactively flip it. After deploying,
  enable web search by hand in Admin Settings → Web Search.
