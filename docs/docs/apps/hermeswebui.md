# Hermes WebUI

## Purpose

`hermeswebui` is the chat web frontend for hermes. It talks to `hermes-app`'s OpenAI-compatible
gateway API server rather than its own legacy backend. See the
[AI / LLM stack](../architecture/ai-llm-stack.md) page for how it fits the wider stack.

## Design decisions

- The image bakes `groupadd -g 1024 / useradd -u 1024 hermeswebui`. Its
  `/hermeswebui_init.bash` entrypoint normally starts as root, aligns the runtime UID/GID with
  the mounted volumes, then execs the server as 1024. Started directly as 1024, it detects the
  non-root start and skips that root branch instead, seeding `/app` and building the venv as
  1024 and asserting UID/GID == 1024. That lets the pod run as 1024 directly, with no self-chown
  step. `fsGroup: 1024` makes the `/data` PVC group-writable so `HERMES_WEBUI_STATE_DIR`
  persists across restarts.
