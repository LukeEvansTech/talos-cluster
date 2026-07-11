# Routing guide

## Principles

- Prefer the most specific project that clearly fits. If nothing fits with
  confidence, give LOW confidence (below 0.8) so the harness flags it for
  review — never apply the `needs-review` label yourself; flagging is the
  harness's job.
- Never invent projects or labels. Only use ones in the catalog.

## Priority rules

- A stated date or hard deadline in the text ("by Friday", "before the 25th",
  "for Monday") → P2. Escalate to P1 only when it is within 48h and missing it
  has real consequences, or when someone is blocked on it.
- Routine errands and chores → P3.
- Someday / reading / research ideas → P4.
- Default P3 when unsure — never default to P1.

## Project hints

- **Work** — clients (PDS, Scendea), invoices, status reports, standups, PRs.
- **Home** — house and garden repairs, bills, insurance, the car (servicing,
  tax).
- **Homelab** — the cluster, servers, networking, self-hosted services
  (Talos, Plex, etc.).
- **Shopping** — buying / ordering / restocking anything: groceries, pet
  supplies, gear.
- **Selling** — listing or selling things (eBay, marketplace).
- **Hobbies** — 3D printing, keyboards, games, making things.
- **Someday** — vague "maybe / some day" ideas, "look into…", "learn…".

## Examples

- "email Sarah the Q3 deck by Fri" → Work, deadline_date: this Friday, P2
- "buy milk tomorrow" → Shopping, due_date: tomorrow, P3
- "look into standing desks" → Someday, P4
