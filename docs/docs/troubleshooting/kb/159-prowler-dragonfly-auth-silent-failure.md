# KB-159: Prowler's Celery Worker Never Authenticated to Dragonfly

**Status:** Fixed in pull request #3390. Kept for the failure signature: the app looked healthy
throughout, and nothing about the API or the route hinted that scheduled scans had never run.

## Symptom

The `worker` container in `prowler-api` and the whole `prowler-beat` pod had well over a hundred
restarts each. Their logs showed the same loop:

```text
consumer: Cannot connect to redis://dragonfly.database.svc.cluster.local:6379/0: Authentication required..
Trying again in 32.00 seconds... (100/100)
```

followed by exit code 1 and a restart. Nothing else pointed at the problem: the route's health
check passed, the UI loaded, login worked, and the `api` container ran without error, because none
of those paths touch the broker. The only visible symptom was that scheduled scans never
dispatched, and there was no alert for that.

## Cause

Dragonfly, used here as the Celery broker and Django's cache, has required authentication since it
was deployed. The app's `ExternalSecret` only templated `VALKEY_HOST`, `VALKEY_PORT`, and
`VALKEY_DB`, with no credentials, so every Celery consumer failed the same authentication check and
hit its retry ceiling before exiting. The `worker` and `prowler-beat` containers depend entirely on
that connection, while the `api` container serves requests without it, which is why only two of the
three components ever showed symptoms.

## Fix

Template `VALKEY_USERNAME` and `VALKEY_PASSWORD` from the `dragonfly` 1Password item into the
app's `ExternalSecret`, the same pattern already used by paperless, netbox, and keeper. Both
HelmReleases already carry `reloader.stakater.com/auto`, so adding the credentials rolled the pods
without a manual restart.

When wiring a new app to Dragonfly, template both credential fields from the first commit: a
missing broker password fails quietly whenever some other part of the app still works without the
broker.

## References

- [talos-cluster#3390](https://github.com/LukeEvansTech/talos-cluster/pull/3390)
- [Prowler app page](../../apps/prowler.md)
