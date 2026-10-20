# Sonarr

## Settings that live only in the app

Notification connections are stored in Sonarr's database, not in Git. `chaski Download` (id 4)
fires on **Import Complete** only, so a season pack sends one push rather than one per episode;
Sonarr (v4 and v5) sends that event with `eventType: "Download"`. It was switched from On Import and On
Upgrade on 2026-09-29, after a bulk add of 175 series sent 1,689 pushes in a day and spent the
Pushover account's monthly quota.

Read or change connections through the API from inside the pod, where the key and port are
environment variables. The response includes each webhook URL, so project it down to names and
triggers before it reaches a terminal:

```bash
kubectl -n media exec deploy/sonarr -c app -- sh -c \
  'curl -s -H "X-Api-Key: $SONARR__AUTH__APIKEY" "http://localhost:$SONARR__SERVER__PORT/api/v3/notification"' \
  | jq -r '.[] | "\(.id) \(.name) " + ([to_entries[] | select((.key | startswith("on")) and .value == true) | .key] | join(","))'
```

`RADARR__*` works the same way in Radarr.

## v5 upgrade

Moving from v4 to v5 migrates `sonarr.db` one way (schema 217 to 236), so reverting the image tag
does not bring v4 back; a VolSync restore of the config volume taken before the upgrade does. The
first v5 start runs those migrations before `/ping` answers, which is why the HelmRelease carries
a startup probe with a 30-minute budget.

v5 rewrites `AuthenticationMethod` values `None` and `Basic` to `Forms` and, when the database has
no user, creates one with a password written once to the log. The HelmRelease pins
`SONARR__AUTH__METHOD: Forms`, and the ExternalSecret supplies `SONARR__AUTH__INITIALUSERNAME` and
`SONARR__AUTH__INITIALPASSWORD` from the `sonarr` item's login fields, which v5 reads only when no
user exists.
