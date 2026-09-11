"""Minimal S3 client: SigV4 request signing over urllib, no third-party deps.

The cloud sandbox this routine runs in has a bare Python and no package install
step, so the ledger talks to object storage through the standard library. Only
the four verbs the ledger needs are implemented.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_ALGORITHM = "AWS4-HMAC-SHA256"


def _sign(key: bytes, message: str) -> bytes:
    """One HMAC-SHA256 round of the SigV4 key derivation."""
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    """Derive the date/region/service-scoped signing key."""
    key = _sign(f"AWS4{secret}".encode("utf-8"), datestamp)
    key = _sign(key, region)
    key = _sign(key, service)
    return _sign(key, "aws4_request")


class S3Error(RuntimeError):
    """An S3 request failed in a way the caller has to deal with."""

    def __init__(self, status: int, message: str):
        super().__init__(f"S3 {status}: {message}")
        self.status = status


class S3Client:
    """A small, explicit S3 client over urllib.

    Only host, x-amz-date and x-amz-content-sha256 are signed. Signing headers
    that an intermediary may add or strip -- accept-encoding above all -- makes
    the signature depend on a hop you do not control, which fails as an opaque
    ``InvalidRequest`` rather than as a proxy problem.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
        timeout: int = 30,
        opener: urllib.request.OpenerDirector | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()
        parsed = urllib.parse.urlsplit(self.endpoint)
        self.host = parsed.netloc
        self._scheme = parsed.scheme

    def _request(
        self, method: str, key: str, body: bytes = b"", query: dict[str, str] | None = None
    ) -> bytes:
        """Sign and send one request; return the body or raise S3Error."""
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256

        canonical_uri = "/" + self.bucket
        if key:
            canonical_uri += "/" + urllib.parse.quote(key, safe="/")
        canonical_query = urllib.parse.urlencode(sorted((query or {}).items()), quote_via=urllib.parse.quote)

        canonical_headers = (
            f"host:{self.host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(self.secret_key, datestamp, self.region, "s3"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = f"{self._scheme}://{self.host}{canonical_uri}"
        if canonical_query:
            url += "?" + canonical_query
        request = urllib.request.Request(url, data=body or None, method=method)
        request.add_header("Host", self.host)
        request.add_header("x-amz-date", amz_date)
        request.add_header("x-amz-content-sha256", payload_hash)
        request.add_header(
            "Authorization",
            f"{_ALGORITHM} Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}",
        )
        if body:
            request.add_header("Content-Type", "application/json")

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:  # pragma: no cover - network shape
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise S3Error(exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover
            raise S3Error(0, str(exc)) from exc

    def get(self, key: str) -> bytes | None:
        """Object body, or None when it does not exist."""
        try:
            return self._request("GET", key)
        except S3Error as exc:
            if exc.status in (403, 404):
                return None
            raise

    def put(self, key: str, body: bytes) -> None:
        """Write an object."""
        self._request("PUT", key, body=body)

    def delete(self, key: str) -> None:
        """Remove an object."""
        self._request("DELETE", key)

    def list_keys(self, prefix: str, limit: int = 1000) -> list[str]:
        """Keys under a prefix, following continuation tokens."""
        keys: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": str(min(limit, 1000))}
            if token:
                query["continuation-token"] = token
            raw = self._request("GET", "", query=query)
            root = ET.fromstring(raw)
            namespace = {"s3": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
            path = "s3:Contents/s3:Key" if namespace else "Contents/Key"
            keys.extend(node.text or "" for node in root.findall(path, namespace))
            truncated = root.find("s3:IsTruncated" if namespace else "IsTruncated", namespace)
            token_node = root.find(
                "s3:NextContinuationToken" if namespace else "NextContinuationToken", namespace
            )
            if truncated is None or (truncated.text or "").lower() != "true" or token_node is None:
                break
            token = token_node.text
        return keys
