"""Active-content removal and prompt-injection marking for Gate 6."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re


_SUSPICIOUS = (
    re.compile(r"ignore (?:all |the )?(?:previous|prior) instructions", re.IGNORECASE),
    re.compile(r"(?:system|developer) prompt", re.IGNORECASE),
    re.compile(r"(?:execute|run) (?:this |the )?(?:command|code|script)", re.IGNORECASE),
    re.compile(r"(?:send|upload|reveal|exfiltrate).{0,40}(?:secret|password|memory|key)", re.IGNORECASE),
    re.compile(r"you are (?:now|an?)", re.IGNORECASE),
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked = 0
        self.parts: list[str] = []
        self.active_content_found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "iframe", "object", "embed", "form"}:
            self._blocked += 1
            self.active_content_found = True
        if any(name.casefold().startswith("on") for name, _ in attrs):
            self.active_content_found = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "iframe", "object", "embed", "form"} and self._blocked:
            self._blocked -= 1

    def handle_data(self, data: str) -> None:
        if not self._blocked and data.strip():
            self.parts.append(data.strip())


@dataclass(frozen=True, slots=True)
class SanitizedDocument:
    source_url: str
    text: str
    active_content_removed: bool
    suspicious_instructions: tuple[str, ...]
    trusted_as_instructions: bool = False


def sanitize_document(source_url: str, html: str, *, maximum_text_bytes: int = 200_000) -> SanitizedDocument:
    if not source_url.startswith(("https://", "http://")):
        raise ValueError("retrieved content requires an explicit HTTP source")
    if maximum_text_bytes < 1:
        raise ValueError("text budget must be positive")
    parser = _TextExtractor()
    parser.feed(html)
    text = " ".join(parser.parts)
    encoded = text.encode("utf-8")
    if len(encoded) > maximum_text_bytes:
        encoded = encoded[:maximum_text_bytes]
        text = encoded.decode("utf-8", errors="ignore")
    suspicious = tuple(
        pattern.pattern for pattern in _SUSPICIOUS if pattern.search(text)
    )
    return SanitizedDocument(
        source_url,
        text,
        parser.active_content_found,
        suspicious,
    )
