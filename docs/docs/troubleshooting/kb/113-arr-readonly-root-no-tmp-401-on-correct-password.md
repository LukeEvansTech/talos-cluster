# KB-113: `*arr` app returns 401 on the correct password (no writable /tmp)

**Status:** App-template gotcha. Fix is one `persistence` entry.

## Symptom

A browser login to an `*arr` app (Radarr, Sonarr, Prowlarr, ...) returns `HTTP 401` and issues no
session cookie, even with the correct password. A wrong password still gets the normal
`302 → /login?loginFailed=true` redirect. The app logs an encryption error, not a credential
failure.

## Cause

The app runs `readOnlyRootFilesystem: true` with no writable `/tmp`. ASP.NET Core's data-protection
layer stages a new key-ring entry through `Path.GetTempFileName()`, which needs `/tmp` to be
writable:

```text
System.IO.IOException: Read-only file system : '/tmp/'
```

Once the app's existing keys expire, it can create no replacement, so `CookieAuthenticationHandler`
cannot encrypt the auth ticket. Forms login then fails even though the credential is correct.
API-key clients (an exportarr sidecar, another `*arr` app syncing indexers) never take this code
path, so the break stays invisible until someone opens the browser UI.

## Fix

Add a writable `/tmp` in the app-template `persistence` block:

```yaml
persistence:
  tmp:
    type: emptyDir
```

## How to recognise fast

- Correct password gives `401`; wrong password (the control) gives the normal `302` redirect. The
  control matters: it shows the failure is in issuing the cookie, not in checking the credential.
- Radarr hit this because it was the only `*arr` app here with `readOnlyRootFilesystem` and no
  `tmp` emptyDir; its newest on-disk key had expired 2025-11-10. Sonarr and Prowlarr already carried
  the mount.

## References

- `kubernetes/apps/media/radarr/app/helmrelease.yaml`, `persistence.tmp`
- #4814 (the fix), #4816 (the auth rollout it unblocked)
