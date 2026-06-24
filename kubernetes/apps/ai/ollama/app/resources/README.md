# Ollama models

Models are **provisioned declaratively** by the `provisioner` sidecar in the Ollama
StatefulSet (see [`helmrelease.yaml`](../helmrelease.yaml)). The sidecar runs the ollama
image as a CLI client against the server in the same pod and reconciles the declared set onto
**each replica's** RWO `/models` PVC. Because it runs on every replica, the model set is
identical across the fleet by construction, and a fresh/blank PVC self-heals.

The files in this directory are the source of truth; `app/kustomization.yaml`'s
`configMapGenerator` bundles them into the `ollama-models` ConfigMap mounted at `/config`.

## Declared model set

| Model                       | Source                                                  | How                                                     |
| --------------------------- | ------------------------------------------------------- | ------------------------------------------------------- |
| `qwen3.6:27b`               | Ollama library                                          | `ollama pull` (`models.list`)                           |
| `qwen3.6-35b-a3b`           | `unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS`                | `ollama create` (`qwen3.6-35b-a3b.Modelfile`)           |
| `qwen3-30b-a3b-abliterated` | `mradermacher/Qwen3-30B-A3B-abliterated-i1-GGUF:Q4_K_S` | `ollama create` (`qwen3-30b-a3b-abliterated.Modelfile`) |

Each is ~17Gi, so they fit the ~17Gi VRAM budget on a 24Gi L4 (weights + KV for
`OLLAMA_NUM_PARALLEL=3` + overhead); all three together are ~52Gi of the 100Gi PVC.

- `qwen3.6-35b-a3b` is the cluster default (`OLLAMA_MODEL` → LiteLLM `self-hosted`).
- `qwen3-30b-a3b-abliterated` is the uncensored model, exposed as LiteLLM
  `self-hosted-uncensored` with **no** cloud fallback (a cloud model would reintroduce
  refusals). Two ~17Gi models can't co-reside on one 24Gi card, so alternating traffic
  between the default and the abliterated model triggers a model swap (~seconds).

## Adding / changing a model

1. **Library model** — add its ref to `models.list`.
2. **Custom/HF model** — drop a `<name>.Modelfile` here (`FROM hf.co/<org>/<repo>:<quant>` plus params); it is created as `<name>`. Use a quant Ollama accepts (standard scheme names like `Q4_K_S`/`IQ4_XS`, not bare `Q3_K`).
3. Add the Modelfile path to the `configMapGenerator.files` list in `app/kustomization.yaml` (`models.list` is already listed).
4. Commit. Flux applies the ConfigMap; Stakater Reloader restarts the pods; the reconciler pulls/creates the new model within one pass. Apply immediately with `kubectl -n ai rollout restart statefulset/ollama`.

Always call these models with the chat endpoint, `think: false`, and a `format` JSON schema
for structured extraction — the schema constraint eliminates occasional output-shape wobble
on the 35B and guarantees valid typed objects.

## Notes

- The reconciler is idempotent and resilient: it only pulls/creates what's missing, swallows
  transient failures, and loops every ~30m (`RECONCILE_INTERVAL`). It never exits, so it can't
  flip the pod to NotReady and drop the server from the Service.
- The exact 3.6-35B-A3B abliterated sibling (huihui-ai's `...-MTP-GGUF`) is intentionally not
  used: its files carry bare `Q3_K`/`Q4_K` quant tags that Ollama's HF puller rejects ("not a
  valid quantization scheme"), and only its `Q2_K` fits VRAM. mradermacher's imatrix GGUF of
  mlabonne's Qwen3-30B-A3B abliterated uses standard scheme names, so `Q4_K_S` pulls cleanly.
  Any Ollama-pullable GGUF (`FROM hf.co/<org>/<repo>:<standard-quant>`) works here.
