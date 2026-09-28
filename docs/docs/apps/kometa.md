# Kometa

Kometa (`kubernetes/apps/media/kometa`) runs as a nightly CronJob in the `media` namespace,
building Plex collections, overlays and review playlists for Movies and TV.

## The 02:00 schedule

MDBList's free tier resets its 1,000-request daily quota at midnight UTC. Kometa 2.5 honours
`Retry-After` on an HTTP 429 with no cap, so a run that starts before the reset, while the
previous day's requests are still spent, sleeps until the reset instead of failing. `@daily` in
Europe/London runs at 23:00 UTC in summer, an hour before the reset, so every run used to start
already out of quota.

The fix, in #5344, moved the schedule to `0 2 * * *`. 02:00 local is 01:00 UTC in summer and
02:00 UTC in winter, always after the reset. 02:00 also survives the UK's spring DST transition:
clocks jump from 01:00 to 02:00 GMT that night, so a 01:00 local schedule would be skipped.

## The 20-hour run deadline

On 2026-09-23, a run stalled in OMDb and MDBList rate-limit sleeps and was still running three
days later, silently skipping the 24th, 25th and 26th under `concurrencyPolicy: Forbid` (#5429).
`activeDeadlineSeconds: 72000` now kills any run still going 20 hours after it starts, well
past the roughly 4-hour length of a normal run, and before the next 02:00 schedule.
