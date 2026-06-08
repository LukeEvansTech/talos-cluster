# Ollama models

Models are **runtime-provisioned**, not reconciled by Flux. The HelmRelease only
manages the Deployment/StatefulSet; model blobs live on each replica's per-ordinal
`models-ollama-<N>` PVC (they survive pod restarts and rollouts, not PVC deletion).

This directory holds Modelfiles for reproducibility — apply them with
`ollama create` on each replica when provisioning a fresh PVC.

## Current standard

- **Production:** `qwen3.6:27b` (Ollama library) — `ollama pull qwen3.6:27b`
- **Candidate (Acreage scoring):** `qwen3.6-35b-a3b` — built from
  [`qwen3.6-35b-a3b.Modelfile`](./qwen3.6-35b-a3b.Modelfile)

Always call these with the chat endpoint, `think: false`, and a `format` JSON
schema for structured extraction — the schema constraint eliminates occasional
output-shape wobble on the 35B and guarantees valid typed objects.
