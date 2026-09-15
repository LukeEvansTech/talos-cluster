# KB-033: OPNsense Record Exists but Is Unowned (No Registry TXT Row)

**Status:** Reference. This is external-dns's TXT registry working as designed, in place since
`opnsense-dns` moved to `policy: sync` with `registry: txt` in
[#5168](https://github.com/LukeEvansTech/talos-cluster/pull/5168). The fix is a decision about who
owns the row, then one manual step.

## Symptom

An app's `HTTPRoute` is attached to `envoy-internal`, its HelmRelease is healthy, and every other
new app gets its record within a minute, but this one hostname either does not resolve on the LAN
or resolves to something other than `${ENVOY_INTERNAL_IP}`. Nothing alerts, and at the default log
level the external-dns container says nothing about it either: the skip is silent.

In Services → Unbound DNS → Overrides → Host Overrides the name is present as an ordinary A row,
and there is **no** row named `k8s.main.a-<name>.${SECRET_DOMAIN}` beside it. That missing TXT row
is the tell.

To see the controller's reasoning, raise the log level (`logLevel: debug` on the HelmRelease, or
temporarily on the Deployment) and it logs, once per reconcile:

```text
Skipping endpoint <name>.${SECRET_DOMAIN} 0 IN A  ${ENVOY_INTERNAL_IP} [] because owner id does not match (found: "", required: "main")
```

`found: ""` means no owner at all, as opposed to another external-dns instance's owner ID.

## Cause

`opnsense-dns` only touches rows it owns, and ownership is a TXT row in the same table named
`k8s.main.<type>-<name>` (`txtOwnerId: main`, `txtPrefix: k8s.main.%{record_type}-`). A host
override with no such row is unowned, so the registry reports an empty owner and external-dns
refuses to update or delete it. That is the safety property that keeps hand-made rows and the
device rows managed from `network-ops` out of the controller's reach, and it applies equally to:

- a row someone typed into the OPNsense UI, or created from `network-ops`, for the same name;
- a row left over from before the cutover, when the controller ran `upsert-only` with no registry
  and therefore never wrote ownership rows. The cutover deleted those from a reviewed allowlist, so
  one that survives is a row the allowlist missed.

The skip is per name. Nothing else is blocked, and none of the `opnsense-dns` alert rules fire,
because from the controller's point of view nothing failed.

## Fix

Decide who should own the name. There are only two answers.

**Hand it to external-dns.** Delete the unowned row (Services → Unbound DNS → Overrides → Host
Overrides, then Apply) and wait one reconcile. The controller creates the A row and its registry
row together. Check the result rather than the log:

```bash
dig +short <name>.${SECRET_DOMAIN} @<resolver>    # expect ${ENVOY_INTERNAL_IP}
```

and confirm the `k8s.main.a-<name>` TXT row now sits beside the A row in the Host Overrides table.

**Keep it hand-made.** Remove the hostname from the app's `HTTPRoute` (or move the route to the
other gateway). The declared endpoint disappears and the skip stops. Do not leave both in place:
the app's URL keeps pointing at whatever the hand-made row says, and nothing will ever tell you.

Never resolve it by typing the registry TXT row in by hand so the controller adopts the row. It
works, but it hides which system wrote the A row, and the next `policy: sync` decision (the
hostname leaving Git) deletes it.

## References

- [Split DNS: OPNsense host overrides, ownership and deletion](../../architecture/split-dns.md#opnsense-host-overrides-ownership-and-deletion)
- Related: [KB-034](034-opnsense-delete-blocked-by-alias.md), the other way a row can outlive the
  reconcile that should have removed it.
