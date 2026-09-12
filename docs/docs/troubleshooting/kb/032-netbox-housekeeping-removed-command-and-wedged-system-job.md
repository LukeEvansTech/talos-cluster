# KB-032: NetBox Housekeeping Fails Two Ways at Once (Removed Command, Wedged System Job)

**Status:** Resolved. `housekeeping.enabled: false` was set on the HelmRelease in
[#5084](https://github.com/LukeEvansTech/talos-cluster/pull/5084), and the wedged internal job was
repaired live. Both root causes are open upstream bugs, so expect to meet the second one again.

## Symptom

One `KubeJobFailed` per day for `default/netbox-housekeeping`, each Job
`BackoffLimitExceeded`. Nothing else about NetBox looks wrong: the web and worker pods are `Running`
with no restarts, the HelmRelease is `UpgradeSucceeded`, and the UI works.

The Job's own output is the whole story:

```console
$ kubectl exec deploy/netbox -c netbox -- \
    /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py housekeeping --help
Unknown command: 'housekeeping'
```

Note the version boundary in the Job history — this is the tell that it is an upgrade, not a
regression in the cluster:

```console
$ kubectl -n default get jobs -o custom-columns=NAME:.metadata.name,STATUS:.status.conditions[0].type,IMAGE:.spec.template.spec.containers[0].image
netbox-housekeeping-29806560   Complete   ghcr.io/netbox-community/netbox:v4.6.10
netbox-housekeeping-29812320   Failed     ghcr.io/netbox-community/netbox:v4.7.0
netbox-housekeeping-29813760   Failed     ghcr.io/netbox-community/netbox:v4.7.0
```

**And a second, silent failure hides behind the noisy one.** Housekeeping had already stopped two
weeks before the CronJob broke, with no alert at all. Check the internal job history, not just the
CronJob:

```console
$ kubectl exec deploy/netbox -c netbox -- /opt/netbox/venv/bin/python \
    /opt/netbox/netbox/manage.py nbshell -c \
    'from core.models import Job
for j in Job.objects.filter(name="System Housekeeping").order_by("-created")[:3]:
    print(j.created, j.status, j.scheduled)'
2026-08-19 13:53:23  scheduled  2026-08-20 13:53:22   <- stuck, three weeks in the past
2026-08-18 13:53:23  completed  2026-08-19 13:53:22
2026-08-17 13:53:23  completed  2026-08-18 13:53:22
```

A `scheduled` job whose scheduled time is in the past is the signature. `/core/background-queues`
in the UI shows 0 scheduled jobs on every queue, which contradicts it.

## Cause

Two unrelated upstream defects, both live at the same time.

### 1. The chart calls a management command NetBox deleted

`manage.py housekeeping` was deprecated in NetBox v4.6
([netbox#21304](https://github.com/netbox-community/netbox/issues/21304)) and **removed in v4.7.0**
([netbox#21565](https://github.com/netbox-community/netbox/issues/21565)). `netbox-chart` has not
caught up: as of 8.3.74 it still defaults `housekeeping.enabled` to `true` with that command baked
into `housekeeping.command`, so the CronJob cannot succeed on any v4.7.x image. Tracked as
[netbox-chart#1391](https://github.com/netbox-community/netbox-chart/issues/1391).

The v4.7.0 release note says the work is "performed by the individual management commands
introduced in NetBox v4.6". **Do not plan around that sentence** — the shipped image has no such
commands, so there is nothing to repoint the CronJob at:

```console
$ kubectl exec deploy/netbox -c netbox -- \
    find /opt/netbox/netbox -path '*/management/commands/*.py' ! -name '__init__.py'
.../core/management/commands/{makemigrations,nbshell,rqworker,syncdatasource,upgrade}.py
.../dcim/management/commands/{buildschema,trace_paths}.py
.../extras/management/commands/{populate_image_sizes,rebuild_config_context_cache,reindex,renaturalize,runscript,webhook_receiver}.py
.../ipam/management/commands/rebuild_prefixes.py
.../utilities/management/commands/calculate_cached_counts.py
```

None of the four housekeeping steps (`send_census_report`, `clear_expired_sessions`,
`prune_changelog`, `check_for_new_releases`) has a CLI entry point. They exist only as
`core.jobs.SystemHousekeepingJob`.

### 2. `enqueue_once` trusts a database row over the queue

The obvious reading of the stalled job — "scheduled jobs need a scheduler, and only `rqworker` is
deployed" — **is wrong, and worth resisting**, because it leads to deploying an `rqscheduler` that
is neither needed nor wanted. NetBox's `rqworker` subclass *is* the scheduler:

```python
# core/management/commands/rqworker.py
for job, kwargs in registry['system_jobs'].items():
    job.enqueue_once(**kwargs)
options['with_scheduler'] = True
```

The actual defect is in `enqueue_once` (`netbox/jobs.py`): it returns the existing `core.models.Job`
row whenever that row's `scheduled` time and `interval` match what is being asked for, and **never
checks that the corresponding job still exists in the queue backend**. Lose the queue-side job and
the row is left saying `scheduled` forever while nothing is enqueued, and every subsequent worker
start reads that row as proof a schedule exists and does nothing.
[netbox#22714](https://github.com/netbox-community/netbox/issues/22714) (open, accepted).

It takes the two stores disagreeing, which narrows the causes usefully:

- **The queue side lost the job** — a Dragonfly restart without persistence, or eviction.
- **The two were restored independently** — upstream's repro is a Postgres restore to a point
  before housekeeping ran, while Redis kept moving. The reverse works too.

**Worker downtime alone is not one of them**, and assuming it is will send you to delete a row that
did not need deleting. The scheduled entry lives in Redis, so it survives the worker being down
across its due time; RQ's embedded scheduler enqueues the overdue job when a worker comes back. If
the registry check below finds the job, it is merely pending — wait for it rather than reaching for
the workaround.

Prove it by resolving the row's own `job_id` against RQ, which is the check `enqueue_once` omits.
The question to answer is **"will anything still run this job?"**, and neither obvious check
answers it alone:

- **Presence in the scheduled registry is not it.** A healthy job leaves that registry the moment
  the scheduler promotes it into the queue, while the Django row stays `scheduled` until a worker
  starts it. A registry-only check therefore reports the wedge signature for any job merely waiting
  on a busy worker, and acting on that deletes a live row. Testing for **this specific ID** matters
  too: any other scheduled NetBox task would satisfy a bare count.
- **Existence of a job object is not it either.** RQ keeps job hashes around after they settle, so
  `RQJob.fetch()` can succeed for a job in a terminal state — `finished`, `failed`, `stopped`,
  `canceled` — that will never run again. The row still reads `scheduled`, `enqueue_once` still
  short-circuits on it, and that is just as wedged as the job being absent.
- **Nor is the status field, on its own.** The status lives in the job hash while the reference
  lives in a separate registry or queue key, so eviction can take one and leave the other: a hash
  still reporting `scheduled` with nothing in any `ScheduledJobRegistry` is wedged, however healthy
  the status reads.

```python
from core.models import Job
from rq.job import Job as RQJob
from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from rq.exceptions import NoSuchJobError
import django_rq

j = Job.objects.filter(name="System Housekeeping", status="scheduled").first()
jid = str(j.job_id)
try:
    print("rq status:", RQJob.fetch(jid, connection=django_rq.get_connection("default")).get_status().value)
except NoSuchJobError:
    print("rq status: MISSING")
for q in ("high", "default", "low"):
    queue = django_rq.get_queue(q)
    print(q, "scheduled:", jid in ScheduledJobRegistry(queue=queue).get_job_ids(),
             "queued:", jid in queue.get_job_ids(),
             "started:", jid in StartedJobRegistry(queue=queue).get_job_ids())
```

A job is healthy only if it is in a live state **and** something still holds a reference to it.
Both halves, on the same queue:

| `rq status` | Needs | Verdict |
| --- | --- | --- |
| `scheduled` | `scheduled: True` | Healthy — waiting for its due time |
| `queued` | `queued: True` | Healthy — due, waiting on a busy worker. **Do nothing** |
| `started` | `started: True` | Healthy — running right now |
| `MISSING` | — | Wedged — the queue side lost the job |
| `finished`, `failed`, `stopped`, `canceled` | — | Wedged — the object survives, nothing will run it |
| any of the first three | all `False` | Wedged — the status survived, the reference did not |

The last row is the one worth reading twice: a partial eviction leaves the hash reporting
`scheduled` while no registry references it, so the status alone looks healthy. Treat a live status
with no matching membership exactly like a missing job.

## Fix

**Disable the CronJob.** It has no working command and the worker covers the function:

```yaml
housekeeping:
  enabled: false
```

Keep the `affinity` block commented rather than deleting it — the RWO `media` PVC co-location
constraint still applies if the chart ever ships a working CronJob again.

**Un-wedge the system job** using upstream's documented workaround — delete the stale `scheduled`
row, then restart the worker so `enqueue_once` has nothing to short-circuit on:

```bash
kubectl exec deploy/netbox -c netbox -- /opt/netbox/venv/bin/python \
  /opt/netbox/netbox/manage.py nbshell -c \
  'from core.models import Job
Job.objects.filter(name="System Housekeeping", status="scheduled").delete()'
kubectl -n default rollout restart deploy/netbox-worker
```

The worker runs housekeeping immediately on start and schedules the next occurrence.

**Verify in both places.** The database row is exactly what lied for three weeks, so a `scheduled`
row on its own is not evidence. Re-run the check above against the *new* row and require the first
healthy shape — `rq status: scheduled`, and the row's own `job_id` present in `default`'s
`ScheduledJobRegistry`:

```console
rq status: scheduled
high scheduled: False queued: False started: False
default scheduled: True queued: False started: False
low scheduled: False queued: False started: False
```

Finally, clear the failed Jobs. `KubeJobFailed` latches on the Job object and keeps firing until
that object is gone, so fixing the cause does not silence it (see
[KB-007](007-flux-not-ready-artifact-failed-alert-storms.md) for the same latching behaviour in a
different alert). Pruning the CronJob deletes the Jobs it owns and clears them with it; delete them
by name if you want the alerts gone before that reconcile lands, or for any Job that has outlived
its owner:

```bash
kubectl -n default delete job netbox-housekeeping-29818080
```

Jobs that `failedJobsHistoryLimit` already rotated out need nothing — rotation *is* the controller
deleting them, so they are gone and no longer alerting.

## Still open

Nothing detects the wedge. It is silent by construction, and disabling the CronJob removed the
accidental canary that made it visible at all. A useful check would alert on a `core.models.Job` in
`scheduled` state whose `scheduled` timestamp is more than one `interval` in the past, read from
`/api/core/jobs/`.

Unrelated but noticed in the same code: `SystemHousekeepingJob` defines `delete_expired_jobs()` and
never calls it from `run()`.
