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
setting that also defaults to false. Pocket ID v2.19.0 renamed it to `EMAIL_VERIFICATION`
(`disabled`, `optional` or `required`); the manifest now sets `optional`, the same behaviour, since the
old name is only honoured by a shim due to go in v3. It gates the real "verify your email" flow: Pocket ID sends a
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

## Putting an app behind Pocket ID

The `pocket-id` component (`kubernetes/components/pocket-id`) signs people in at the gateway, so
the app itself needs no login code. It adds an Envoy Gateway `SecurityPolicy` with OIDC on the
HTTPRoute named `${APP}`: a browser with no session is sent to `https://id.${SECRET_DOMAIN}`, comes
back to `/oauth2/callback`, and gets a cookie for that app's host. `/oauth2/logout` clears it. The
Pocket ID session itself lasts 30 days (`SESSION_DURATION`), so after the first sign-in each further
app costs one redirect and no prompt.

To put an app behind it:

1. Create a Pocket ID OIDC client whose ID is the app name, with the callback URL
   `https://<app>.${SECRET_DOMAIN}/oauth2/callback`, PKCE on, consent skipped and access restricted
   to one access tier (below). Create a client secret and store it as `POCKET_ID_CLIENT_SECRET` in
   a 1Password item of its own, `<app>-pocket-id` (`Talos` vault).
2. Add `../../../../components/pocket-id` to `components` in the app's `ks.yaml`.
3. Keep the route that serves the browser named `${APP}`. A HelmRelease with more than one route
   names them `<app>-<key>`, so set `forceRename: "{{ .Release.Name }}"` on `route.app`.
4. Give any path that must work without a browser session its own route on the same host,
   with `PathPrefix` rules: API clients with their own keys, webhooks, images a mail client
   loads. The policy only targets `${APP}`, and the gateway sends each request to the longest
   matching prefix across both routes.
5. The component sets the route's Gatus check to expect the 302 to Pocket ID, which proves the
   gateway and sign-in path but not the app. To check the app too, annotate its Service with
   `gatus.home-operations.com/enabled: "true"` and an endpoint named `<app>-service` (the sidecar
   names a Service's endpoint after it, which would collide with the route's).

The policy covers traffic through the gateway only. A pod that can reach the Service skips it, so
this relies on the namespace's network policies admitting ingress from `network` and the
namespace itself, as `custom` does.

## Access tiers

Pocket ID groups describe what a member is trusted with, not which app they use, and every OIDC
client is restricted to exactly one of them. No client is open to every user, so a new account
reaches nothing until it joins a group. Pocket ID has no nested groups, so the owner is a member of
all three.

| Group | Trusted with | Notes |
| --- | --- | --- |
| `infra` | Anything that can reach or change the estate | Clients require re-authentication |
| `personal` | The owner's private data | |
| `household` | What other household members share | |

If an app maps groups onto its own roles, those groups are named after the app
(`paperless-admins`), not added as tiers.

Apps whose phone or TV clients call the API directly cannot sit behind the gateway redirect; they
keep their own OIDC login, restricted to a tier in the same way.

## One login per app

A person signs in once per app. Which sign-in that is depends on the app:

- **No login of its own** (kopia, cupdate): the gateway component is the sign-in.
- **Its own login, with OIDC support** (termix, filebrowser): the app signs in against Pocket ID
  itself and the gateway component is left off. The client keeps the app's name and tier, with
  the app's callback (termix `/plugin-api/sso/callback`, filebrowser `/api/auth/oidc/callback`),
  and the app reads the secret from the same `<app>-pocket-id` item through its own
  ExternalSecret. Filebrowser sends no PKCE challenge, so its client has PKCE off; change that
  before the cutover or Pocket ID rejects every sign-in. Filebrowser creates a new user on the
  first OIDC sign-in and refuses one whose username matches an existing password user, so check
  for a clash first. termix links an existing account to SSO instead, so it keeps the gateway and
  its password login until the owner has linked theirs, then loses both in one change.
- **Its own login that can be switched off** (backrest): switch it off and keep the gateway.
  Backrest's config is seeded with auth disabled on each start.
- **Its own login that can neither use Pocket ID nor be switched off** (certwarden): keep both
  until the app can. CertWarden's OIDC requires a custom scope that Pocket ID grants only to
  requests carrying an RFC 8707 `resource` parameter, which CertWarden does not send.

## References

- `kubernetes/apps/security/pocket-id/app/helmrelease.yaml`, the email verification and GeoLite2
  settings.
- [#3990](https://github.com/LukeEvansTech/talos-cluster/pull/3990), the email verification fix.
- [#4562](https://github.com/LukeEvansTech/talos-cluster/pull/4562), enabling GeoLite2 geolocation.
- `kubernetes/components/pocket-id`, the gateway sign-in component; `sakuga` is its first consumer.
