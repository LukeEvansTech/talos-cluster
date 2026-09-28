# Pocket ID

Pocket ID is the cluster's OIDC identity provider (`kubernetes/apps/security/pocket-id`). Other
apps authenticate against it as OIDC clients.

## Email verification

Until #3990, the manifest set `EMAILS_VERIFIED: "true"` unconditionally. Pocket ID writes that flag
whenever a user signs up or edits their own email address, and stamps it straight into the
`email_verified` OIDC claim with no proof the user controls the address. `AllowOwnAccountEdit`
defaults to true and this cluster does not override it, so any signed-in user could type any
address into their own profile and have Pocket ID assert it as verified to every relying party.

The fix dropped `EMAILS_VERIFIED` and set `EMAIL_VERIFICATION_ENABLED: "true"` instead, a separate
setting that also defaults to false. It gates the real "verify your email" flow: Pocket ID sends a
confirmation link and only claims `email_verified: true` once the user completes it. Both settings
matter independently. Enabling verification without dropping the old flag would have left the old
flag in place, and dropping the old flag without enabling verification would leave every new or
changed address permanently unverified with no prompt to fix it. The change was not a lockout
risk: `EmailVerified` only feeds the OIDC claim and was never used to gate login, and since the
flag is written only at signup or on an email change, existing rows were untouched.

## GeoLite2 cache

`GEOLITE_DB_PATH` points at `/app/geolite/GeoLite2-City.mmdb`, an `emptyDir` capped at 256Mi,
rather than the default path under `/app/data` on the VolSync-backed PVC. Pocket ID downloads the
GeoLite2-City database on start if it is missing and refreshes it every 14 days, writing a temp
file and renaming it into place in the same directory. The database is a rebuildable copy of a
public MaxMind artifact rather than state, so it follows the same rule as this cluster's other
rebuildable caches (tracearr's poster cache, #4561): keep it off the backed-up PVC.

## References

- `kubernetes/apps/security/pocket-id/app/helmrelease.yaml`, the email verification and GeoLite2
  settings.
- [#3990](https://github.com/LukeEvansTech/talos-cluster/pull/3990), the email verification fix.
- [#4562](https://github.com/LukeEvansTech/talos-cluster/pull/4562), enabling GeoLite2 geolocation.
