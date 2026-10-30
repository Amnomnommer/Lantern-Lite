"""Gate 6 hardening: untrusted pages and files are parsed in a disposable worker process.

Fetching stays in the explorer, which already validates every address. Everything
that touches hostile content runs in a short-lived child process instead:
- consent markers in the page;
- sanitising and redaction;
- link and dialogue extraction;
- PDF decoding.

The child:
- gets an empty environment and a throwaway working directory;
- has hard limits: memory, CPU seconds, no file writes, few open files, no core
  dumps;
- has no network when the system allows an unprivileged user and network
  namespace (`unshare -rn`). The journal records which isolation applied;
- returns one bounded JSON object, which the parent checks field by field.

If the worker crashes, hangs, oversteps a limit or returns anything malformed, that
one page is skipped with a reason and reading continues (degrade, never fail open).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

MAX_INPUT_BYTES = 31_000_000
MAX_OUTPUT_BYTES = 8_000_000
TIMEOUT_SECONDS = 30
MEMORY_BYTES = 1024 * 1024 * 1024
CPU_SECONDS = 20
_PAGE_FIELDS = {"refusal", "text", "suspicious", "links", "cited", "exchanges"}
_FILE_FIELDS = {"text", "error"}


# -- inside the worker -----------------------------------------------------------------

def parse_page(url: str, content_type: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    """What one fetched page yields; pure, and identical to the in-process path."""
    from lantern.explorer import Explorer, dialogue_exchanges, extract_cited, extract_links, redact
    from lantern.retrieval.content import sanitize_document

    html = body.decode("utf-8", errors="replace")
    refusal = Explorer._page_says_no(headers, html)
    if refusal:
        return {"refusal": refusal, "text": "", "suspicious": [], "links": [], "cited": [], "exchanges": []}
    document = sanitize_document(url, html, maximum_text_bytes=200_000)
    links = extract_links(url, html) if content_type != "text/plain" else []
    cited = extract_cited(url, html) if content_type != "text/plain" else []
    text = redact(document.text)
    return {"refusal": None, "text": text, "suspicious": list(document.suspicious_instructions),
            "links": links, "cited": cited, "exchanges": dialogue_exchanges(text)}


def parse_file(name: str, data: bytes) -> dict[str, Any]:
    from lantern.reading_digest import file_text

    try:
        return {"text": file_text(name, data), "error": None}
    except ValueError as error:
        return {"text": "", "error": str(error)}


def _limit() -> None:
    import resource

    for limit, value in ((resource.RLIMIT_AS, MEMORY_BYTES), (resource.RLIMIT_CPU, CPU_SECONDS),
                         (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 64)):
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass  # a lower existing limit stays


def main() -> None:
    _limit()
    request = json.loads(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1_000_000))
    data = base64.b64decode(request["data"])
    if request["kind"] == "page":
        result = parse_page(request["url"], request["content_type"], request["headers"], data)
    else:
        result = parse_file(request["name"], data)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


# -- in the parent ---------------------------------------------------------------------

_ISOLATION: list[str] | None = None


def isolation() -> list[str]:
    """`unshare -rn` (no network inside) when this system allows it, else nothing extra; probed once."""
    global _ISOLATION
    if _ISOLATION is None:
        _ISOLATION = []
        tool = shutil.which("unshare")
        if tool:
            try:
                probe = subprocess.run([tool, "-rn", "true"], capture_output=True, timeout=5, env={})
                if probe.returncode == 0:
                    _ISOLATION = [tool, "-rn"]
            except (OSError, subprocess.SubprocessError):
                pass
    return list(_ISOLATION)


def isolation_name() -> str:
    return "rlimits+no-network" if isolation() else "rlimits"


def _run(request: dict[str, Any]) -> dict[str, Any]:
    source_root = str(Path(__file__).resolve().parents[1])
    bootstrap = (f"import sys; sys.path.insert(0, {source_root!r}); "
                 "from lantern.reading_worker import main; main()")
    with tempfile.TemporaryDirectory(prefix="lantern-reader-") as scratch:
        try:
            done = subprocess.run([*isolation(), sys.executable, "-I", "-c", bootstrap],
                                  input=json.dumps(request).encode("utf-8"), capture_output=True,
                                  timeout=TIMEOUT_SECONDS, cwd=scratch, env={"LANG": "C.UTF-8"})
        except subprocess.TimeoutExpired as error:
            raise ValueError("the reading worker took too long") from error
        except OSError as error:  # it could not even start: skip the page, never read it in-process
            raise ValueError(f"the reading worker could not start ({error.__class__.__name__})") from error
    if done.returncode != 0:
        raise ValueError(f"the reading worker stopped (exit {done.returncode})")
    if len(done.stdout) > MAX_OUTPUT_BYTES:
        raise ValueError("the reading worker said too much")
    try:
        return json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("the reading worker's answer is not JSON") from error


def _strings(value: object, limit: int, count: int) -> list[str]:
    if type(value) is not list or len(value) > count or any(type(v) is not str or len(v) > limit for v in value):
        raise ValueError("the reading worker's answer is out of bounds")
    return value


def page(url: str, content_type: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    """Parse one fetched page in a disposable worker; ValueError (skip the page) on anything unexpected."""
    if len(body) > MAX_INPUT_BYTES:
        raise ValueError("page too large for the reading worker")
    result = _run({"kind": "page", "url": url, "content_type": content_type,
                   "headers": {k: v for k, v in headers.items() if k in ("x-robots-tag", "tdm-reservation", "content-type")},
                   "data": base64.b64encode(body).decode("ascii")})
    if type(result) is not dict or set(result) != _PAGE_FIELDS:
        raise ValueError("the reading worker's answer has unexpected fields")
    refusal = result["refusal"]
    if refusal is not None and (type(refusal) is not str or len(refusal) > 300):
        raise ValueError("the reading worker's refusal is out of bounds")
    if type(result["text"]) is not str or len(result["text"].encode("utf-8")) > 400_000:
        raise ValueError("the reading worker's text is out of bounds")
    links = _strings(result["links"], 2048, 5000)
    if any(not link.startswith("https://") for link in links):
        raise ValueError("the reading worker returned a non-https link")
    if any(link not in links for link in _strings(result["cited"], 2048, 5000)):
        raise ValueError("the reading worker cited a link the page does not have")
    _strings(result["suspicious"], 500, 200)
    exchanges = result["exchanges"]
    if type(exchanges) is not list or len(exchanges) > 50 or any(
            type(e) is not dict or set(e) != {"said", "reply"} or any(type(v) is not str or len(v) > 2000 for v in e.values())
            for e in exchanges):
        raise ValueError("the reading worker's exchanges are out of bounds")
    return result


def file(name: str, data: bytes) -> str:
    """The text of an owner-chosen file (txt, md, pdf), decoded in a disposable worker; ValueError when unreadable."""
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("file too large")
    result = _run({"kind": "file", "name": name[:255], "data": base64.b64encode(data).decode("ascii")})
    if type(result) is not dict or set(result) != _FILE_FIELDS:
        raise ValueError("the reading worker's answer has unexpected fields")
    if result["error"] is not None:
        raise ValueError(str(result["error"])[:300])
    if type(result["text"]) is not str or len(result["text"]) > 6_000_000:
        raise ValueError("the reading worker's text is out of bounds")
    return result["text"]
