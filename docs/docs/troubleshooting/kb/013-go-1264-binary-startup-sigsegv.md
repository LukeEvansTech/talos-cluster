# KB-013: Go 1.26.4 Binary SIGSEGV at Startup (Before Any Logging)

**Status:** Not fixable in-cluster (upstream Go regression); left as-is, low impact. Documented
for recognition and for the repro technique.

## Symptom

A pure-Go pod (here `chaski`, a webhook relay) **SIGSEGVs at startup** (`exit 139`) on a
fraction of fresh pod starts (~60–70%), recovering in ~1s on the kubelet restart. With 2
replicas this causes **no downtime**, but a single restart trips a `KubePodCrashLooping`-style
page on every rollout or restart — which can look like "the app is failing" when the cluster is
otherwise green.

## Cause

The crash is a **Go runtime-bootstrap regression**, not an application bug. It happens
*before* any application code runs:

- `GODEBUG=inittrace=1` prints **nothing** → the crash is before package `init()`.
- `GOTRACEBACK=crash` prints **nothing** → before the runtime's signal handler installs (hence
  a raw `139` with **no obtainable stack** — the runtime dies before it can produce one).
- The binary was built with **Go 1.26.4**, pure-Go, no CGO. cf. golang/go#78822 (Go 1.26.x
  linux/amd64 SIGSEGV regressions).

Ruled out via a throwaway-probe Deployment (one variable at a time): `GOMAXPROCS` 24→2, memory
limit 128Mi→1Gi, seccomp `RuntimeDefault`→`Unconfined` — none move the rate; not node-specific;
identical on multiple app versions. Circumstantial proof it's the toolchain: hundreds of
older-Go binaries start fine on the same nodes, and this is one of the first Go-1.26.4 binaries
deployed.

## Fix

No config knob fixes it and no older release of the affected app dodges it (all built on the
same Go era). Options, in order of preference:

1. **Leave it** if replicas ≥ 2 and recovery is sub-second — impact is a noisy alert, not an
   outage. (This is what was chosen.)
2. Rebuild the binary on a **different Go toolchain** once a fixed release exists.
3. Silence the rollout-time crash-loop alert for the specific workload if it's too noisy.

## Repro technique (reuse for any "crashes before logging" Go bug)

1. Copy the live Deployment to a throwaway one: `replicas: 10`, **distinct labels** so the
   Service doesn't select it, strip the probes.
2. Add `GODEBUG=inittrace=1` + `GOTRACEBACK=crash`.
3. Roll it and count `restartCount > 0`.
4. `inittrace`-silent = the crash precedes `init()`. A/B single env/resource variables across
   rolls to localise.

If other Go-1.26 binaries start crash-looping the same way, that confirms it's toolchain-wide.

## References

- golang/go#78822 — Go 1.26.x linux/amd64 startup SIGSEGV reports.
- `GODEBUG` runtime knobs: <https://pkg.go.dev/runtime#hdr-Environment_Variables>
