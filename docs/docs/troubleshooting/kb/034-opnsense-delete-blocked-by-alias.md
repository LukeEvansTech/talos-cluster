# KB-034: OPNsense Delete Blocked by a Hand-Made Alias

**Status:** Reference. The refusal is deliberate behaviour of
`LukeEvansTech/external-dns-opnsense-webhook`, and `OPNsenseDeleteBlocked` (added in
[#5168](https://github.com/LukeEvansTech/talos-cluster/pull/5168)) exists to surface it. The fix is
one manual change to the alias; the controller finishes the job on its next reconcile.

## Symptom

`OPNsenseDeleteBlocked` (warning) fires. The webhook sidecar in `opnsense-dns` logs the refusal on
every reconcile, naming the row's UUID:

```text
delete A <name>.${SECRET_DOMAIN} (<uuid>): opnsense: row has alias children; refusing to delete
```

and the external-dns container reports the same apply failure each cycle, an HTTP 500 from the
webhook. Because the whole apply fails, **every other pending change in that reconcile waits
behind it**, so a new app can also appear to get no record while this is outstanding; the log line
above says which row is the blocker.

The metric behind the alert is `externaldns_webhook_opnsense_delete_blocked_total`, which only
exists after the first refusal. The rule has a second branch precisely so that first sample alerts
(a bare `increase()` over a series with no earlier value reads zero).

## Cause

A hostname was removed from Git (or moved to the other gateway), so under `policy: sync`
external-dns planned the deletion of its A row and registry row. In OPNsense a host override can
carry **aliases** (the Aliases grid on the same Services → Unbound DNS → Overrides page, each alias
tied to a parent host row), and deleting the parent row cascades to them. Someone had added an
alias by hand to a controller-owned row, so completing the delete would have silently destroyed a
name the controller never knew about.

The provider therefore refuses any delete whose row still has enabled alias children, increments
the counter, and returns the error. It does this every reconcile until the alias is gone. It never
deletes the alias for you, and it never abandons the delete either.

## Fix

Decide where the alias belongs, then make one change in OPNsense:

- **The alias should survive the app.** Re-home it: create a host override of its own for the
  alias name (or move it under a hand-made parent row), then delete the alias from the
  controller-owned row and Apply.
- **The alias was only ever for that app.** Delete the alias from the row and Apply.

On the next reconcile the delete completes, the A row and its `k8s.main.a-<name>` TXT row go, and
the alert clears once the counter stops increasing (the rule looks back one hour). Confirm in the
Host Overrides table rather than from the alert state:

```bash
kubectl logs -n network deployment/opnsense-dns -c webhook | grep '<name>'
dig +short <name>.${SECRET_DOMAIN} @<resolver>    # expect no answer
```

Deleting the parent row by hand in the UI also clears the alert, but it takes the aliases with it,
which is exactly the loss the refusal exists to prevent. Edit the alias, not the row.

## References

- [Split DNS: OPNsense host overrides, ownership and deletion](../../architecture/split-dns.md#opnsense-host-overrides-ownership-and-deletion)
- Related: [KB-033](033-opnsense-record-exists-but-is-unowned.md), the unowned-row case.
