"""HTTP clients for Sonarr, Tautulli and the request system.

Each client validates the *shape* of what came back, not merely the status code.
Every one of these services can answer 200 with a body that means "I failed", and
a routine that only checks the status treats that as an empty result -- which,
for play history, reads as "nobody watched this".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# The external-id schemes that identify a *show* in Plex guids and Sonarr records.
SHOW_GUID_SOURCES = ("tvdb", "tmdb", "imdb")


class SourceError(RuntimeError):
    """A source could not be read in a way the caller must not paper over."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ResultError(SourceError):
    """The service answered, and its answer was "no".

    Kept apart from its parent because the two mean opposite things for safety.
    A refusal is information -- this rating key no longer exists. A timeout is
    the absence of information, and treating the two alike turns "I could not
    check" into "I checked, and it is fine".
    """


def guid_ids(guids: list[str] | None) -> set[tuple[str, str]]:
    """``(source, id)`` pairs from Plex guids such as ``tvdb://75710``."""
    ids: set[tuple[str, str]] = set()
    for guid in guids or []:
        source, sep, value = str(guid).partition("://")
        if sep and source in SHOW_GUID_SOURCES and value:
            ids.add((source, value))
    return ids


class _Http:
    """Shared JSON-over-HTTP plumbing with explicit timeouts."""

    def __init__(self, base_url: str, timeout: int = 60, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()

    def get_json(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict | None = None,
    ) -> Any:
        """GET and decode JSON, raising SourceError on any failure."""
        url = f"{self.base_url}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise SourceError(f"GET {path} returned {response.status}")
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SourceError(f"GET {path} returned {exc.code}", status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourceError(f"GET {path} failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SourceError(f"GET {path} returned a non-JSON body") from exc

    def request_json(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> tuple[int, Any]:
        """Send a non-GET request; return (status, decoded-body-or-None)."""
        payload = json.dumps(body).encode() if body is not None else None
        all_headers = dict(headers or {})
        if payload:
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=payload, headers=all_headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                try:
                    return response.status, json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    return response.status, None
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourceError(f"{method} {path} failed: {exc}") from exc


class Sonarr:
    """Sonarr v3 API, read paths plus the three writes this routine may make."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 120, opener=None):
        self.http = _Http(base_url, timeout=timeout, opener=opener)
        self.headers = {"X-Api-Key": api_key}

    def status(self) -> dict:
        """System status; the preflight that proves the instance answers."""
        return self.http.get_json("/api/v3/system/status", self.headers)

    def media_management(self) -> dict:
        """Media-management config, including the recycle bin settings."""
        return self.http.get_json("/api/v3/config/mediamanagement", self.headers)

    def all_series(self) -> list[dict]:
        """The whole library."""
        data = self.http.get_json("/api/v3/series", self.headers)
        if not isinstance(data, list):
            raise SourceError("series endpoint did not return a list")
        return data

    def series(self, series_id: int) -> dict | None:
        """One series, or None when Sonarr says it does not exist.

        Only a 404 returns None. A timeout or a 500 is propagated, because the
        callers read None as "confirmed gone" -- so swallowing a transient read
        failure here would turn it into a false confirmed deletion.
        """
        try:
            return self.http.get_json(f"/api/v3/series/{series_id}", self.headers)
        except SourceError as exc:
            if exc.status == 404:
                return None
            raise

    def tags(self) -> list[dict]:
        """Tag definitions, so labels can be resolved to ids at runtime."""
        return self.http.get_json("/api/v3/tag", self.headers)

    def exclusions(self) -> list[dict]:
        """Import-list exclusions, keyed on tvdbId."""
        data = self.http.get_json("/api/v3/importlistexclusion", self.headers)
        if not isinstance(data, list):
            raise SourceError("importlistexclusion endpoint did not return a list")
        return data

    def import_events(self, page_size: int = 1000, max_pages: int = 500) -> dict[int, list[dict]]:
        """Every retained import event, grouped by series id.

        One paged sweep rather than a request per series. Each record carries its
        ``episodeId``, which is what lets availability take every episode's
        *first* import rather than its latest upgrade.
        """
        events: dict[int, list[dict]] = {}
        page = 1
        while page <= max_pages:
            payload = self.http.get_json(
                f"/api/v3/history?page={page}&pageSize={page_size}" "&sortKey=date&sortDirection=ascending&eventType=3",
                self.headers,
            )
            records = (payload or {}).get("records") or []
            for record in records:
                if record.get("eventType") != "downloadFolderImported":
                    continue
                series_id = record.get("seriesId")
                if isinstance(series_id, int):
                    events.setdefault(series_id, []).append(record)
            total = int((payload or {}).get("totalRecords") or 0)
            if page * page_size >= total or not records:
                break
            page += 1
        return events

    def history_horizon(self) -> str | None:
        """Date of the oldest retained history row.

        Availability is derived from history, so how far back history goes is a
        hard limit on what the routine can know. Read it, do not assume it.
        """
        page = self.http.get_json(
            "/api/v3/history?page=1&pageSize=1&sortKey=date&sortDirection=ascending",
            self.headers,
        )
        records = (page or {}).get("records") or []
        return records[0].get("date") if records else None

    def add_tag(self, series_ids: list[int], tag_id: int) -> tuple[int, Any]:
        """Add one tag to many series. 202 means queued, never "done"."""
        return self.http.request_json(
            "PUT",
            "/api/v3/series/editor",
            self.headers,
            {"seriesIds": series_ids, "tags": [tag_id], "applyTags": "add"},
        )

    def create_tag(self, label: str) -> tuple[int, Any]:
        """Create a tag by label."""
        return self.http.request_json("POST", "/api/v3/tag", self.headers, {"label": label})

    def delete_series(self, series_id: int, add_exclusion: bool = True) -> tuple[int, Any]:
        """Delete a series and its files, optionally excluding it from re-import."""
        query = f"?deleteFiles=true&addImportListExclusion={'true' if add_exclusion else 'false'}"
        return self.http.request_json("DELETE", f"/api/v3/series/{series_id}{query}", self.headers)


class Tautulli:
    """Tautulli API v2, with result-level validation and stable pagination."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 120, opener=None):
        self.http = _Http(base_url, timeout=timeout, opener=opener)
        self.api_key = api_key

    def _call(self, cmd: str, **params) -> Any:
        """Invoke one command and unwrap the envelope, or raise."""
        query = {"apikey": self.api_key, "cmd": cmd, **params}
        payload = self.http.get_json("/api/v2", params=query)
        response = (payload or {}).get("response") or {}
        if response.get("result") != "success":
            raise ResultError(f"Tautulli {cmd} returned result={response.get('result')!r}")
        return response.get("data")

    def preflight(self) -> str:
        """Cheap liveness check; returns the joke string it answers with."""
        data = self._call("arnold")
        if not isinstance(data, str) or not data.strip():
            raise SourceError("Tautulli preflight returned no usable payload")
        return data

    def episode_history(self, page_size: int = 500, max_pages: int = 400) -> tuple[list[dict], dict]:
        """Every ungrouped episode play, with the metadata to judge completeness.

        ``grouping=0`` because a grouped row hides the individual plays behind
        one percentage. Ascending date order because new plays then land at the
        end of the last page instead of shifting every row forward mid-walk,
        which would silently skip records.
        """
        rows: list[dict] = []
        seen: set[int] = set()
        start = 0
        declared: int | None = None
        duplicates = 0
        without_id = 0
        for _ in range(max_pages):
            data = self._call(
                "get_history",
                media_type="episode",
                grouping=0,
                order_column="date",
                order_dir="asc",
                start=start,
                length=page_size,
            )
            if not isinstance(data, dict) or "data" not in data:
                raise SourceError("Tautulli get_history returned an unexpected structure")
            declared = data.get("recordsFiltered") if declared is None else declared
            page = data.get("data") or []
            for row in page:
                row_id = row.get("row_id") or row.get("id")
                if row_id is None:
                    # No row id yet for an in-progress session; dropping it under-counted
                    # `retrieved` and blocked the run. Kept undeduplicated: over-counting is safe.
                    rows.append(row)
                    without_id += 1
                    continue
                if int(row_id) in seen:
                    duplicates += 1
                    continue
                seen.add(int(row_id))
                rows.append(row)
            if len(page) < page_size:
                break
            start += page_size

        meta = {
            "declared": declared,
            "retrieved": len(rows),
            "duplicates_skipped": duplicates,
            "without_id": without_id,
            "complete": declared is not None and len(rows) >= int(declared),
        }
        return rows, meta

    def metadata(self, rating_key: int) -> dict | None:
        """Item metadata, for the external IDs behind a rating key.

        None means Tautulli answered and has no such item. A timeout or a server
        error is raised instead: swallowing it here let the active-session check
        drop the show somebody is watching right now.
        """
        try:
            data = self._call("get_metadata", rating_key=rating_key)
        except ResultError:
            return None
        return data if isinstance(data, dict) and data else None

    def now_playing_show_keys(self) -> set[int]:
        """Show rating keys with an active episode session right now.

        An episode session names its show in ``grandparent_rating_key``. One that
        does not cannot be matched to a series, and a short answer here reads as
        "nobody is watching it", so it raises instead.
        """
        data = self._call("get_activity")
        keys: set[int] = set()
        for session in (data or {}).get("sessions", []) or []:
            if session.get("media_type") not in (None, "", "episode"):
                continue
            value = session.get("grandparent_rating_key")
            if value in (None, ""):
                if session.get("media_type") == "episode":
                    raise SourceError("an episode session carries no show rating key")
                continue
            try:
                keys.add(int(value))
            except (TypeError, ValueError) as exc:
                raise SourceError(f"unparsable show rating key {value!r}") from exc
        return keys

    def now_playing_ids(self) -> set[tuple[str, str]]:
        """External ids of every show being watched right now, as ``(source, id)``.

        Resolved live from the show's own metadata rather than the play-history
        crosswalk. A candidate is a series nobody has finished an episode of, so it
        may have no historical rating key -- matching sessions against that
        crosswalk could never stop a deletion for a first viewing.
        """
        ids: set[tuple[str, str]] = set()
        for key in self.now_playing_show_keys():
            meta = self.metadata(key)
            if not meta:
                continue
            ids |= guid_ids(meta.get("guids"))
        return ids


class RequestSystem:  # pylint: disable=too-few-public-methods
    """Jellyseerr/Overseerr requests -- the only proof a person asked for a show."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 60, opener=None):
        self.http = _Http(base_url, timeout=timeout, opener=opener)
        self.headers = {"X-Api-Key": api_key}

    def tv_requests(self, page_size: int = 100, max_pages: int = 100) -> dict[tuple[str, int], dict]:
        """Every TV request, keyed by ``("tvdb", id)`` and ``("tmdb", id)``.

        Both keys, because Seerr records the TMDb id reliably and the TVDB id only
        when it could resolve one; a lookup tries TVDB first and falls back.
        Absence here is not evidence of anything: most of the library has no
        record either way.
        """
        out: dict[tuple[str, int], dict] = {}
        skip = 0
        for _ in range(max_pages):
            payload = self.http.get_json(
                "/api/v1/request",
                self.headers,
                {"take": page_size, "skip": skip, "filter": "all", "sort": "added"},
            )
            results = (payload or {}).get("results") or []
            for item in results:
                if item.get("type") != "tv":
                    continue
                media = item.get("media") or {}
                record = {
                    "requested_by": (item.get("requestedBy") or {}).get("displayName"),
                    "created_at": item.get("createdAt"),
                    "is_auto_request": bool(item.get("isAutoRequest")),
                }
                for source, field in (("tvdb", "tvdbId"), ("tmdb", "tmdbId")):
                    value = media.get(field)
                    if isinstance(value, int) and value:
                        out[(source, value)] = record
            info = (payload or {}).get("pageInfo") or {}
            skip += page_size
            if len(results) < page_size or skip >= int(info.get("results") or 0):
                break
        return out


def find_request(requests: dict[tuple[str, int], dict], tvdb_id: int | None, tmdb_id: int | None) -> dict | None:
    """The request record for a series, matched on TVDB first, then TMDb."""
    if tvdb_id and ("tvdb", tvdb_id) in requests:
        return requests[("tvdb", tvdb_id)]
    if tmdb_id and ("tmdb", tmdb_id) in requests:
        return requests[("tmdb", tmdb_id)]
    return None
