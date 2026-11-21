# GitLab

Throwaway lab in the `default` namespace for trying GitLab's security settings
(`gitlab.${SECRET_DOMAIN}`, internal only). Nothing depends on it. It runs the Linux package
image (`gitlab/gitlab-ee`), which bundles its own PostgreSQL 17 and Redis, because the
cluster's CNPG runs PostgreSQL 18 and GitLab 19 does not support it.

## Puma must run in cluster mode (19.6 and later)

From 19.6.0, Puma in single mode (`puma['worker_processes'] = 0`) fails to boot most of the
time. It logs `Unable to load application: ArgumentError: Can't reinstall subscriptions.
GitlabSchema is using …ActionCableWithLoadBalancing` and runit retries about every 26 s.
The 19.6.0 upgrade took the instance down from 2026-11-19 16:00 until the next morning,
apart from the odd boot that won the race.

The cause is a race at boot:

- In single mode `Gitlab::Cluster::LifecycleEvents.on_worker_start` runs its block
  immediately, during the Rails initializers, rather than after fork.
- In 19.6 that block starts `ActionCableSampler`, whose thread calls `GitlabSchema.subscriptions`.
- The sampler thread autoloads `app/graphql/gitlab_schema.rb` while the main thread is
  still eager-loading. The first load dies part-way with `NameError: uninitialized constant
  Types::CustomIntrospection::…` (swallowed by the sampler's `safe_sample`).
- The eager loader then requires the file again, and the second `use` of the subscriptions
  plugin raises.

With `worker_processes = 2` the hooks are deferred until after fork, so the sampler starts
only once loading has finished. Four restarts in a row came up clean in about 50 s each,
against hours of retries in single mode. Two workers use about 0.5 GiB more memory (around
4.3 GiB, against the 8 GiB limit).

Editing `workers` in `/var/opt/gitlab/gitlab-rails/etc/puma.rb` by hand does not test this.
The same file passes the worker count to `set_puma_worker_count`, and that value is what
decides single or cluster mode.

The Kubernetes probes are TCP checks on nginx, so the pod stays `Ready` while Puma
crash-loops behind it. Check `/var/log/gitlab/puma/current` rather than the pod status.

## No `fsGroup`

The pod runs as root and the Linux package sets its own file modes, so the pod has no
`fsGroup`. With `fsGroup: 0`, every mount made the files on the PVC group-writable, so
`/etc/gitlab/ssh_host_*_key` ended up at `0660`. sshd refuses keys that loose and restarted
every second, logging about 1.8 million lines a day.
