# kguardian spike

A **time-boxed evaluation** of [kguardian-dev/kguardian](https://github.com/kguardian-dev/kguardian):
an eBPF runtime-observability tool that watches real pod traffic/syscalls and
synthesises least-privilege **NetworkPolicy**, **CiliumNetworkPolicy**, and
**seccomp** profiles via a `kubectl` plugin.

> **Why this lives in `docs/spikes/` and not `kubernetes/`:** the controller is a
> privileged, `hostNetwork`, host-mounting eBPF DaemonSet. It should be run by
> hand, vetted, used to harvest policies, then removed — not committed to the
> auto-reconciling Flux tree. Nothing here is picked up by Flux.

## Result (2026-06-07): ❌ blocked on Talos kernel 6.18

Ran live against this cluster (chart 1.12.0, controller 1.8.1, amd64,
kernel 6.18.29-talos). Broker, database, evaluator, and frontend all came up
healthy — but **the eBPF controller crash-loops on every node**: the kernel
verifier rejects its network-probe program at load:

```text
libbpf: prog 'trace_udp_send': BPF program load failed: -EINVAL
  10: (85) call bpf_probe_read#4
  program of this type cannot use helper bpf_probe_read#4
Error: Failed to load network probe eBPF: Invalid argument (os error 22)
```

`trace_udp_send` is an fentry/BTF tracing program (`BPF_PROG(... struct sock
*sk ...)`), and tracing-type programs may not call the legacy `bpf_probe_read`
helper — they must use `bpf_probe_read_kernel`. Talos's 6.18 kernel enforces
this strictly, so the program never loads and no traffic/syscall data is ever
captured. **No policies can be generated.** This is an upstream kguardian/libbpf
defect against newer kernels, not a Talos misconfiguration — the prerequisites
below are all satisfied.

**Verdict:** not usable on this cluster until upstream fixes the helper usage.
Filed for follow-up; recheck on a newer chart/controller release, or test
against a node pinned to an older (6.2–6.11) kernel.

## Prerequisites (all met — failure is at eBPF load, not setup)

| Requirement                                                                 | This cluster                                       | Verdict                                                  |
| --------------------------------------------------------------------------- | -------------------------------------------------- | -------------------------------------------------------- |
| Kernel 6.2+ (eBPF CO-RE)                                                    | Talos v1.13.2 → **Linux 6.18**                     | ✅                                                       |
| containerd socket `/run/containerd/containerd.sock`                         | Talos CRI containerd default path                  | ✅ (no override, unlike k3s)                             |
| runtime bundle `/run/containerd/io.containerd.runtime.v2.task`              | Talos CRI default                                  | ✅                                                       |
| `/sys/fs/bpf`, `/sys/kernel/debug`, `/sys/kernel/tracing`, `/proc` hostPath | present on Talos nodes                             | ✅                                                       |
| `privileged: true` + `CAP_BPF` + `hostNetwork` DaemonSet                    | Talos enforces PSA                                 | ⚠️ needs **privileged** namespace (see `namespace.yaml`) |
| PostgreSQL backend                                                          | bundled PG18 on `ceph-block` (spike) / CNPG (prod) | ✅                                                       |
| Tolerates `node-role.kubernetes.io/control-plane:NoSchedule`                | 3× control-plane nodes                             | ✅ runs on all nodes                                     |

**The one real risk** — `hostNetwork: true`. We've had a `hostNetwork`
conntrack-flood incident on this cluster before (scanopy). kguardian is
_passive_ eBPF observation (not active scanning), so it should not flood
conntrack — but watch node conntrack while it runs:
`talosctl -n <node> read /proc/sys/net/netfilter/nf_conntrack_count`.

**Licensing note:** BSL 1.1 (converts to Apache-2.0 on 2029-01-01), not OSI-OSS today.

## Run the spike

```bash
# 1. Privileged namespace (Talos PSA requires this for the eBPF DaemonSet)
kubectl apply -f docs/spikes/kguardian/namespace.yaml

# 2. Install the chart (bundled Postgres on ceph-block)
helm install kguardian oci://ghcr.io/kguardian-dev/charts/kguardian \
  --version 1.12.0 \
  --namespace kguardian \
  --values docs/spikes/kguardian/values.yaml \
  --wait

# 3. Verify — controller (DaemonSet, 1/node), broker, db, frontend
kubectl get pods -n kguardian -o wide

# 4. Install the kubectl plugin
sh -c "$(curl -fsSL https://raw.githubusercontent.com/kguardian-dev/kguardian/main/scripts/quick-install.sh)"
kubectl kguardian --version
```

## Harvest policies for one namespace

kguardian learns from observed behaviour, so **let the target workload run
5–15 min** after install before generating. `--dry-run=true` is the default —
it writes YAML to `--output-dir` and applies nothing.

```bash
# Pick a self-contained app to profile, e.g. it-tools in `default`.
# Generate a Cilium policy (this cluster's CNI) — dry-run, written to ./policies
kubectl kguardian gen ciliumnetworkpolicy -n default --output-dir ./policies

# Or a single workload + a plain NetworkPolicy / seccomp profile
kubectl kguardian gen networkpolicy <pod> -n default --output-dir ./policies
kubectl kguardian gen seccomp        <pod> -n default --output-dir ./policies

# Review before doing anything with it
ls ./policies && cat ./policies/*.yaml
```

Run `kubectl kguardian gen --help` for the full subcommand/flag set.

## Teardown

```bash
helm uninstall kguardian -n kguardian
kubectl delete -f docs/spikes/kguardian/namespace.yaml   # also removes the bundled PVC
```

## Productionising (only if the spike proves out)

- Move to Flux under `kubernetes/apps/security/kguardian/` (standard 4-file pattern).
- Point the broker at **CNPG** instead of bundled Postgres:
  `database.enabled=false`, `database.external.host=<cnpg-rw-svc>`,
  `database.external.sslMode=require` (matches the cluster's CNPG TLS default),
  and provide creds via `database.existingSecret` sourced from a 1Password
  ExternalSecret. Pre-create the `rust` role + `kube` database on CNPG.
- Decide whether the controller runs continuously (drift detection) or is
  spun up periodically just to regenerate policies.

## Status

Run live on 2026-06-07 and **torn down** — see the Result section above. The
install + runbook here are kept as a vetted, ready-to-run reference for a recheck
once upstream fixes the eBPF helper usage (or against an older-kernel node).
