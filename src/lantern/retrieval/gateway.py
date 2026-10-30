"""Owner-launched, exact-URL public retrieval gateway for Gate 6."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from email.message import Message
import hashlib
from http.client import HTTPSConnection
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
from typing import Any, Callable, Iterator
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .content import sanitize_document
from .request import ResearchRequest


_ALLOWED_CONTENT_TYPES = frozenset(
    {"text/html", "text/plain", "application/xhtml+xml"}
)
_USER_AGENT = "Project-Lantern-Gate6/0.1 (+owner-launched; read-only)"


Resolver = Callable[[str], tuple[str, ...]]
Fetcher = Callable[[str, str, str, float], Any]


class _PinnedHTTPSConnection(HTTPSConnection):
    """Verify TLS for the owner-reviewed host while connecting to a validated IP."""

    def __init__(self, hostname: str, address: str, timeout: float) -> None:
        self._validated_address = address
        super().__init__(
            hostname,
            443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                sock,
                server_hostname=self.host,
            )
        except BaseException:
            sock.close()
            raise


def _resolve_public_addresses(hostname: str) -> tuple[str, ...]:
    addresses = {
        str(item[4][0])
        for item in socket.getaddrinfo(
            hostname,
            443,
            type=socket.SOCK_STREAM,
        )
    }
    return tuple(sorted(addresses))


def validate_public_https_url(
    url: str,
    *,
    resolver: Resolver = _resolve_public_addresses,
) -> tuple[str, tuple[str, ...]]:
    """Reject credentials, unusual ports, and non-public targets."""
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https":
        raise PermissionError("Gate 6 live retrieval requires HTTPS")
    if not parsed.hostname:
        raise ValueError("retrieval URL requires a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise PermissionError("retrieval URLs cannot contain credentials")
    if parsed.fragment:
        raise ValueError("remove URL fragments before owner review")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("retrieval URL has an invalid port") from error
    if port not in (None, 443):
        raise PermissionError("Gate 6 live retrieval permits only HTTPS port 443")

    hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        raise PermissionError("local hostnames are outside public retrieval scope")

    addresses = resolver(hostname)
    if not addresses:
        raise OSError("retrieval hostname did not resolve")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise OSError("resolver returned an invalid IP address") from error
        if not parsed_address.is_global:
            raise PermissionError(
                "retrieval target resolved outside globally routable public space"
            )
    return hostname, tuple(sorted(set(addresses)))


@contextmanager
def _default_fetch(
    url: str,
    hostname: str,
    address: str,
    timeout: float,
) -> Iterator[Any]:
    """Fetch without a second DNS lookup or redirect handling."""
    parsed = urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"

    connection = _PinnedHTTPSConnection(hostname, address, timeout)
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Accept": "text/html,text/plain,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "User-Agent": _USER_AGENT,
            },
        )
        yield connection.getresponse()
    finally:
        connection.close()


def _response_content_type(headers: Message) -> str:
    content_type = headers.get_content_type().casefold()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise PermissionError(
            f"unsupported retrieval content type: {content_type}"
        )
    return content_type


def _read_bounded(response: Any, maximum_bytes: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > maximum_bytes:
                raise ValueError("declared response exceeds remaining byte budget")
        except ValueError as error:
            if str(error) == "declared response exceeds remaining byte budget":
                raise
            raise ValueError("invalid Content-Length header") from error
    payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise ValueError("response exceeds remaining byte budget")
    return payload


def retrieve_public_documents(
    request: ResearchRequest,
    urls: tuple[str, ...],
    *,
    timeout_seconds: float = 15.0,
    resolver: Resolver = _resolve_public_addresses,
    fetcher: Fetcher = _default_fetch,
    now: datetime | None = None,
) -> dict[str, object]:
    """Fetch exact owner-selected URLs into a non-authoritative review package."""
    if request.privacy_class != "public":
        raise PermissionError("live retrieval accepts only public research requests")
    if not urls:
        raise ValueError("at least one exact owner-selected URL is required")
    if len(urls) > request.maximum_sources:
        raise ValueError("URL count exceeds the request source budget")
    if len(set(urls)) != len(urls):
        raise ValueError("duplicate retrieval URLs are not allowed")
    if not 0.1 <= timeout_seconds <= 60.0:
        raise ValueError("timeout must be between 0.1 and 60 seconds")

    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    remaining = request.maximum_bytes
    sources: list[dict[str, object]] = []
    for url in urls:
        hostname, addresses = validate_public_https_url(url, resolver=resolver)
        if remaining < 1:
            raise ValueError("research byte budget exhausted before all URLs")

        address = addresses[0]
        with fetcher(url, hostname, address, timeout_seconds) as response:
            status = int(response.status)
            if status != 200:
                raise HTTPError(
                    url,
                    status,
                    "retrieval requires HTTP 200; redirects are disabled",
                    response.headers,
                    None,
                )
            content_type = _response_content_type(response.headers)
            raw = _read_bounded(response, remaining)
            charset = response.headers.get_content_charset() or "utf-8"

        remaining -= len(raw)
        try:
            rendered = raw.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError) as error:
            raise ValueError("retrieved text has an unsupported or invalid encoding") from error
        document = sanitize_document(
            url,
            rendered,
            maximum_text_bytes=len(raw) or 1,
        )
        sources.append(
            {
                "source_url": url,
                "source_host": hostname,
                "retrieved_at": timestamp,
                "http_status": 200,
                "content_type": content_type,
                "raw_bytes": len(raw),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "sanitized_text": document.text,
                "sanitized_text_sha256": hashlib.sha256(
                    document.text.encode("utf-8")
                ).hexdigest(),
                "active_content_removed": document.active_content_removed,
                "suspicious_instructions": list(document.suspicious_instructions),
                "trusted_as_instructions": False,
                "connection_pinned_to_validated_address": True,
            }
        )

    package: dict[str, object] = {
        "mode": "owner_launched_public_retrieval",
        "generated_at": timestamp,
        "request": asdict(request),
        "sources": sources,
        "safety": {
            "authoritative_state_accessed": False,
            "authoritative_state_mutated": False,
            "private_memory_accessed": False,
            "network_requests_performed": len(sources),
            "connection_pinned_to_validated_address": True,
            "external_writes_performed": False,
            "authority_granted": False,
            "identity_enrolled": False,
            "automatic_import_available": False,
            "web_content_trusted_as_instructions": False,
        },
        "review_required": True,
    }
    canonical = json.dumps(
        package,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    package["package_sha256"] = hashlib.sha256(canonical).hexdigest()
    return package


def write_evidence_package(
    package: dict[str, object],
    output_path: str | Path,
) -> Path:
    """Create a private, non-overwriting JSON artifact for owner review."""
    path = Path(output_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (
        json.dumps(package, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    return path
