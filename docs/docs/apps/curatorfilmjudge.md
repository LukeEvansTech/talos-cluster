# Curatorfilmjudge

The taste half of the weekly Radarr library cleanup, in the `custom` namespace. Reads the plan
`curatorfilm` left on the shared volume, asks Claude which candidates are worth keeping, and puts
each verdict on record to wait out a review window; it then carries out the verdicts from earlier
runs whose wait is over and that the engine still agrees with. `curatorfilm`'s own
`app/resources/curator/README.md` covers the engine side; this page covers the judge's own
CronJob, its trust boundary, and how a failed run is meant to be read.

## Three steps, kept separate on purpose

The CronJob runs three containers in order, all on the same Python image:

1. `prompt` (`build_prompt.py`) splits the plan's candidates into batches of
   `CLEANUP_JUDGE_BATCH` (50 here) and writes one file per batch.
2. `judge` (`judge.py`) sends each batch to Claude through the cluster's LiteLLM gateway, one
   request per batch, and writes one result file per batch.
3. `app` (`read_verdicts.py`, then the engine's `execute` and `report`) merges the results,
   records them as proposals, carries out the proposals that are due, and sends the digest.

Only `judge` talks to a model, and it is treated as the least-trusted step in the pipeline:

- It holds no credential beyond the gateway key (a separate `ExternalSecret` from the
  Radarr/Tautulli/Seerr/webhook one `app` uses).
- It gets no write access to the state volume at all (see below).
- Every answer it returns is re-validated by the executor against live Radarr immediately before
  each deletion, covering tags, collections, status, file presence and playback.

A wrong or hostile answer out of `judge` can therefore only choose badly among films the executor
independently re-clears; it cannot forge a candidate, bypass a protection, or reach the library
directly. A `keep` is the one answer with a lasting effect of its own: once its wait is over it
becomes a `keep` tag in `act` mode, so both `keep` and `delete` need a reason of at least 12
characters. Neither is acted on in the run that gives it; see
[the review window](curatorfilm.md#the-review-window).

## The judge never sees the state volume

`persistence.state` in `app/helmrelease.yaml` is an `advancedMounts`, not a `globalMount`:
`prompt` gets it mounted read-only, `app` gets read-write, and `judge` gets no mount at all. With
write access, a compromised judge step (a malicious PyPI dependency, for one) could rewrite
`plan.json` to insert a protected film into `candidates`, which the executor trusts as the base
set. The executor's live re-checks cover tags, collections, status, file presence and playback,
not membership of the candidate list itself.

## The request to the gateway

`judge.py` uses the official `anthropic` Python SDK with a zero-argument client. The manifest
supplies `ANTHROPIC_BASE_URL` (the in-cluster LiteLLM service) and the Secret supplies
`ANTHROPIC_AUTH_TOKEN`, which the SDK sends as `Authorization: Bearer`. Each batch is one
`POST /v1/messages`:

- `system` is `taste.md`, unchanged.
- The one user message is the batch's candidate records as JSON.
- `output_config.format` is a JSON schema:
  `{"verdicts": [{"movie_id": int, "verdict": "delete" | "keep" | "review", "reason": str}]}`.
- `max_tokens` is 16000, the most a non-streaming request can ask for inside the SDK's ten-minute
  timeout.
- Nothing else is sent: no sampling, thinking or effort settings, so the model's defaults apply,
  and no `fallbacks` field, which the gateway reads its own way.

`JUDGE_MODEL` is the gateway model group `claude-opus`, not a model id. The gateway answers with
the group's name in `model`. That group is deliberately outside the router's fallbacks: the
general group falls back to a local model when the budget is spent or the provider key lapses, and
a deletion verdict from a fallback model is not acceptable.

A batch fails, with the reason recorded, when:

- `stop_reason` is anything but `end_turn` (a refusal and `max_tokens` included);
- the reply's `model` is not the group that was asked for;
- the SDK raises after its own retries (its defaults are left on: two retries, ten-minute timeout);
- the JSON does not match the schema. It is checked again in `judge.py` although the API
  constrains it, because the reply crosses a gateway.

A batch that hits `max_tokens` fails by this rule and its films come back next week, so the
remedy for a recurring one is a smaller `CLEANUP_JUDGE_BATCH`.

The step never exits non-zero. Each batch's outcome is written to the work volume, and a failure
before or between batches is written as a `judge-failed` marker, so `app` always runs and the
digest always goes out. No batch is started more than 25 minutes into the step: one request can
take 30 minutes at worst (three attempts at the SDK's timeout), which keeps the step inside the
Job's one-hour `activeDeadlineSeconds`.

## The SDK installs at start-up

The `judge` initContainer runs `pip install --target /tmp/sdk anthropic==<version>` at start-up
(the root filesystem is read-only and the pod runs as a non-root user), then
`PYTHONPATH=/tmp/sdk python3 /prompt/judge.py`. `ANTHROPIC_SDK_VERSION` is pinned in the manifest
so the exact version is visible in Git; its dependencies are whatever pip resolves that day. The
SDK's compiled dependencies (`pydantic-core`, `jiter`) ship musllinux wheels for the Alpine image
this uses, so nothing is built in the pod. The install and one run peak at about 215 MiB, hence
the container's 512 MiB limit.

This is a supply-chain surface: a bad package runs with the gateway key and nothing else. It is
not a network boundary either, since nothing stops the process making its own outbound
connection.

## Files between the steps

All three steps share an `emptyDir` at `/work` that dies with the pod.

| File                  | Written by | Holds                                                                    |
| --------------------- | ---------- | ------------------------------------------------------------------------ |
| `skip`                | `prompt`   | why nothing is being asked; `judge` exits without calling anything       |
| `batches.json`        | `prompt`   | the plan's `run_id`, the id field, and each batch's name and ids         |
| `candidates-NNN.json` | `prompt`   | one batch's records, sent as the user message as-is                      |
| `result-NNN.json`     | `judge`    | one batch's outcome (below)                                              |
| `judge-failed`        | `judge`    | why the step stopped early, including a failed SDK install               |

A result file is one JSON object:

```json
{
  "plan_run_id": "run-20261121T080001-ab12cd",
  "batch": "001",
  "status": "ok",
  "why": "",
  "verdicts": [{ "movie_id": 123, "verdict": "delete", "reason": "…" }],
  "model": "claude-opus",
  "stop_reason": "end_turn",
  "usage": { "input_tokens": 12000, "output_tokens": 3400 },
  "duration_ms": 91000
}
```

`status` is `ok` or `failed`; `verdicts` is present only when it is `ok`, and `why` says what
failed otherwise. `read_verdicts.py` takes none of it on trust: a file that names another plan or
another batch is refused, as is one whose `verdicts` is not a list of objects. A verdict naming a
film that belongs to a different batch is dropped, because the judge never saw that film's
record. The merged result is `recommendations.json`, and `judgement.json` carries the status
(`judged`, `partial`, `failed`, `malformed` or `not asked`), the per-batch failures, the input
and output token totals the digest reports, and the model group that answered.

One failed batch does not discard the others. Its films have no verdict this run, `execute` counts
them as unjudged and exits 9, and `read_verdicts.py` itself exits 1, so the Job fails and the
staleness alert is not reset.

## Limits and deferral

| Setting                 | Where it is set              | Value | Effect                                                  |
| ----------------------- | ---------------------------- | ----- | ------------------------------------------------------- |
| `CLEANUP_JUDGE_LIMIT`   | `curatorfilm` (the planner)  | 150   | Candidates offered to the judge per run, largest first. |
| `CLEANUP_JUDGE_BATCH`   | `curatorfilmjudge`, `prompt` | 50    | Candidates per request.                                 |
| `CLEANUP_MAX_DELETIONS` | `curatorfilmjudge`, `app`    | 100   | Deletions per weekly run; the engine's default is 30.   |
| `CLEANUP_GRACE_DAYS`    | `curatorfilmjudge`, `app`    | 6     | Days a verdict waits before `execute` acts on it.       |

The judge limit is applied where the plan is written, not here: `plan.json` lists the offered
candidates as `candidates` and the rest as `judge_deferred`. A deferred candidate is not an
unjudged one, so it does not fail the run; the digest says how many were deferred. The deletion
cap is raised to 100 to work through the first backlog and is meant to go back to the default.

## The digest and the review document

`report` reads the proposals from the ledger and builds the review document on every run. It reads
the ledger and not this run's result, so a run that was blocked, or whose `execute` never
finished, still lists every verdict that is waiting. The digest leads with the number a person can
act on: its title says how many titles go at the next run and on what date (`would go` in a dry
run), and its text lists them with the judge's reasons.

Publishing the document is optional and on here. With `CLEANUP_REVIEW_URL` set, `report` renders
the digest, puts it in the document, and posts the document there signed with the Radarr API key
this step already holds; the push then links to `CLEANUP_REVIEW_LINK` when that is set, through
the `chaski` route's `url` parameter, which is left off the notification when the digest names no
link. A receiver that did not take the document is named in the digest and makes `report` exit 11.
This manifest sets it to the [Curator](curator.md) Service,
`http://curator.custom.svc.cluster.local:8080`, and the link to `https://curator.${SECRET_DOMAIN}/`,
the page where the week's verdicts can be overruled before the next run. With
`CLEANUP_REVIEW_URL` unset nothing is sent and the digest mentions no page. The contract is in [Curatorfilm](curatorfilm.md#the-review-document) and the engine's README.

## Never retry: `backoffLimit: 0`

The CronJob sets `backoffLimit: 0` and `concurrencyPolicy: Forbid`. This pipeline ends in DELETEs,
and a Kubernetes-driven retry would re-run judgement and execution against a plan the previous
attempt may have already partly acted on. A non-zero exit here only marks the Job failed; it never
makes Kubernetes itself retry the pipeline. In the shared `curator` engine code, `cmd_execute`
treats a failed deletion (Radarr answered with a non-2xx status) as a safe no-op to retry, and an
uncertain one (a transport error after issuing the DELETE) as unsafe to retry: `_delete_one`
records it and leaves it rather than re-issuing the DELETE.

## The gateway key

The judge's only credential is a LiteLLM virtual key, scoped to the `claude-opus` group and
mirrored to the `litellm` 1Password item as `LITELLM_CURATOR_API_KEY`. The `curatorfilmjudge-gateway`
`ExternalSecret` templates it into its own Secret as `ANTHROPIC_AUTH_TOKEN`; nothing else is in
that Secret. The key and the model group are declared with the gateway, under
`kubernetes/apps/ai/`, and the Flux Kustomization depends on `litellm` for that reason. See
[AI and LLM stack](../architecture/ai-llm-stack.md) for how virtual keys are issued and rotated.

A key the gateway does not accept, or one that may not use the group, fails every batch with the
gateway's own error in the digest (`401` or `403`), and the run gives no new verdicts. It does not
stop deletion: `execute` still acts on proposals from earlier runs that are now due, then exits 9
for the unjudged candidates. A failed judge run is therefore not proof that nothing was deleted;
the digest's execution section says what was.
