#!/usr/bin/env python3
"""Report container images whose upstream has moved on where Renovate cannot see it.

Renovate only compares tags of the same shape on the same image path, so it goes quiet when a
project renames its image, changes tag shape (a suffix, a part count, calver to semver), stops
publishing, or archives. In October 2026 this had left about twenty apps stale with no PR, among
them a two-major Jellyfin gap, a game server whose registry had vanished and a 2024 Chrome.

This asks each image's upstream GitHub repository instead of its tag list. The repository comes
from the image's `org.opencontainers.image.source` label, else from a ghcr.io path. Updates that
already have an open Renovate PR or a Dependency Dashboard entry are dropped, so what is left is
what Renovate will never raise. IGNORE lists deliberate exceptions; say why.

Run locally:  GH_TOKEN=$(gh auth token) python3 .github/scripts/check_upstream_drift.py
Prints a Markdown report. Exits 0 when clean, 10 with findings, 11 when some images could not be
checked (rate limit, network), and 1 if the script itself fails.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import Message

ROOT = pathlib.Path(__file__).resolve().parents[2]
REPO = "LukeEvansTech/talos-cluster"
STALE_IMAGE_DAYS = 365
# Renovate holds releases 3 days and the review gate can add more; past this it has missed it.
RELEASE_GRACE_DAYS = 21
# A crash exits 1, so report outcomes use their own codes; incomplete means never close the issue.
EXIT_CLEAN, EXIT_FINDINGS, EXIT_INCOMPLETE = 0, 10, 11
# Marks a reason that is only a staleness signal, listed apart from updates Renovate cannot raise.
WATCH = "watch: "

# image (no tag): reason
IGNORE = {
    "ghcr.io/claabs/epicgames-freegames-node": "upstream publishes only `latest`",
    "docker.io/scuzza/gluetun-webui": "upstream publishes only `latest`",
    "ghcr.io/codelooks-com/packer-runner": "our own image, rebuilt on every push",
    "itzg/minecraft-server": "`java25` is a JRE track; Renovate follows its digest",
    "ghcr.io/ggml-org/llama.cpp": "`server` track, versioned by the llamacpp Renovate preset",
    "mikolatero/vlmcsd": "upstream archived in 2020; nothing newer exists",
    "ghcr.io/timothystewart6/prometheus-plex-exporter": "dormant fork, no releases to move to",
}

# Comment lines may sit between repository and tag; CNPG clusters name theirs `imageName`.
PAIR_RE = re.compile(r"^\s*repository:\s*\"?([\w./-]+)\"?\s*\n(?:\s*#.*\n)*\s*tag:\s*\"?([\w.+-]+)", re.M)
IMAGE_RE = re.compile(r"^\s*-?\s*image(?:Name)?:\s*\"?((?:[\w.-]+/)+[\w.-]+):([\w.+-]+)", re.M)
VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
PRERELEASE_RE = re.compile(r"[-.](alpha|beta|rc|pre|dev)\.?\d*", re.I)
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class Skip(Exception):
    """The registry could not be asked (rate limit, network); not evidence of drift."""


def image_refs() -> list[tuple[str, str]]:
    """Every distinct (image, tag) under kubernetes/; one image can follow several tag tracks."""
    refs: set[tuple[str, str]] = set()
    # Kustomize generators turn shell scripts into Jobs, so their image lines deploy too.
    paths = [*(ROOT / "kubernetes").rglob("*.yaml"), *(ROOT / "kubernetes").rglob("*.sh")]
    for path in paths:
        refs.update(extract(path.read_text(errors="ignore")))
    return sorted(refs)


def extract(text: str) -> list[tuple[str, str]]:
    """(image, tag) pairs declared in one manifest, templated ones dropped."""
    found = PAIR_RE.findall(text) + IMAGE_RE.findall(text)
    # A tag with no digit on an unqualified name is a values key such as `repository: foo`.
    return [(image, tag) for image, tag in found if "$" not in image and ("/" in image or re.search(r"\d", tag))]


def hub_repo(image: str) -> str | None:
    """Docker Hub `namespace/name`, or None for other registries."""
    first, _, rest = image.partition("/")
    if first == "docker.io":
        image = rest
    elif "." in first or ":" in first:
        return None
    return image if "/" in image else f"library/{image}"


def fetch(url: str, token: str | None = None, method: str = "GET") -> tuple[int, bytes, Message]:
    """Status, body and headers for url; raises Skip when the registry cannot be asked."""
    headers = {"Accept": ACCEPT, **({"Authorization": f"Bearer {token}"} if token else {})}
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as res:
            return res.status, res.read(), res.headers
    except urllib.error.HTTPError as err:
        if err.code == 429 or err.code >= 500:
            raise Skip(f"registry answered {err.code}") from err
        return err.code, b"", err.headers
    except (OSError, ValueError) as err:  # network errors and malformed URLs
        raise Skip(type(err).__name__) from err


class Registry:  # pylint: disable=too-few-public-methods  # one call, `get`, is all callers need
    """A Docker Registry v2 repository, authenticated anonymously when it asks."""

    def __init__(self, image: str) -> None:
        host, _, self.path = image.partition("/")
        self.base = f"https://{host}/v2/{self.path}"
        self.token: str | None = None

    def get(self, ref: str, kind: str = "manifests", method: str = "GET") -> tuple[int, bytes]:
        """Status and body of a manifest or blob, fetching an anonymous token on the first 401."""
        status, body, headers = fetch(f"{self.base}/{kind}/{ref}", self.token, method)
        if status == 401 and self.token is None:
            params = dict(re.findall(r'(\w+)="([^"]*)"', headers.get("WWW-Authenticate", "")))
            if "realm" in params:
                query = "&".join(f"{k}={params[k]}" for k in ("service", "scope") if k in params)
                _, auth, _ = fetch(f"{params['realm']}?{query}")
                self.token = json.loads(auth or b"{}").get("token") or ""
                status, body, _ = fetch(f"{self.base}/{kind}/{ref}", self.token, method)
        return status, body


def image_meta(image: str, tag: str) -> dict:
    """Build time and labels for image:tag, or an error when the tag cannot be fetched."""
    hub = hub_repo(image)
    if hub:
        return hub_meta(hub, tag)
    reg = Registry(image)
    status, body = reg.get(tag)
    if status != 200:
        return {"error": f"registry answered {status}"}
    manifest = json.loads(body)
    if "manifests" in manifest:
        listed = manifest["manifests"]
        entries = [m for m in listed if m.get("platform", {}).get("architecture") == "amd64"]
        _, body = reg.get((entries or listed)[0]["digest"])
        manifest = json.loads(body or b"{}")
    config = manifest.get("config", {}).get("digest")
    if not config:
        return {"error": "no image config"}
    status, body = reg.get(config, "blobs")
    if status != 200:
        raise Skip(f"config blob answered {status}")
    blob = json.loads(body or b"{}")
    labels = (blob.get("config") or {}).get("Labels") or {}
    return {"created": blob.get("created"), "labels": labels}


def hub_meta(hub: str, tag: str) -> dict:
    """Docker Hub variant of image_meta, spending one counted pull per image."""
    # The Hub API names each platform's digest without touching the anonymous pull limit, so
    # only the single platform manifest read below counts; blob reads never do.
    status, body, _ = fetch(f"https://hub.docker.com/v2/repositories/{hub}/tags/{tag}")
    if status != 200:
        return {"error": f"Docker Hub answered {status}"}
    data = json.loads(body)
    meta = {"created": data.get("tag_last_pushed") or data.get("last_updated"), "labels": {}}
    images = data.get("images") or []
    amd64 = [i for i in images if i.get("architecture") == "amd64" and i.get("digest")]
    if amd64:
        meta["labels"] = hub_labels(hub, amd64[0]["digest"])
    return meta


def hub_labels(hub: str, digest: str) -> dict:
    """Image config labels for one Docker Hub platform manifest."""
    reg = Registry(f"registry-1.docker.io/{hub}")
    status, body = reg.get(digest)
    if status != 200:
        raise Skip(f"Docker Hub manifest answered {status}")
    config = json.loads(body or b"{}").get("config", {}).get("digest")
    if not config:
        return {}
    status, body = reg.get(config, "blobs")
    if status != 200:
        raise Skip(f"config blob answered {status}")
    return (json.loads(body or b"{}").get("config") or {}).get("Labels") or {}


def tag_exists(image: str, tag: str) -> bool:
    """Whether the registry publishes image:tag."""
    hub = hub_repo(image)
    if hub:
        return fetch(f"https://hub.docker.com/v2/repositories/{hub}/tags/{tag}")[0] == 200
    return Registry(image).get(tag, method="HEAD")[0] == 200


def safe_meta(image: str, tag: str) -> dict:
    """image_meta, with a registry that cannot be asked reported as `skipped`."""
    try:
        return image_meta(image, tag)
    except Skip as why:
        return {"skipped": str(why)}


def same_name(repo_name: str, image: str) -> bool:
    """Whether a repository name and an image name plausibly name the same project."""
    a, b = (re.sub(r"[^a-z0-9]", "", x.lower()) for x in (repo_name, image.split("/")[-1]))
    return bool(a and b) and (a in b or b in a)


def source_repo(image: str, meta: dict) -> str | None:
    """Upstream GitHub `owner/name` from the source label, else from a ghcr.io path."""
    label = meta.get("labels", {}).get("org.opencontainers.image.source", "")
    match = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?]|$)", label)
    # A label inherited from a base image or a shared build repo names someone else's releases.
    if match and same_name(match[2], image):
        return f"{match[1]}/{match[2]}"
    parts = image.split("/")
    if parts[0] == "ghcr.io" and len(parts) >= 3:
        return f"{parts[1]}/{parts[2]}"
    return None


def graphql(query: str) -> dict:
    """Run a GitHub GraphQL query with GH_TOKEN and return its `data`."""
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"bearer {os.environ['GH_TOKEN']}"},
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        payload = json.loads(res.read())
    # Rate limits arrive as HTTP 200 with `errors`; only a missing repository is an answer.
    failures = [e for e in payload.get("errors") or [] if e.get("type") != "NOT_FOUND"]
    if failures:
        raise RuntimeError(f"GitHub GraphQL failed: {failures[0].get('message')}")
    return payload.get("data") or {}


def upstream(repos: list[str]) -> dict[str, dict | None]:
    """Name, archived flag and latest release for each repo, 40 per query; None when not found."""
    out: dict[str, dict | None] = {}
    for start in range(0, len(repos), 40):
        chunk = repos[start : start + 40]
        body = " ".join(
            f'r{n}: repository(owner: "{r.split("/")[0]}", name: "{r.split("/")[1]}") '
            "{ nameWithOwner isArchived latestRelease { tagName publishedAt } }"
            for n, r in enumerate(chunk)
        )
        data = graphql(f"{{ {body} }}")
        out.update({r: data.get(f"r{n}") for n, r in enumerate(chunk)})
    return out


def renovate_pending() -> list[str]:
    """Each open Renovate PR and each pending dashboard line, lowercased."""
    owner, name = REPO.split("/")
    data = graphql(
        f'{{ repository(owner: "{owner}", name: "{name}") {{ '
        "pullRequests(states: OPEN, first: 100) { nodes { title body } } "
        'issues(states: OPEN, first: 5, filterBy: { createdBy: "renovate[bot]" }) '
        "{ nodes { title body } } } }"
    )
    repo = data.get("repository") or {}
    # Group PRs ("Update Prowler group") name their images only in the body's package table.
    prs = [f"{p['title']}\n{p['body']}" for p in repo.get("pullRequests", {}).get("nodes", [])]
    # The Detected Dependencies section names every image, pending or not, so it is cut off.
    issues = repo.get("issues", {}).get("nodes", [])
    dash = [i["body"].split("## Detected")[0] for i in issues if "Dashboard" in i["title"]]
    lines = [line for body in dash for line in body.splitlines()]
    return [chunk.lower() for chunk in prs + lines]


def is_pending(image: str, tag: str, pending: list[str]) -> bool:
    """Whether Renovate already has this image's own tag track in a PR or dashboard entry."""
    match = VERSION_RE.search(tag)
    track = match[0] if match else tag
    return any(image.lower() in chunk and track in chunk for chunk in pending)


def version(tag: str) -> tuple[int, ...] | None:
    """First dotted number in tag as a tuple, or None."""
    match = VERSION_RE.search(tag)
    return tuple(int(x) for x in match[0].split(".")) if match else None


def newer(release: str, tag: str) -> bool:
    """Whether release is past tag, counting a stable release past the same-numbered prerelease."""
    rel, cur = version(release), version(tag)
    if not rel or not cur:
        return False
    if padded(rel, cur) != padded(cur, rel):
        return padded(rel, cur) > padded(cur, rel)
    return bool(PRERELEASE_RE.search(tag)) and not PRERELEASE_RE.search(release)


def padded(ver: tuple[int, ...], other: tuple[int, ...]) -> tuple[int, ...]:
    """ver with trailing zeros to other's length, so 2.25 and 2.25.0 compare equal."""
    return ver + (0,) * (len(other) - len(ver))


def calendar(ver: tuple[int, ...]) -> bool:
    """Whether a version is calendar-based (2026.10.1), which never orders against semver."""
    return ver[0] >= 2000


def shape(tag: str) -> str:
    """Digits collapsed, so 1.2.3 and 1.4.0 match but 1.2 and 1.2.3-alpine do not."""
    return re.sub(r"\d+", "9", tag.removeprefix("v"))


def age_days(stamp: str | None, now: datetime) -> int | None:
    """Whole days from an ISO timestamp to now; None when absent or an epoch stamp."""
    # Reproducible builds stamp the epoch, which says nothing about the image's age.
    if not stamp or stamp.startswith("1970"):
        return None
    return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).days


def release_reasons(image: str, tag: str, info: dict, now: datetime) -> list[str]:
    """Why upstream's latest release is newer than tag yet invisible to Renovate or long overdue."""
    reasons = []
    release = info.get("latestRelease")
    current, latest = version(tag), version(release["tagName"]) if release else None
    if release and current and latest and calendar(current) != calendar(latest):
        reasons.append(f"upstream switched version scheme; latest release is {release['tagName']}")
    elif release and current and latest and newer(release["tagName"], tag):
        released = age_days(release["publishedAt"], now) or 0
        # Same version in our tag's own prefix and suffix: Renovate can see it, it may just lag.
        ours = VERSION_RE.sub(".".join(map(str, latest[: len(current)])), tag, count=1)
        same_shape = shape(tag) == shape(release["tagName"])
        # ours equals tag when only a prerelease suffix differs; finding it proves nothing.
        trackable = same_shape or (ours != tag and shape(ours) == shape(tag) and tag_exists(image, ours))
        if not trackable:
            name = release["tagName"]
            reasons.append(f"upstream {name} is not published as a `{shape(tag)}` tag")
        elif released > RELEASE_GRACE_DAYS:
            reasons.append(f"upstream {release['tagName']} released {released} days ago")
    return reasons


def assess(
    image: str,
    tag: str,
    meta: dict,
    source: tuple[str | None, dict | None],
    now: datetime,
) -> list[str]:
    """Reasons Renovate will not move this image; empty when it is fine.

    `source` is the (repository from the image's label, upstream info for it) pair.
    """
    if "error" in meta:
        return [f"image cannot be pulled ({meta['error']})"]
    repo, info = source
    reasons = []
    built = age_days(meta.get("created"), now)
    stale = built is not None and built > STALE_IMAGE_DAYS
    if not re.search(r"\d", tag) or SHA_RE.fullmatch(tag):
        reasons.append(f"floating tag `{tag}` gives Renovate nothing to compare")
    if info:
        release = (info.get("latestRelease") or {}).get("tagName", "")
        # GitHub redirects old renames forever; a move matters once it carries newer releases.
        if repo and info["nameWithOwner"].lower() != repo.lower() and newer(release, tag):
            reasons.append(f"repository moved to {info['nameWithOwner']}")
        if info["isArchived"] and stale:
            reasons.append("upstream archived")
        reasons += release_reasons(image, tag, info, now)
    if stale and not reasons:
        reasons.append(f"{WATCH}image built {built} days ago")
    return reasons


Ref = tuple[str, str]


def collect() -> tuple[list[Ref], dict[Ref, dict], dict[Ref, str | None], dict, list[str]]:
    """Look up each image: refs, registry metadata, source repos, upstream, pending."""
    refs = image_refs()
    with cf.ThreadPoolExecutor(16) as pool:
        metas = dict(zip(refs, pool.map(lambda ref: safe_meta(*ref), refs)))
    repos = {ref: source_repo(ref[0], metas[ref]) for ref in refs}
    info = upstream(sorted({r for r in repos.values() if r}))
    return refs, metas, repos, info, renovate_pending()


def render(rows: list[str], watch: list[str], skipped: list[str], checked: int) -> None:
    """Print the Markdown report."""
    print(
        f"Checked {checked} images ({len(IGNORE)} ignored, {len(skipped)} unreachable): "
        f"{len(rows)} need action, {len(watch)} to watch.\n"
    )
    sections = (
        ("Updates Renovate will not raise", rows),
        ("Upstream quiet for over a year", watch),
    )
    for title, items in sections:
        if items:
            print(f"### {title}\n\n| Image | Upstream | Why |\n| --- | --- | --- |")
            print("\n".join(items) + "\n")
    if skipped:
        names = ", ".join(f"`{s}`" for s in skipped)
        print(f"\nNot checked this run (registry unreachable or rate-limited): {names}")


def main() -> int:
    """Print the drift report and return an EXIT_* status."""
    refs, metas, repos, info, pending = collect()
    now = datetime.now(timezone.utc)
    rows: list[str] = []
    watch: list[str] = []
    skipped = [f"{i}:{t}" for (i, t), meta in metas.items() if "skipped" in meta]
    for image, tag in refs:
        if f"{image}:{tag}" in skipped:
            continue
        repo = repos[(image, tag)]
        try:
            reasons = assess(image, tag, metas[(image, tag)], (repo, info.get(repo or "")), now)
        except Skip:
            skipped.append(f"{image}:{tag}")
            continue
        if image in IGNORE:
            # An ignore excuses the drift it names; a tag that can no longer be pulled is new.
            reasons = [r for r in reasons if r.startswith("image cannot be pulled")]
        if not reasons or is_pending(image, tag, pending):
            continue
        why = "; ".join(r.removeprefix(WATCH) for r in reasons)
        row = f"| `{image}:{tag}` | {repo or '?'} | {why} |"
        (watch if reasons[0].startswith(WATCH) else rows).append(row)
    render(rows, watch, skipped, len(refs) - len(skipped))
    if skipped:
        return EXIT_INCOMPLETE
    return EXIT_FINDINGS if rows or watch else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
