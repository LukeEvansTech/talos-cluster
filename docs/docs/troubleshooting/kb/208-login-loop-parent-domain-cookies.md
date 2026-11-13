# KB-208: Login loops in one browser profile while the app logs a successful login

**Status:** Browser-side. Nothing in the cluster changes. The fix is deleting cookies on the
parent domain.

## Symptom

Submitting the correct credentials to an app's web login returns to the login page with no
error. The app's own log records a successful login for every attempt, often several within a
few seconds. The same login works in a private window and from `curl` through the same route.

## Cause

Cookies set on the parent domain `.${SECRET_DOMAIN}` are sent to every app under it. Analytics
cookies left there by an unrelated site arrive in the same `Cookie` header as the app's session
cookie. An app whose cookie parser stops at a value it cannot read then never sees its session
cookie, so it treats the next request as logged out. SABnzbd is the case seen here; it parses
cookies with CherryPy and Python's `http.cookies`. Its session cookie is also bound to the client
address from `X-Forwarded-For`, which stays stable through the Envoy gateway, so the proxy is not
the cause.

Clearing the app's own site data leaves the parent-domain cookies in place, which is why that
does not help. A private window starts without them, which is why it works.

## Fix

In the browser's site-data settings, open the entry for the bare parent domain rather than the
app's subdomain, delete its cookies, then log in again.

## How to recognise fast

- The app logs a successful login on each attempt, so the credentials are correct.
- A `curl` login through the public route, keeping cookies in a jar, reaches the app on the next
  request. That rules out the server and the gateway.
- A private window works and the normal profile does not, so the cause is stored in the profile
  as cookies, a per-site rule or an extension.
- The profile holds cookies whose host is the parent domain itself.

## References

- [KB-113](113-arr-readonly-root-no-tmp-401-on-correct-password.md) is the server-side neighbour:
  the app returns `401` on the correct password because it cannot issue a session cookie.
