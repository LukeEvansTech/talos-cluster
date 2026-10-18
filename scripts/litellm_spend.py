"""Summarise LiteLLM traffic per API key and model group from the proxy's spend logs.

Runs INSIDE the litellm pod (it reads LITELLM_MASTER_KEY from the pod env and calls
localhost), so nothing secret leaves the cluster. Run: just kube litellm-spend [days]
"""

import collections
import datetime as dt
import json
import math
import os
import sys
import urllib.parse
import urllib.request

BASE = "http://localhost:4000"


def get(path):
    """GET a proxy admin endpoint with the master key."""
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def fetch_rows(days):
    """Page through /spend/logs/ui; the plain /spend/logs aggregates by day when given dates."""
    end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    start = end - dt.timedelta(days=days + 1)
    rows, page = [], 1
    while True:
        query = urllib.parse.urlencode(
            {
                "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
                "page": page,
                "page_size": 100,
            }
        )
        data = get("/spend/logs/ui?" + query)
        rows += data["data"]
        # total_pages stops at the 10,000-row count cap, so keep paging while capped.
        if not data["data"] or (page >= data.get("total_pages", 1) and not data.get("total_is_capped")):
            return rows
        page += 1


def seconds(row):
    """Request duration in seconds, or None when a timestamp is missing."""
    try:
        start = dt.datetime.fromisoformat(row["startTime"].replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(row["endTime"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return (end - start).total_seconds()


def pct(values, q):
    """Nearest-rank percentile; 0 for an empty list."""
    values = sorted(values)
    return values[max(0, math.ceil(q * len(values)) - 1)] if values else 0


def main():
    """Print the per-key table and the provider budgets."""
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    rows = fetch_rows(days)
    groups = collections.defaultdict(lambda: {"n": 0, "fail": 0, "spend": 0.0, "lat": [], "in": [], "out": []})
    for row in rows:
        meta = row.get("metadata") or {}
        key = (meta.get("user_api_key_alias") or "(no alias)", row.get("model_group") or "-")
        agg = groups[key]
        agg["n"] += 1
        # Rows logged before the status field existed carry null, which LiteLLM counts as success.
        agg["fail"] += row.get("status") not in ("success", None)
        agg["spend"] += row.get("spend") or 0.0
        lat = seconds(row)
        if lat is not None:
            agg["lat"].append(lat)
        agg["in"].append(row.get("prompt_tokens") or 0)
        agg["out"].append(row.get("completion_tokens") or 0)

    print(f"{len(rows)} requests over the last {days} days\n")
    header = (
        f"{'key':20} {'model group':18} {'reqs':>5} {'fail':>4} {'USD':>8} "
        f"{'p50 s':>6} {'p95 s':>6} {'p95 in':>7} {'p95 out':>7}"
    )
    print(header)
    print("-" * len(header))
    for (alias, group), agg in sorted(groups.items(), key=lambda item: -item[1]["spend"]):
        print(
            f"{alias[:20]:20} {group[:18]:18} {agg['n']:>5} {agg['fail']:>4} {agg['spend']:>8.2f} "
            f"{pct(agg['lat'], 0.5):>6.1f} {pct(agg['lat'], 0.95):>6.1f} "
            f"{pct(agg['in'], 0.95):>7} {pct(agg['out'], 0.95):>7}"
        )
    print("\nProvider budgets:")
    for name, budget in get("/provider/budgets").get("providers", {}).items():
        print(
            f"  {name}: {budget.get('spend') or 0:.2f} of {budget.get('budget_limit')} USD, "
            f"resets {budget.get('budget_reset_at')}"
        )


if __name__ == "__main__":
    main()
