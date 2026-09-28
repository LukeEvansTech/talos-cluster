# KB-157: Renovate digest bump silently changed a custom image's base runtime and broke its plugin

**Status:** Resolved. The lesson (track a custom image's base runtime and its bundled plugin as
separate versions) applies to any digest-pinned, custom-built image.

## Symptom

`sentinel-syslog` (a custom Logstash image bundling the Microsoft Sentinel output plugin,
`ghcr.io/lukeevanstech/logstash-sentinel`) crashlooped after three consecutive Renovate digest
bumps ([#4343](https://github.com/LukeEvansTech/talos-cluster/pull/4343),
[#4351](https://github.com/LukeEvansTech/talos-cluster/pull/4351),
[#4432](https://github.com/LukeEvansTech/talos-cluster/pull/4432), merged 2026-08-17 to
2026-08-20). Each bump upgraded an already-running release, so Flux's upgrade remediation
(`spec.upgrade.remediation`: `retries: 3`, `strategy: rollback`) rolled each one back to the last
working digest, and the HelmRelease quietly kept running a three-week-old image and
only the `FluxHelmReleaseNotReady` alert, firing from 2026-08-20 22:48, surfaced the problem.

## Cause

The upstream image build had moved its Logstash base onto the JRuby 10 (Ruby 3.4) runtime while
still publishing under the old, now-inaccurate version tag. The pinned Sentinel output plugin
(2.1.0) **installed fine at build time but failed to load at pipeline creation** under the new
runtime (`Couldn't find any output plugin named
microsoft-sentinel-log-analytics-logstash-output-plugin`), so Logstash exited immediately, the pod
crashlooped, and the Helm upgrade timed out.

## Fix

Image side (`LukeEvansTech/containers#54`): bump the plugin pin from 2.1.0 to 2.5.0, publish under
the correct version tag, and add Renovate custom managers so the base runtime version and the gem
pin are tracked as two separate values instead of one combined tag.

Cluster side ([#4450](https://github.com/LukeEvansTech/talos-cluster/pull/4450)): point the
HelmRelease at the fixed image, verified in-cluster before merge (plugin listed, plugin starts,
pipeline starts).

## Lessons (generic)

- **"Installs but won't load" is a different failure from "not installed."** It only shows up once
  the plugin tries to initialise a pipeline, so a build-time check that the gem installed is not
  enough to catch a runtime mismatch.
- **A custom image's version tag can drift from its actual base runtime.** If the tag names a
  plugin or app version but not the language runtime underneath it, a base rebuild can change that
  runtime without the tag changing to reflect it.
- **Automatic rollback remediation can mask a rolling failure.** Each of three digest bumps landed
  on an image that was still broken and got silently rolled back, so nothing paged until the
  HelmRelease had been stuck for days. Watch for a Kustomization or HelmRelease stuck on an old
  digest across several unrelated bumps, not just for the bump PR that introduced the break.

## References

- `kubernetes/apps/security/sentinel-syslog/app/helmrelease.yaml`, the image reference this
  incident touched.
- [LukeEvansTech/talos-cluster#4450](https://github.com/LukeEvansTech/talos-cluster/pull/4450),
  the fix.
