# KB-013: Go pod startup SIGSEGV was a UPX stub vs. service-link env vars, not a Go regression

**Status:** Resolved upstream 2026-08-27. The "unfixable upstream Go runtime regression"
theory below was wrong on two counts: it isn't a Go bug, and it **is** fixable. The local
`enableServiceLinks: false` workaround held from 2026-08-25 until upstream dropped the UPX step
(`home-operations/chaski#137` → `#138`, shipped in chart/app 0.5.1); the workaround has since
been removed. Kept for the diagnostic method and the proof numbers.

## Symptom

A pod running a UPX-packed Go binary (here `chaski`, a webhook relay) **SIGSEGVs at startup**
(`exit 139`) on a fraction of fresh pod starts, recovering in ~1s on the kubelet restart. With
2 replicas this causes **no downtime**, but a single restart trips a `KubePodCrashLooping`-style
page on every rollout or restart, which can look like "the app is failing" when the cluster is
otherwise green.

## Cause

The crash is real and genuinely happens before Go's own signal handler installs, but not for
the reason first assumed.

**Original (wrong) theory:** `GODEBUG=inittrace=1` and `GOTRACEBACK=crash` both printed nothing,
which correctly places the crash before package `init()` and before the runtime's signal
handler, but was then read as "Go 1.26.4 runtime-bootstrap regression," on the weak evidence
that this was one of the first Go-1.26.4 binaries deployed. No config knob was found to fix it,
so it was left as-is and documented as unfixable.

**What actually happened**, established the same day by two PRs 20 minutes apart:

- `#4589`: the shipped chaski image is UPX-packed, and the crash is inside the UPX decompressor
  stub, not Go code, and not version-specific (it reproduces on Go 1.27.0 too, ruling out a
  Go-1.26.4-specific regression). First attempted fix, `GODEBUG=asyncpreemptoff=1`, was based
  on an async-preemption theory and did not hold in production (7 of 12 pods still crashed
  after merge).
- `#4591`: controlled A/B testing on a production-config node pinned the actual trigger. The
  UPX stub segfaults once the process environment crosses roughly 500 variables (a **count**
  threshold, not a size one: 460 short vars was 0/8 crashes, 1000 short vars was 8/8). Kubernetes'
  automatic `*_SERVICE_HOST`/`*_PORT` service-link injection put chaski at ~460-518 env vars,
  right on the threshold, which is why only a fraction of starts crashed (stack-layout
  randomisation tips individual starts over the line or not). Proven on the production config:
  20/20 crashes with the links present, 0/20 with them stripped, 0/40 with the same binary
  UPX-unpacked.

So the "before any logging, no obtainable stack" signal was accurate; the inference from it
("must be the Go toolchain") was not. It's the UPX stub, tipped over by namespace-driven
environment size, and it recurred for the wrong reason worked out on the first attempted fix too.

## Fix

**Upstream (final):** the UPX step was removed from the image build after the findings below
were reported (`home-operations/chaski#137`, fixed by `#138`), first shipping in **0.5.1**. The
binary went from ~7 MB packed to ~27 MB plain, and starts cleanly at 460 / 600 / 1000 / 2000
environment variables where the packed one died above ~500. Compression was the thing that made
any app in a large namespace fragile to environment size at all, so removing it is the real fix.

**Local (stopgap, now removed):** `enableServiceLinks: false` via a `postRenderers` patch on the
Deployment (the chart has no values knob for it), which dropped the Kubernetes-injected
service-link variables and kept the pod's environment under the stub's threshold. It held from
2026-08-25 until the 0.5.1 bump; nothing in chaski reads service-link env vars, so there was no
functional loss either way. Reach for the same patch on any other pod that shows this shape.

## Lessons

- Silence under `GODEBUG=inittrace=1` / `GOTRACEBACK=crash` proves the crash is early and
  pre-signal-handler; it says nothing about *why*. That's equally consistent with "Go runtime
  bug" and "something non-Go running before Go's signal handler installs" (here: a UPX
  decompressor stub).
- "One of the first binaries built on a new toolchain version" is a plausible-looking but weak
  signal for "must be a toolchain regression". It's equally consistent with "one of the first
  binaries built with some other new property" (here: UPX packing, in a namespace with enough
  Services to push env-var count over a threshold).
- Confirming a root cause can take more than one controlled test: `#4589` correctly moved the
  suspect from "Go regression" to "UPX stub" but the specific mechanism it proposed didn't hold
  in production; `#4591`'s A/B env-var-count experiment is what actually pinned it down. Prefer a
  repeatable crash-count experiment over a plausible-sounding theory before calling a root cause
  confirmed.
- For any unexplained pre-application-code crash in a pod, `enableServiceLinks: false` is worth
  trying early when the namespace has many Services. The default service-link injection scales
  with namespace size, not with anything about the app.

## References

- `home-operations/chaski#137`, the upstream report, and `#138`, the UPX removal shipped in 0.5.1
- `kubernetes/apps/default/chaski/app/helmrelease.yaml`, where the stopgap patch lived
  numbers.
- `#3276` tracks dropping the patch once the upstream image stops UPX-packing.
