# KB-059: A hung par2/unrar/7z helper stalls the entire SABnzbd queue

**Status:** Resolved (#5466).

## Symptom

SABnzbd's post-processing queue stops advancing. Downloads keep completing and queuing for
post-processing, but nothing finishes it. On 2026-09-26 a par2 repair spun on one CPU core for
about 30 hours without reading a byte, while roughly 3,800 downloaded jobs waited behind it.

## Cause

SABnzbd post-processes one job at a time and puts no timeout on the external helpers (par2,
unrar, 7z) it shells out to. A single helper that hangs, rather than crashing or exiting, stalls
every job queued behind it indefinitely.

SABnzbd's own `mode=cancel_pp` API cannot be used to recover from this: `PostProcessor.cancel_pp`
(checked against 5.1.3 and `develop`) returns after checking only the first job in its list, so it
misses the active job whenever another job is ahead of it in the queue.

No existing tool in this repository covered the gap. decluttarr (already deployed) deliberately
skips completed downloads. Cleanuparr is torrent-only.

## Fix

`resources/pp-watchdog.sh`, run in the background inside the SABnzbd app container (`catatonit --
sh -c "/pp-watchdog/pp-watchdog.sh & exec /entrypoint.sh"`), watches `rchar + wchar` from
`/proc/<pid>/io` for every `par2`/`unrar`/`7z` process once a minute. A helper whose counters
haven't moved for 30 minutes gets `SIGTERM`; one that survives 5 more minutes gets `SIGKILL`.
SABnzbd then fails that one job, the arr blocklists the release and searches again, and
post-processing moves on to the next job.

A helper blocked _reading_ a pipe is left alone: that's Direct Unpack's `unrar` waiting for
SABnzbd to hand it the next volume, not a hang. `unrar` blocked _writing_ a pipe is the
[sabnzbd/sabnzbd#3638](https://github.com/sabnzbd/sabnzbd/issues/3638) hang, and is killed.

## Why a background process, not a sidecar

A sidecar container would need `shareProcessNamespace` to see the helper processes at all. That
setting also lets every other container in the pod see this one's environment, including
`gluetun-webui`, which runs as the same uid. `gluetun-webui` would then be able to read SABnzbd's
API key and the gluetun sidecar's WireGuard key. Running the watchdog as a background process
inside the SABnzbd container itself avoids sharing the pod's process namespace at all.

## References

- Fix: #5466.
- [sabnzbd/sabnzbd#3638](https://github.com/sabnzbd/sabnzbd/issues/3638): the pipe-write hang the
  watchdog's read/write pipe distinction exists for.
