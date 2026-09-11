"""HTTP clients for Radarr, Tautulli and the request system.

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


class SourceError(RuntimeError):
    """A source could not be read in a way the caller must not paper over."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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
            raise SourceError(
                f"GET {path} returned {exc.code}", status=exc.code
            ) from exc
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
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=payload, headers=all_headers, method=method
        )
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


class Radarr:
    """Radarr v3 API, read paths plus the three writes this routine may make."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 120, opener=None):
        self.http = _Http(base_url, timeout=timeout, opener=opener)
        self.headers = {"X-Api-Key": api_key}

    def status(self) -> dict:
        """System status; the preflight that proves the instance answers."""
        return self.http.get_json("/api/v3/system/status", self.headers)

    def media_management(self) -> dict:
        """Media-management config, including the recycle bin settings."""
        return self.http.get_json("/api/v3/config/mediamanagement", self.headers)

    def movies(self) -> list[dict]:
        """The whole library."""
        data = self.http.get_json("/api/v3/movie", self.headers)
        if not isinstance(data, list):
            raise SourceError("movie endpoint did not return a list")
        return data

    def movie(self, movie_id: int) -> dict | None:
        """One movie, or None when Radarr says it does not exist.

        Only a 404 returns None. A timeout or a 500 is propagated, because the
        callers read None as "confirmed gone" -- so swallowing a transient read
        failure here would turn it into a false confirmed deletion.
        """
        try:
            return self.http.get_json(f"/api/v3/movie/{movie_id}", self.headers)
        except SourceError as exc:
            if exc.status == 404:
                return None
            raise

    def tags(self) -> list[dict]:
        """Tag definitions, so labels can be resolved to ids at runtime."""
        return self.http.get_json("/api/v3/tag", self.headers)

    def collections(self) -> list[dict]:
        """Collections with their monitored flag and full membership."""
        data = self.http.get_json("/api/v3/collection", self.headers)
        if not isinstance(data, list):
            raise SourceError("collection endpoint did not return a list")
        return data

    def exclusions(self) -> list[dict]:
        """Import-list exclusions. Keyed on tmdbId; there is no imdbId here."""
        return self.http.get_json("/api/v3/exclusions", self.headers)

    def import_lists(self) -> list[dict]:
        """Configured import lists."""
        return self.http.get_json("/api/v3/importlist", self.headers)

    def movie_history(self, movie_id: int) -> list[dict]:
        """Full history for one film: grabs, imports, deletions, renames."""
        data = self.http.get_json(
            f"/api/v3/history/movie?movieId={movie_id}", self.headers
        )
        return data if isinstance(data, list) else []

    def import_events(
        self, page_size: int = 1000, max_pages: int = 200
    ) -> dict[int, list[dict]]:
        """Every retained import event, grouped by movie id.

        One paged sweep rather than a request per film: the library is thousands
        of records and the per-movie endpoint would turn availability into an
        hour of HTTP. Only import events are kept, since that is the only event
        type availability is derived from.
        """
        events: dict[int, list[dict]] = {}
        page = 1
        while page <= max_pages:
            payload = self.http.get_json(
                f"/api/v3/history?page={page}&pageSize={page_size}"
                "&sortKey=date&sortDirection=ascending&eventType=3",
                self.headers,
            )
            records = (payload or {}).get("records") or []
            for record in records:
                if record.get("eventType") != "downloadFolderImported":
                    continue
                movie_id = record.get("movieId")
                if isinstance(movie_id, int):
                    events.setdefault(movie_id, []).append(record)
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

    def add_tag(self, movie_ids: list[int], tag_id: int) -> tuple[int, Any]:
        """Add one tag to many films. 202 means queued, never "done"."""
        return self.http.request_json(
            "PUT",
            "/api/v3/movie/editor",
            self.headers,
            {"movieIds": movie_ids, "tags": [tag_id], "applyTags": "add"},
        )

    def create_tag(self, label: str) -> tuple[int, Any]:
        """Create a tag by label."""
        return self.http.request_json(
            "POST", "/api/v3/tag", self.headers, {"label": label}
        )

    def delete_movie(
        self, movie_id: int, add_exclusion: bool = True
    ) -> tuple[int, Any]:
        """Delete a film and its files, optionally excluding it from re-import."""
        query = f"?deleteFiles=true&addImportExclusion={'true' if add_exclusion else 'false'}"
        return self.http.request_json(
            "DELETE", f"/api/v3/movie/{movie_id}{query}", self.headers
        )


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
            raise SourceError(
                f"Tautulli {cmd} returned result={response.get('result')!r}"
            )
        return response.get("data")

    def preflight(self) -> str:
        """Cheap liveness check; returns the joke string it answers with."""
        data = self._call("arnold")
        if not isinstance(data, str) or not data.strip():
            raise SourceError("Tautulli preflight returned no usable payload")
        return data

    def movie_history(
        self, page_size: int = 500, max_pages: int = 200
    ) -> tuple[list[dict], dict]:
        """Every ungrouped movie play, with the metadata to judge completeness.

        Two deliberate choices. ``grouping=0`` because a grouped row hides the
        individual plays behind one percentage, and the count that matters is of
        distinct *users*. Ascending date order because new plays then land at the
        end of the last page instead of shifting every row forward mid-walk,
        which would silently skip records.
        """
        rows: list[dict] = []
        seen: set[int] = set()
        start = 0
        declared: int | None = None
        duplicates = 0
        for _ in range(max_pages):
            data = self._call(
                "get_history",
                media_type="movie",
                grouping=0,
                order_column="date",
                order_dir="asc",
                start=start,
                length=page_size,
            )
            if not isinstance(data, dict) or "data" not in data:
                raise SourceError(
                    "Tautulli get_history returned an unexpected structure"
                )
            declared = data.get("recordsFiltered") if declared is None else declared
            page = data.get("data") or []
            for row in page:
                row_id = row.get("row_id") or row.get("id")
                if row_id is None:
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
            "complete": declared is not None and len(rows) >= int(declared),
        }
        return rows, meta

    def metadata(self, rating_key: int) -> dict | None:
        """Item metadata, for the external IDs behind a rating key."""
        try:
            data = self._call("get_metadata", rating_key=rating_key)
        except SourceError:
            return None
        return data if isinstance(data, dict) and data else None

    def now_playing_rating_keys(self) -> set[int]:
        """Rating keys with an active session right now."""
        data = self._call("get_activity")
        keys: set[int] = set()
        for session in (data or {}).get("sessions", []) or []:
            for field in ("rating_key", "grandparent_rating_key", "parent_rating_key"):
                value = session.get(field)
                if value not in (None, ""):
                    try:
                        keys.add(int(value))
                    except (TypeError, ValueError):
                        continue
        return keys

    def now_playing_ids(self) -> set[tuple[str, str]]:
        """External ids of whatever is playing right now, as ``(source, id)``.

        Resolved live rather than from the play-history crosswalk. A candidate is
        by definition a film with no play history, so it has no historical rating
        key -- matching active sessions against that crosswalk could never stop a
        deletion for exactly the films at risk of a first viewing.
        """
        ids: set[tuple[str, str]] = set()
        for key in self.now_playing_rating_keys():
            meta = self.metadata(key)
            if not meta:
                continue
            for guid in meta.get("guids") or []:
                if guid.startswith(("tmdb://", "imdb://")):
                    source, _, value = guid.partition("://")
                    ids.add((source, value))
        return ids


class RequestSystem:  # pylint: disable=too-few-public-methods
    """Jellyseerr/Overseerr requests -- the only proof a person asked for a film."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 60, opener=None):
        self.http = _Http(base_url, timeout=timeout, opener=opener)
        self.headers = {"X-Api-Key": api_key}

    def movie_requests(
        self, page_size: int = 100, max_pages: int = 100
    ) -> dict[int, dict]:
        """Every movie request, keyed by tmdbId.

        Absence here is not evidence of anything: the request system is lightly
        used, so most of the library has no record either way.
        """
        out: dict[int, dict] = {}
        skip = 0
        for _ in range(max_pages):
            payload = self.http.get_json(
                "/api/v1/request",
                self.headers,
                {"take": page_size, "skip": skip, "filter": "all", "sort": "added"},
            )
            results = (payload or {}).get("results") or []
            for item in results:
                if item.get("type") != "movie":
                    continue
                media = item.get("media") or {}
                tmdb_id = media.get("tmdbId")
                if tmdb_id is None:
                    continue
                requested_by = (item.get("requestedBy") or {}).get("displayName")
                out[int(tmdb_id)] = {
                    "requested_by": requested_by,
                    "created_at": item.get("createdAt"),
                    "is_auto_request": bool(item.get("isAutoRequest")),
                }
            info = (payload or {}).get("pageInfo") or {}
            skip += page_size
            if len(results) < page_size or skip >= int(info.get("results") or 0):
                break
        return out
