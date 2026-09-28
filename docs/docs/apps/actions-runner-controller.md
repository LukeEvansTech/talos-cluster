# Actions runner controller

## Purpose

Actions-runner-controller (ARC) is the `gha-runner-scale-set-controller` chart: one controller
Deployment in `actions-runner-system` that every runner scale set's `controllerServiceAccount`
points at. Two Flux Kustomizations apply it. `actions-runner-controller` renders `app/`, and
`actions-runner-controller-runners`, which `dependsOn` the first, renders `runners/`: one directory
per GitHub org or repository scale set.

Ten scale sets exist, each a self-hosted runner pool for one GitHub target. The scale set name
matches its `runners/` directory name except for `packer`, whose `runnerScaleSetName` is
`packer-vsphere`:

| Scale set           | Target                           | Mode       | `maxRunners` | Work storage |
| ------------------- | -------------------------------- | ---------- | ------------ | ------------ |
| `codelooks-org`     | codelooks-com (org, shared pool) | dind       | 2            | 40Gi         |
| `lurcher`           | LukeEvansTech/lurcher            | dind       | 1            | 40Gi         |
| `networkops`        | LukeEvansTech/network-ops        | dind       | 1            | 40Gi         |
| `nut`               | LukeEvansTech/nut-apps           | dind       | 1            | 40Gi         |
| `packer-vsphere`    | codelooks-com/packer-vsphere     | kubernetes | 3            | 40Gi         |
| `seedbox`           | LukeEvansTech/seedbox-apps       | dind       | 1            | 40Gi         |
| `subspy`            | LukeEvansTech/subspy             | dind       | 1            | 40Gi         |
| `talos-cluster`     | LukeEvansTech/talos-cluster      | dind       | 3            | 25Gi         |
| `terraform-vsphere` | codelooks-com/terraform-vsphere  | kubernetes | 2            | 5Gi          |
| `truenas`           | LukeEvansTech/truenas-apps       | dind       | 1            | 40Gi         |

The six newest scale sets (`lurcher`, `networkops`, `nut`, `seedbox`, `subspy`, `truenas`) start
their `maxRunners` at 1 and are meant to grow once real usage justifies it. `codelooks-org`, the
shared pool, starts at 2 for the same reason. `talos-cluster`, `packer-vsphere` and
`terraform-vsphere` predate that pilot convention and already run at higher ceilings.

## GitHub App authentication

Each scale set's `ExternalSecret` extracts a GitHub App's credentials from one of two 1Password
items: `github` for the seven LukeEvansTech-scoped runners, `github-codelooks` for the three
codelooks-com-scoped ones (`codelooks-org`, `packer`, `terraform-vsphere`).

`codelooks-org` is org-scoped rather than repository-scoped, and that changes what permission the
GitHub App needs. An org-scoped scale set needs the app to hold the organisation permission
"Self-hosted runners: Read and write" (#4410); repository-scoped registration, which is all the
other scale sets have needed, only needs the repository-level one. If the organisation permission
is missing, the listener fails to register the scale set, and the error surfaces in the listener
pod rather than in the `ExternalSecret`.

## fsGroup and runner storage

All ten scale sets mount their `_work` volume from `miroir-local`, a real ext4 block device whose
root directory comes up `root:root 0755`. The runner images all run as uid/gid 1001, so every pod
spec sets `securityContext.fsGroup: 1001`. Without it, the runner cannot create `_work/_tool`, and
every job dies within about two seconds with `UnauthorizedAccessException` before a single step
runs. `miroir-local`'s CSIDriver reports `fsGroupPolicy: ReadWriteOnceWithFSType`, and the claim is
`ReadWriteOnce` plus ext4, so kubelet honours `fsGroup` and chowns the volume on mount.

This broke all three scale sets that existed when their work volumes moved from `openebs-hostpath`
(an already-writable host directory) to `miroir-local` in #3583. The fix landed in #3739.
`talos-cluster` failed the same way but silently: its only privileged job, image pre-pull, kept
skipping for unrelated reasons, so nothing exercised `_work/_tool` until after the fix had already
merged.

The dind-mode scale sets that build container images size their `work` claim at 40Gi, roomier than
`talos-cluster`'s 25Gi, because the `super-linter` image alone is several gigabytes and `checkov`
and `trivy` add more. That claim never holds dind's own image store, which lives on the sidecar's
writable layer (node ephemeral storage) instead. Spegel mirrors containerd pulls cluster-wide but
not dind's, so every dind job pulls `super-linter` from `ghcr.io` fresh. `terraform-vsphere`'s
kubernetes-mode claim only needs 5Gi: Terraform's own footprint is remote state plus a handful of
small providers.

## Docker sidecar resources

The `gha-runner-scale-set` chart's dind mode generates a Docker sidecar container with no values
field to size it. Seven of the eight dind-mode scale sets (`codelooks-org`, `lurcher`,
`networkops`, `nut`, `seedbox`, `subspy`, `truenas`) carry a `postRenderers` Kustomize patch that
adds resource requests and limits to that sidecar after the chart renders. `talos-cluster`, the
oldest scale set in the fleet, carries no such patch and sets its runner environment directly
instead (`ACTIONS_RUNNER_CONTAINER_HOOKS`, `ACTIONS_RUNNER_POD_NAME`) rather than relying on the
chart's default dind wiring the way the newer scale sets do.

## Kubernetes-mode runners: no pod-management RBAC

`packer-vsphere` and `terraform-vsphere` run in `containerMode: kubernetes`, whose chart normally
auto-creates a permissive ServiceAccount that lets `container:` jobs, service containers and
`docker://` actions run. Both scale sets instead point `serviceAccountName` at a ServiceAccount
with no pod-management Role, which suppresses that auto-created one, so those three step types
fail; plain `run:` steps and non-container actions such as JavaScript or composite actions still
execute normally, since they run in the runner's own process rather than a separate pod. Both also
override `ACTIONS_RUNNER_REQUIRE_JOB_CONTAINER` from kubernetes mode's default of `true` to
`false`, because their steps run directly on the runner. `terraform-vsphere` needs no Kubernetes
permissions at all: its steps only talk to vCenter and Cloudflare R2.

## talos-cluster: os:admin access and its guardrail

`talos-cluster` is the one scale set with real cluster privilege. Its `rbac.yaml` mints a
`talos.dev` `ServiceAccount` with the `os:admin` role into a Secret also named
`talos-cluster-runner`, mounted at `~/.talos/config` under key `config`. The runner pods
themselves carry no Kubernetes RBAC, because their only privileged job runs `talosctl image pull`,
never `kubectl`.

This wiring has broken once already. Pointing the volume at the similarly named
`talos-cluster-runner-secret` (the GitHub App credentials, which carry no `talosconfig` key) with
`subPath: talosconfig` rendered an empty directory instead of a file, and `talosctl` failed with
"is a directory" on every image-pull run until #4329 fixed it. Because this runner holds
`os:admin`, the workflow that uses it triggers on `push` to `main`, never `pull_request`, so
untrusted PR code is never scheduled onto it.

## packer: pinned to a digest

`packer`'s runner image is `codelooks-com/packer-runner`, built by that private repository rather
than by home-operations. It is pinned to an immutable digest rather than `:latest`: a mutable tag
once let a node keep a stale cached image whose baked Ansible virtualenv was missing `pywinrm`,
which failed Windows template builds with "No module named 'winrm'" (#3034). Renovate bumps the
digest as new images are built.

## Secret templating conventions

`terraform-vsphere` and `packer` each carry a second `ExternalSecret` (`terraform-vsphere-creds`,
`packer-vsphere-creds`) that surfaces vSphere and backend credentials as the environment variables
their own tool reads natively: `TF_VAR_<name>` for Terraform, `PKR_VAR_<name>` for Packer. Both
templates carry one literal, non-1Password field, `TF_VAR_vsphere_unverified_ssl` and
`PKR_VAR_vsphere_insecure_connection` respectively, both `"true"`, because vCenter presents a VMCA
certificate neither pod trusts. `terraform-vsphere-creds` also carries the Cloudflare R2 access
keys the OpenTofu S3 backend uses for remote state.
