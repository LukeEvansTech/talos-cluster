# Kometa

Kometa (`kubernetes/apps/media/kometa`) runs as a nightly CronJob in the `media` namespace,
building Plex collections and overlays for Movies and TV. Titles it adds through Radarr and Sonarr
carry the `kometa` tag there (plus `kometa-top250`, `kometa-bestpicture` or `kometa-trending`);
review them with each app's tag filter.

## What Kometa adds to Radarr and Sonarr

Global `add_missing` is off in `config.yml`. Three lists add missing titles, each through its own
entry so the additions can be filtered and tagged:

| List                 | Adds through                         | Per-source tag       | Filters                                   |
| -------------------- | ------------------------------------ | -------------------- | ----------------------------------------- |
| IMDb Top 250 films   | `resources/top250-add.yml`           | `kometa-top250`      | Original language                         |
| IMDb Top 250 series  | `resources/top250-add-tv.yml`        | `kometa-top250`      | Original language, TMDb type and genre    |
| TMDb Trending films  | `resources/trending-add.yml`         | `kometa-trending`    | Original language, rating floor           |
| TMDb Trending series | `resources/trending-add-tv.yml`      | `kometa-trending`    | Original language, type, genre and rating |
| Oscar Best Picture   | `default: oscars` template variables | `kometa-bestpicture` | None                                      |

The four feed files are collection files with `build_collection: false`: they build nothing in
Plex and exist only to run the add. The `default: imdb` and `default: tmdb` entries beside them
build the Plex collections and add nothing. The split is needed because a chart default takes its
add settings as template variables but has no way to take `filters`, while Kometa applies a feed's
TMDb filters to each missing title before it reaches Radarr or Sonarr.

Every feed drops titles whose TMDb original language is Hindi, Tamil, Telugu, Malayalam, Kannada,
Bengali, Marathi, Punjabi, Gujarati or Urdu. The two series feeds also drop the TMDb types `news`,
`talk_show`, `reality` and `video` and the genres Soap, News and Talk. Documentaries pass. The
trending series feed adds the pilot only; the Top 250 series feed monitors every episode.

The IMDb Top 250 add ran through `default: imdb` until 2026-11-21. Unfiltered, it added every
Hindi-language series on the chart along with reality shows, none of them wanted.

The trending film floor is a TMDb score of 7.0 on at least 500 votes. It was 6.5 on 200 votes
until 2026-11-21, which admitted four films that IMDb rates between 5.3 and 5.9. TMDb scores for
new releases run well above IMDb's: two of those four now hold a TMDb score above 7.0 on more than
500 votes, so the floor thins the feed without guaranteeing quality.

A filter only stops a new addition. A title already in Radarr or Sonarr stays until it is removed
there.

## Failed runs

Kometa exits 0 even when collections or playlists fail, and reports them only as `Kometa Failure`
rows in its run log. The CronJob greps `/config/logs/meta.log` for that string and exits 1, so a
failure surfaces as `KubeJobFailed` rather than a green Job. The review playlists failed on every
run for days unnoticed this way: Kometa rejects `radarr_taglist` and `sonarr_taglist` inside
playlists. For any other doubt, read the `Error Summary` block at the end of the Job's log.

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
