"""Read-only exploration of the web: Lantern's library (owner direction, 2026-10-22).

"Library of Alexandria is the internet, the sum of human knowledge." The owner
wants every Lantern to read the web like an organic user, following links and
generating its own searches, without ever publishing anything, and to meet the
web in a deliberate order: neutral reference first, then spaces written by and
for minority and underprivileged communities, then the wider web, so those
perspectives form before louder voices can drown them out.

Boundaries (all enforced here, on top of the Gate 6 gateway):

- GET only: no cookies, logins, forms, uploads, posting, or script execution.
- Only HTTPS hosts in the owner's curriculum for the current stage or an earlier
  one; stages advance only by owner command.
- Lantern asks each site whether AI may read it (owner direction, 2026-10-22:
  "identify if a site wants it to be able to read it"), through machine-readable
  signals only: robots.txt rules for Lantern and for well-known AI crawlers, "noai"
  in X-Robots-Tag headers or robots meta tags, and the TDM Reservation Protocol
  (tdm-reservation header, /.well-known/tdmrep.json). Any "no" means the page is
  neither read nor stored. Each host is visited at most once per politeness
  interval; pages and bytes per session are bounded.
- Plain-language policies (owner, 2026-10-22: "can we enable plain-language
  pages?"): before reading a new site, Lantern reads its ai.txt and the terms or
  acceptable-use pages linked from its front page and looks for refusals
  (scraping or automated access prohibited, no AI training, written permission
  required). Without a language model it cannot prove a "yes", so plain language
  can only add a "no", and the exact sentence is kept for the owner. A recorded
  owner `permission` on a source (e.g. the author said yes) skips this check, but
  never robots.txt or noai signals, which belong to the site operator.
- Curated stages set the order of reading; an `open` stage (the wider web) admits
  any public site that consents by those signals, without per-site approval.
- Every address is validated as public (no private or local networks).
- Text is sanitised (active content removed, instruction-like text flagged and
  never obeyed) and personal details (emails, phone numbers, @handles) are
  redacted before anything is stored.
- Every page keeps its source, stage and community label, so Lantern learns
  *whose* perspective something is instead of flattening it into fact.
- Output is a review-only reading journal plus unapplied Gate 7 concept
  proposals; nothing is imported into memory automatically.
- No session runs without the owner's activation receipt (see explorer_cli).
"""

from __future__ import annotations

from collections import deque
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import re
from typing import Any, Callable, Iterator
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from lantern.learning.concepts import OwnerTurn, concept_proposals, concept_words
from lantern.retrieval.content import sanitize_document
from lantern.retrieval.gateway import _PinnedHTTPSConnection, _resolve_public_addresses, validate_public_https_url

USER_AGENT = "LanternReader/0.1 (+https://github.com/Amnomnommer/Lantern; read-only; honours robots.txt)"
JOURNAL_FORMAT = "lantern_reading_journal_v1"
CURRICULUM_FORMAT = "lantern_reading_curriculum_v1"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{2,30}")
_HOST = re.compile(r"[a-z0-9.-]+\.[a-z]{2,}")
# Sites that turn these crawlers away are telling AI readers "no"; Lantern listens.
AI_AGENTS = ("GPTBot", "ChatGPT-User", "CCBot", "ClaudeBot", "anthropic-ai", "Claude-Web", "Google-Extended",
             "PerplexityBot", "Bytespider", "Applebot-Extended", "meta-externalagent", "cohere-ai", "Diffbot")
_POLICY_LINK = re.compile(r"(?:terms|tos\b|legal|acceptable[-_]?use|ai[-_]?policy|conditions)", re.IGNORECASE)
_REFUSALS = (
    re.compile(r"[^.]{0,160}\b(?:scrap\w*|crawl\w*|spider\w*|harvest\w*|data[- ]mining)\b[^.]{0,120}\b(?:prohibit\w*|not (?:be )?permitted|forbidden|not allowed)[^.]{0,80}", re.IGNORECASE),
    re.compile(r"[^.]{0,160}\b(?:may not|must not|shall not|will not|prohibit\w*|forbid\w*)\b[^.]{0,120}\b(?:scrap\w*|crawl\w*|spider\w*|robots?|bots?|automated (?:means|access|systems|tools|software)|data[- ]mining)\b[^.]{0,80}", re.IGNORECASE),
    re.compile(r"[^.]{0,160}\b(?:may not|must not|shall not|prohibit\w*|without (?:our |the )?(?:prior |express )?(?:written )?(?:consent|permission))\b[^.]{0,120}\b(?:train\w*|artificial intelligence|machine learning|language models?|\bAI\b)[^.]{0,80}", re.IGNORECASE),
    re.compile(r"[^.]{0,80}\bno (?:AI|artificial intelligence)\b[^.]{0,80}", re.IGNORECASE),
)
_META_ROBOTS = re.compile(r"<meta\s+[^>]*name=[\"']?(?:robots|lanternreader)[\"']?[^>]*>", re.IGNORECASE)

Fetcher = Callable[[str, str, str, float], AbstractContextManager[Any]]
Resolver = Callable[[str], tuple[str, ...]]


def request_target(url: str) -> str:
    """The path and query for the request line, percent-encoded (HTTP request lines are ASCII).

    Already-encoded escapes are kept, so encoding is idempotent.
    """
    parsed = urlsplit(url)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&%+/:@!$'()*,;?-._~")
    return path + (f"?{query}" if query else "")


class DataAllowanceUsedUp(Exception):
    """The session's data allowance cannot fit another page."""


@contextmanager
def _reader_fetch(url: str, hostname: str, address: str, timeout: float) -> Iterator[Any]:
    """GET through the Gate 6 pinned connection: no cookies, no body, no redirects followed here."""
    target = request_target(url)
    connection = _PinnedHTTPSConnection(hostname, address, timeout)
    try:
        connection.request("GET", target, headers={
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json",
            "Accept-Encoding": "identity", "User-Agent": USER_AGENT,
        })
        yield connection.getresponse()
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class Source:
    host: str  # matches the host and its subdomains
    community: str  # whose voice this is, e.g. "reference" or a community the owner names
    search: str | None = None  # GET search URL template with {query}, if the site offers one
    skip: tuple[str, ...] = ()  # regexes of paths not worth reading (talk pages, logins, ...)
    # "html": result links are read from the page; "wikimedia": the site's documented search API
    # (JSON), meant for programmatic use, so robots.txt crawler rules do not apply to it.
    search_format: str = "html"
    # A narrowly scoped source: only paths matching `only` are read, and when `anchor`
    # is set, only works linked from that page (group 1 of the `only` pattern is the
    # work id), e.g. one author's fictions and their chapters on a large site.
    only: tuple[str, ...] = ()
    anchor: str | None = None
    # Owner-recorded permission (who said yes, and when). Skips the plain-language
    # policy check only; robots.txt and noai signals always apply.
    permission: str | None = None


@dataclass(frozen=True, slots=True)
class Stage:
    number: int
    name: str
    sources: tuple[Source, ...]
    # Open stages admit any public HTTPS site that consents to AI reading.
    open: bool = False


# A narrowly scoped source (one author on a large site); kept as the pattern for owner-named authors.
RAVENSDAGGER = Source(
    "www.royalroad.com", "Raven'sDagger (fiction; interpersonal craft)", None,
    (r"/reviews", r"/comment", r"/report", r"/follow", r"/favorite", r"/rate"),
    "html", (r"^/profile/147338/fictions$", r"^/fiction/(\d+)(?:/|$)"),
    "https://www.royalroad.com/profile/147338/fictions",
)

DEFAULT_CURRICULUM: tuple[Stage, ...] = (
    # Owner, 2026-10-22: "start Lantern off with the works of Raven'sDagger". Royal Road asks AI
    # readers not to read it, so the owner removed it on 2026-10-25; the stage stays (numbering is
    # part of every activation) and is empty until the owner names a consenting source.
    Stage(1, "Foundation texts", ()),
    Stage(2, "Neutral reference", (
        Source("en.wikipedia.org", "reference",
               "https://en.wikipedia.org/w/rest.php/v1/search/page?q={query}&limit=10",
               (r"^/wiki/[^/]*:", r"^/w/"), "wikimedia"),
        Source("simple.wikipedia.org", "reference", None, (r"^/wiki/[^/]*:", r"^/w/")),
        Source("en.wiktionary.org", "reference", None, (r"^/wiki/[^/]*:", r"^/w/")),
        Source("www.gutenberg.org", "public-domain literature", None, (r"^/ebooks/search", r"^/cache/")),
    )),
    # Stage 2 is chosen by the owner: spaces written by and for minority and
    # underprivileged communities. Empty until the owner names them.
    Stage(3, "Minority and underprivileged voices", ()),
    # Stage 4 is the wider web: any public site whose own signals say AI may read it.
    Stage(4, "The wider web", (), True),
)


def curriculum_to_json(stages: tuple[Stage, ...]) -> dict[str, object]:
    return {"format": CURRICULUM_FORMAT, "stages": [asdict(stage) for stage in stages]}


def curriculum_from_json(value: object) -> tuple[Stage, ...]:
    if type(value) is not dict or value.get("format") != CURRICULUM_FORMAT:
        raise ValueError("unsupported reading curriculum")
    stages = []
    for item in value.get("stages", []):
        sources = []
        for source in item["sources"]:
            host = str(source["host"]).casefold()
            if not _HOST.fullmatch(host):
                raise ValueError(f"invalid curriculum host: {host}")
            search = source.get("search")
            if search is not None and (not str(search).startswith(f"https://{host}/") or "{query}" not in str(search)):
                raise ValueError(f"search for {host} must be an https URL on that host with {{query}}")
            for pattern in source.get("skip", []):
                re.compile(pattern)
            search_format = str(source.get("search_format", "html"))
            if search_format not in {"html", "wikimedia"}:
                raise ValueError(f"unknown search format for {host}")
            only = tuple(source.get("only", []))
            for pattern in only:
                re.compile(pattern)
            anchor = source.get("anchor")
            if anchor is not None and not str(anchor).startswith(f"https://{host}/"):
                raise ValueError(f"anchor for {host} must be an https URL on that host")
            permission = source.get("permission")
            if permission is not None and len(str(permission).strip()) < 10:
                raise ValueError(f"permission for {host} must say who agreed and when")
            sources.append(Source(host, str(source["community"]), search, tuple(source.get("skip", [])), search_format,
                                  only, anchor, permission))
        stages.append(Stage(int(item["number"]), str(item["name"]), tuple(sources), bool(item.get("open", False))))
    if [s.number for s in stages] != list(range(1, len(stages) + 1)):
        raise ValueError("curriculum stages must be numbered 1, 2, 3, ...")
    return tuple(stages)


_QUOTE = re.compile(r'[“"]([^”"]{2,300})[”"]')
_SPOKEN = re.compile(r"[.!?…]$")
# A script or transcript line: "HAMLET. To be…", "NURSE: Madam!", "Anna: Hello." (a name, then the words).
_SCRIPT = re.compile(r"^[ \t]*(?:([A-Z][A-Z'’ -]{1,30})[.:]|([A-Z][a-z'’]+(?: [A-Z][a-z'’]+)?):)[ \t]+([^ \t\n\x0b\f\r].*)$")
_BLANK = re.compile(r"[ \t\n\x0b\f\r]+")  # ASCII whitespace, as on the phone
_NOT_SPEAKERS = {"chapter", "act", "scene", "book", "part", "volume", "note", "notes", "contents", "the end", "prologue",
                 "epilogue", "introduction", "preface", "appendix", "index", "figure", "table", "source", "see also"}
_DIRECTION = re.compile(r"\[[^\]]*\]|\([^)]*\)")
SAME_SPEAKER_GAP = 120  # "Hello," she said. "How are you?" is one speaker going on
REPLY_GAP = 200  # a reply comes soon after what it answers


def _squash(text: str) -> str:
    return " ".join(w for w in _BLANK.split(text.strip()) if w)


def _utterances(text: str) -> list[tuple[int, int, str]]:
    """Quoted speech as (start, end, words), joining one speaker's lines split by "she said"."""
    found: list[tuple[int, int, str]] = []
    for match in _QUOTE.finditer(text):
        words = _squash(match.group(1))
        if found and found[-1][2].endswith(",") and match.start() - found[-1][1] <= SAME_SPEAKER_GAP:
            start, _, before = found.pop()
            found.append((start, match.end(), before[:-1] + ". " + words))
        else:
            found.append((match.start(), match.end(), words))
    return [(s, e, w[:-1] + "." if w.endswith(",") else w) for s, e, w in found]


def _script_pairs(text: str) -> list[dict[str, str]]:
    lines = []
    for line in text.split("\n"):
        match = _SCRIPT.match(line.removesuffix("\r"))
        if match is None:
            lines.append(None)
            continue
        speaker = (match.group(1) or match.group(2)).strip()
        words = _squash(_DIRECTION.sub(" ", match.group(3)))
        heading = speaker.casefold() in _NOT_SPEAKERS or speaker.split()[0].casefold() in _NOT_SPEAKERS
        lines.append(None if heading or not words else (speaker.casefold(), words))
    return [{"said": a[1], "reply": b[1]} for a, b in zip(lines, lines[1:])
            if a is not None and b is not None and a[0] != b[0] and _SPOKEN.search(a[1]) and _SPOKEN.search(b[1])]


def dialogue_exchanges(text: str, limit: int = 50) -> list[dict[str, str]]:
    """Said -> reply pairs from dialogue: raw material for learning how people talk.

    Two kinds, both simple and inspectable (it never knows who spoke for certain, so every
    pair is only material for the Gate 7 phrasebook trial, never a rule Lantern follows):
    - quoted speech in stories: one speaker's lines split by "she said" are joined, and the
      next quote soon after is the reply;
    - scripts and transcripts: consecutive "NAME. words" / "Name: words" lines from two speakers,
      with stage directions in brackets left out.
    Shared with the phone (`dialogue-vectors-v1.json`).
    """
    spoken = [u for u in _utterances(text) if _SPOKEN.search(u[2])]
    pairs = [{"said": a[2], "reply": b[2]} for a, b in zip(spoken, spoken[1:])
             if b[0] - a[1] <= REPLY_GAP and max(len(a[2].split(" ")), len(b[2].split(" "))) >= 3]
    seen: set[tuple[str, str]] = set()
    kept = []
    for pair in pairs + _script_pairs(text):
        key = (pair["said"].casefold(), pair["reply"].casefold())
        if key not in seen:
            seen.add(key)
            kept.append(pair)
    return kept[:limit]


MAX_HOSTS_SEEN = 300
MAX_SITE_LINKS = 20
_ENGLISH = frozenset({"the", "and", "of", "to", "in", "is", "that", "for", "it", "with", "as", "was", "on", "are", "be",
                      "by", "this", "from", "or", "an", "at", "which", "have", "not", "but", "they", "his", "her", "you"})


def looks_english(text: str) -> bool:
    """Whether a page reads as English: common English words make up a fair share of its first 2,000 words.

    Words in every script count (owner's report, 2026-10-30): an Armenian page whose reference list cites English
    titles once passed, because only its Latin-letter words were counted, and Prime learned the citations."""
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", text[:20_000].casefold())[:2000]
    if len(words) < 50 or sum(1 for w in words if not w.isascii()) > len(words) // 5:
        return False  # mostly another script
    return sum(1 for w in words if w in _ENGLISH) / len(words) >= 0.15


def redact(text: str) -> str:
    """Remove personal contact details before anything is stored."""
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return _HANDLE.sub("[handle]", text)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.cited: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            values = {name.casefold(): value or "" for name, value in attrs}
            if values.get("href"):
                self.links.append(values["href"])
                # A source the article itself cites (MediaWiki marks these "external"), not a menu or footer link.
                if "external" in values.get("class", "").split() and "license" not in values.get("rel", ""):
                    self.cited.append(values["href"])


def _https(base: str, hrefs: list[str]) -> list[str]:
    found: list[str] = []
    for href in hrefs:
        parts = urlsplit(urljoin(base, href.strip()))
        if parts.scheme != "https" or not parts.hostname:
            continue
        url = urlunsplit(("https", parts.netloc.casefold(), parts.path or "/", parts.query, ""))
        if url not in found:
            found.append(url)
    return found


def extract_links(base: str, html: str) -> list[str]:
    parser = _Links()
    parser.feed(html)
    return _https(base, parser.links)


def extract_cited(base: str, html: str) -> list[str]:
    """The external sources a page cites, in order."""
    parser = _Links()
    parser.feed(html)
    return _https(base, parser.cited)


# Any wiki's front page and its non-article pages (menus, talk, users, files) teach nothing;
# the first live Gate 6 trial read Wikipedia's Main Page twice. Identical in PhoneReader.kt.
# Disambiguation pages list unrelated things that share a name ("Nile" the river, the musician, the wrestler).
WIKI_FURNITURE = (r"^/(?:wiki/(?:Main_Page|Special:|Talk:|User:|User_talk:|Wikipedia:|Help:|Portal:|Template:|File:|"
                  r"Category:|Draft:|MediaWiki:)|w/index\.php|wiki/[^?#]*(?:_\(disambiguation\)|_%28disambiguation%29)(?:[?#]|$))")


# Sites run by one organisation are one origin: what they say is not independent.
SHARED_ORIGINS = {
    "wikimedia": ("wikipedia.org", "wiktionary.org", "wikimedia.org", "wikibooks.org", "wikiquote.org",
                  "wikisource.org", "wikiversity.org", "wikivoyage.org", "wikidata.org", "wikinews.org",
                  "wikifunctions.org", "mediawiki.org", "toolforge.org", "wmflabs.org"),
}


_LANGUAGE_LABEL = re.compile(r"[a-z]{2,3}(?:-[a-z]{2,8})*")


def other_edition(host: str, reading_host: str) -> bool:
    """Another language edition of the site being read ("ar.wikipedia.org" from "en.wikipedia.org").

    Lantern's reading rules understand English only, so those pages would teach nothing.
    """
    first, _, rest = host.casefold().partition(".")
    reading_first, _, reading_rest = reading_host.casefold().partition(".")
    site = rest.split(".")
    # The rest must be a whole site ("wikipedia.org"), never a bare suffix ("co.uk", or "example").
    return (rest == reading_rest and len(site) >= 2 and len(site[0]) > 3 and first != reading_first
            and first not in ("en", "simple", "www") and _LANGUAGE_LABEL.fullmatch(first) is not None)


def origin(host: str) -> str:
    """Who stands behind a host: a known organisation, else its last two labels ("www.gutenberg.org" -> "gutenberg.org")."""
    host = host.casefold()
    if host.startswith("your file "):
        return "owner file"
    for name, domains in SHARED_ORIGINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return ".".join(host.split(".")[-2:])


@dataclass
class Page:
    url: str
    stage: int
    community: str
    retrieved_at: str
    sha256: str
    text: str
    links_found: int
    suspicious_instructions: list[str]
    via: str  # "seed", "link", "search" or "redirect"
    exchanges: list[dict[str, str]] = field(default_factory=list)
    trusted_as_instructions: bool = False
    english: bool | None = None  # whether the page reads as English (sites that do are kept for later visits)


@dataclass
class Journal:
    stage: int
    started_at: str
    pages: list[Page] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    time_allowance_seconds: float | None = None
    stopped_because: str = "finished"
    isolation: str = ""  # how pages were parsed: a disposable worker with or without network
    # Other sites this reading saw linked but did not visit, and unread links on the sites it did read:
    # material for later discovery (tier 2) and return visits (tier 3) in `lantern.reading_priority`.
    hosts_seen: list[str] = field(default_factory=list)
    site_links: dict[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "format": JOURNAL_FORMAT, "stage": self.stage, "started_at": self.started_at,
            "pages": [asdict(page) for page in self.pages], "skipped": self.skipped, "searches": self.searches,
            "time_allowance_seconds": self.time_allowance_seconds, "stopped_because": self.stopped_because,
            "isolation": self.isolation, "hosts_seen": self.hosts_seen, "site_links": self.site_links,
            "safety": {
                "network_writes_performed": False, "methods_used": ["GET"], "cookies_sent": False,
                "authenticated": False, "forms_submitted": False, "scripts_executed": False,
                "personal_details_redacted": True, "authoritative_state_accessed": False,
                "automatic_import_available": False, "web_content_trusted_as_instructions": False,
            },
            "review_required": True,
        }


class Explorer:
    """One bounded, read-only reading session within the owner's curriculum."""

    def __init__(
        self,
        curriculum: tuple[Stage, ...],
        stage: int,
        *,
        fetcher: Fetcher = _reader_fetch,
        resolver: Resolver = _resolve_public_addresses,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], str] | None = None,
        max_pages: int = 25,
        max_seconds: float | None = None,  # the owner's internet-time allowance
        max_bytes: int = 500_000_000,  # the session's data allowance
        page_bytes: int = 3_000_000,
        politeness_seconds: float = 2.0,
        timeout_seconds: float = 15.0,
        progress: Callable[[str], None] | None = None,
        already_read: frozenset[str] = frozenset(),
        parse: Callable[[str, str, dict[str, str], bytes], dict[str, Any]] | None = None,
        new_hosts_first: int = 0,  # per page, cited sources from origins not read yet that jump the queue (Gate 6 trial)
    ) -> None:
        if not 1 <= stage <= len(curriculum):
            raise ValueError("unknown curriculum stage")
        if not 1 <= max_pages <= 500 or not 1 <= page_bytes <= max_bytes <= 2_000_000_000:
            raise ValueError("reading budgets are out of bounds")
        import time
        self.sources = [s for st in curriculum if st.number <= stage for s in st.sources]
        self.stage_of = {s.host: st.number for st in curriculum if st.number <= stage for s in st.sources}
        self.open_stage = next((st.number for st in curriculum if st.number <= stage and st.open), None)
        self.tdm: dict[str, list[dict[str, object]]] = {}
        self.policies: dict[str, str | None] = {}  # host -> refusal sentence, or None
        if not self.sources and self.open_stage is None:
            raise PermissionError("no sources are open at this stage")
        self.stage = stage
        self.fetcher, self.resolver = fetcher, resolver
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.now = now or (lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        self.max_pages, self.max_bytes, self.page_bytes = max_pages, max_bytes, page_bytes
        if max_seconds is not None and not 1 <= max_seconds <= 4 * 60 * 60:
            raise ValueError("internet time must be between one second and four hours")
        self.max_seconds = max_seconds
        self.politeness, self.timeout = politeness_seconds, timeout_seconds
        self.remaining = max_bytes
        self.last_visit: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser | None] = {}
        self.anchored: dict[str, set[str]] = {}  # anchor URL -> work ids it links to
        self.progress = progress or (lambda _line: None)
        self.already_read = already_read  # pages read in earlier sessions are never read again
        self.new_hosts_first = new_hosts_first
        self.origins_reached: set[str] = set()
        from lantern import reading_worker

        # Hostile content is parsed in a disposable worker; tests may pass the in-process parser.
        self.parse = parse or reading_worker.page
        self.isolation = "in-process (test)" if parse is not None else reading_worker.isolation_name()

    def source_for(self, url: str) -> Source | None:
        host = (urlsplit(url).hostname or "").casefold()
        for source in self.sources:
            if host == source.host or host.endswith("." + source.host):
                return source
        if self.open_stage is not None and _HOST.fullmatch(host):
            return Source(host, f"open web ({host})")
        return None

    def _stage(self, source: Source) -> int:
        return self.stage_of.get(source.host, self.open_stage or self.stage)

    def _allowed(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        source = self.source_for(url)
        if parts.scheme != "https" or source is None:
            return False, "outside the curriculum"
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        if any(re.search(pattern, target) for pattern in source.skip) or re.search(WIKI_FURNITURE, target):
            return False, "path skipped by curriculum"
        if source.only:
            matches = [m for m in (re.search(pattern, parts.path) for pattern in source.only) if m]
            if not matches:
                return False, "outside this source's scope"
            work = next((m.group(1) for m in matches if m.re.groups), None)
            if source.anchor and url != source.anchor:
                if source.anchor not in self.anchored:
                    return False, "anchor page not read yet"
                if work is not None and work not in self.anchored[source.anchor]:
                    return False, "not one of the anchored works"
        robots = self._robots(parts.hostname or "")
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            return False, "disallowed by robots.txt"
        if robots is not None and any(not robots.can_fetch(agent, url) for agent in AI_AGENTS):
            return False, "the site asks AI readers not to read this"
        if self._tdm_reserved(parts.hostname or "", parts.path or "/"):
            return False, "text and data mining reserved (TDMRep)"
        if source.permission is None:
            refusal = self._policy_refusal(parts.hostname or "")
            if refusal:
                return False, f"the site's own policy says no: \"{refusal}\""
        return True, ""

    def _policy_text(self, url: str) -> str | None:
        robots = self._robots(urlsplit(url).hostname or "")
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            return None
        try:
            status, headers, body = self._get(url)
        except (OSError, ValueError, PermissionError):
            return None
        return body.decode("utf-8", errors="replace") if status == 200 else None

    def _policy_refusal(self, hostname: str) -> str | None:
        """Read ai.txt and linked terms pages once per host; return the first refusal sentence."""
        if hostname in self.policies:
            return self.policies[hostname]
        self.policies[hostname] = None  # reading policy pages never recurses into this check
        refusal: str | None = None
        ai_txt = self._policy_text(f"https://{hostname}/ai.txt")
        if ai_txt:
            robots = RobotFileParser()
            robots.parse(ai_txt.splitlines())
            if not robots.can_fetch(USER_AGENT, f"https://{hostname}/") or not robots.can_fetch("*", f"https://{hostname}/"):
                refusal = "ai.txt disallows AI use"
        if refusal is None:
            front = self._policy_text(f"https://{hostname}/") or ""
            pages = [link for link in extract_links(f"https://{hostname}/", front)
                     if urlsplit(link).hostname == hostname and _POLICY_LINK.search(urlsplit(link).path)][:2]
            for page in pages:
                text = sanitize_document(page, self._policy_text(page) or "", maximum_text_bytes=200_000).text
                for pattern in _REFUSALS:
                    match = pattern.search(text)
                    if match:
                        refusal = " ".join(match.group(0).split())[:300]
                        break
                if refusal:
                    break
        self.policies[hostname] = refusal
        return refusal

    def _tdm_reserved(self, hostname: str, path: str) -> bool:
        if hostname not in self.tdm:
            rules: list[dict[str, object]] = []
            try:
                status, headers, body = self._get(f"https://{hostname}/.well-known/tdmrep.json")
                if status == 200:
                    value = json.loads(body.decode("utf-8"))
                    rules = [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []
            except (OSError, ValueError, PermissionError):
                rules = []
            self.tdm[hostname] = rules
        for rule in self.tdm[hostname]:
            location = str(rule.get("location", ""))
            prefix = location.rstrip("*")
            if str(rule.get("tdm-reservation")) == "1" and (location in ("", "/", "/*") or path.startswith(prefix)):
                return True
        return False

    @staticmethod
    def _page_says_no(headers: dict[str, str], html: str) -> str | None:
        """Per-page opt-outs: noai in X-Robots-Tag or robots meta, or a TDM reservation header."""
        if "noai" in headers.get("x-robots-tag", "").casefold():
            return "the page asks AI readers not to use it (X-Robots-Tag)"
        if headers.get("tdm-reservation", "").strip() == "1":
            return "text and data mining reserved (tdm-reservation header)"
        for tag in _META_ROBOTS.findall(html[:200_000]):
            if re.search(r"content=[\"']?[^\"'>]*\bnoai\b", tag, re.IGNORECASE):
                return "the page asks AI readers not to use it (robots meta)"
        return None

    def _get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        hostname, addresses = validate_public_https_url(url, resolver=self.resolver)
        wait = self.politeness - (self.clock() - self.last_visit.get(hostname, -1e9))
        if wait > 0:
            self.sleep(wait)
        self.last_visit[hostname] = self.clock()
        with self.fetcher(url, hostname, addresses[0], self.timeout) as response:
            status = int(response.status)
            headers = {k.casefold(): v for k, v in response.headers.items()}
            limit = min(self.page_bytes, self.remaining)
            body = response.read(limit + 1) if status == 200 else b""
        if len(body) > limit:
            if limit == self.remaining:
                raise DataAllowanceUsedUp
            raise ValueError("page too large")
        self.remaining -= len(body)
        return status, headers, body

    def _robots(self, hostname: str) -> RobotFileParser | None:
        if hostname not in self.robots:
            parser = RobotFileParser()
            try:
                status, _, body = self._get(f"https://{hostname}/robots.txt")
                parser.parse(body.decode("utf-8", errors="replace").splitlines() if status == 200 else [])
            except (OSError, ValueError, PermissionError):
                parser.parse([])
            self.robots[hostname] = parser
        return self.robots[hostname]

    def search_url(self, query: str) -> str | None:
        for source in self.sources:
            if source.search:
                return source.search.replace("{query}", quote(query))
        return None

    def _search_results(self, url: str, journal: Journal) -> list[str]:
        """A documented search API: read result titles into article links."""
        try:
            status, headers, body = self._get(url)
            if status != 200 or not headers.get("content-type", "").startswith("application/json"):
                raise ValueError(f"search returned HTTP {status}")
            pages = json.loads(body.decode("utf-8")).get("pages", [])
        except (OSError, ValueError, PermissionError, AttributeError) as error:
            journal.skipped.append({"url": url, "reason": f"search failed: {error}"})
            return []
        host = urlsplit(url).hostname
        return [f"https://{host}/wiki/{quote(str(p['key']).replace(' ', '_'))}" for p in pages[:10]
                if isinstance(p, dict) and isinstance(p.get("key"), str)]

    def _anchor_works(self, source: Source, links: list[str]) -> set[str]:
        works = set()
        for link in links:
            path = urlsplit(link).path
            for pattern in source.only:
                match = re.search(pattern, path)
                if match and match.re.groups:
                    works.add(match.group(1))
        return works

    @staticmethod
    def _note_sites(reading_host: str, links: list[str], seen: set[str], journal: Journal) -> None:
        own = journal.site_links.setdefault(reading_host, [])
        for link in links:
            host = urlsplit(link).hostname or ""
            if host and host != reading_host and host not in journal.hosts_seen and len(journal.hosts_seen) < MAX_HOSTS_SEEN:
                journal.hosts_seen.append(host)
            elif host == reading_host and link not in seen and link not in own and len(own) < MAX_SITE_LINKS:
                own.append(link)

    def explore(self, seeds: tuple[str, ...], *, searches: tuple[str, ...] = ()) -> Journal:
        """Breadth-first reading from seeds and generated searches, within all budgets."""
        journal = Journal(self.stage, self.now(), isolation=self.isolation)
        # Anchored sources are read from their anchor page first.
        anchors = tuple(s.anchor for s in self.sources if s.anchor and s.anchor not in seeds)
        frontier: deque[tuple[str, str]] = deque((url, "seed") for url in anchors + seeds)
        for query in searches:
            url = self.search_url(query)
            if url:
                journal.searches.append(query)
                frontier.append((url, "search"))
        seen: set[str] = set()
        deadline = None if self.max_seconds is None else self.clock() + self.max_seconds
        journal.time_allowance_seconds = self.max_seconds
        try:
            self._read_frontier(frontier, seen, deadline, journal)
        except KeyboardInterrupt:
            # The owner stopped reading early: keep everything read so far.
            journal.stopped_because = "stopped by the owner"
        self._report_skips(journal)
        self.progress(f"finished: {len(journal.pages)} page(s) read, {len(journal.skipped)} skipped ({journal.stopped_because})")
        return journal

    def _report_skips(self, journal: Journal) -> None:
        reported = getattr(self, "_skips_reported", 0)
        for item in journal.skipped[reported:]:
            # Menus, talk pages and other curriculum skips are routine; they are counted, not listed.
            if item["reason"] not in ("path skipped by curriculum", "already read"):
                self.progress(f"skipped {item['url']}: {item['reason']}")
        self._skips_reported = len(journal.skipped)

    def _read_frontier(self, frontier: deque[tuple[str, str]], seen: set[str], deadline: float | None,
                       journal: Journal) -> None:
        while frontier and len(journal.pages) < self.max_pages and self.remaining > 0:
            self._report_skips(journal)
            if deadline is not None and self.clock() >= deadline:
                journal.stopped_because = "internet time used up"
                break
            url, via = frontier.popleft()
            if url in seen:
                continue
            seen.add(url)
            if url in self.already_read:
                journal.skipped.append({"url": url, "reason": "already read"})
                continue
            source = self.source_for(url)
            if via == "search" and source is not None and source.search_format == "wikimedia":
                for link in self._search_results(url, journal):
                    frontier.append((link, "link"))
                continue
            ok, reason = self._allowed(url)
            if not ok:
                journal.skipped.append({"url": url, "reason": reason})
                continue
            try:
                status, headers, body = self._get(url)
            except DataAllowanceUsedUp:
                journal.stopped_because = "data allowance used up"
                break
            except (OSError, ValueError, PermissionError) as error:
                journal.skipped.append({"url": url, "reason": f"fetch refused: {error}"})
                continue
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                frontier.appendleft((urljoin(url, headers["location"]).split("#")[0], "redirect"))
                continue
            content_type = headers.get("content-type", "").split(";")[0].strip().casefold()
            if status != 200 or content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                journal.skipped.append({"url": url, "reason": f"HTTP {status} {content_type}".strip()})
                continue
            try:  # hostile content is parsed in a disposable worker (Gate 6 hardening)
                parsed = self.parse(url, content_type, headers, body)
            except ValueError as error:
                journal.skipped.append({"url": url, "reason": f"parser refused: {error}"})
                continue
            if parsed["refusal"]:
                journal.skipped.append({"url": url, "reason": parsed["refusal"]})
                continue  # neither stored nor learned from
            links = parsed["links"]
            source = self.source_for(url)
            if source.anchor == url:
                self.anchored[url] = self._anchor_works(source, links)
            text = parsed["text"]
            journal.pages.append(Page(
                url, self._stage(source), source.community, self.now(),
                hashlib.sha256(body).hexdigest(), text, len(links), parsed["suspicious"], via,
                parsed["exchanges"], english=looks_english(text),
            ))
            self.progress(f"read {len(journal.pages)}: {url} ({len(body) // 1024} kB, {source.community})")
            reading_host = urlsplit(url).hostname or ""
            self.origins_reached.add(origin(reading_host))
            self._note_sites(reading_host, links, seen, journal)
            links = [link for link in links if not other_edition(urlsplit(link).hostname or "", reading_host)]
            jumped: list[str] = []
            kept = set(links)
            for link in [c for c in parsed.get("cited", []) if c in kept] if self.new_hosts_first else []:
                # Independent voices on the same subject: a few cited sources from organisations not read yet
                # are read next (other language editions of one site are the same organisation).
                link_origin = origin(urlsplit(link).hostname or "")
                if (len(jumped) < self.new_hosts_first and link not in seen and link_origin not in self.origins_reached
                        and self.source_for(link) is not None):
                    self.origins_reached.add(link_origin)
                    jumped.append(link)
            for link in reversed(jumped):
                frontier.appendleft((link, "cited"))
            for link in links:
                if link not in seen and link not in jumped and self.source_for(link) is not None:
                    frontier.append((link, "link"))


# Words from site menus seen in Workstation journals (the phone's reader keeps only article text).
_MENU = frozenset({"additional", "content", "contents", "personal", "tools", "create", "navigation", "views",
                   "variants", "appearance", "text", "move", "sidebar", "hide", "toggle", "terms", "policy", "privacy",
                   "contributions", "learn", "read", "view", "source", "history", "page", "pages", "wikipedia", "free",
                   "encyclopedia", "jump", "main", "menu", "search", "donate", "account", "accounts", "actions",
                   "action", "log", "edit", "links", "link", "upload", "file", "files", "special", "what",
                   "date", "later", "retrieved", "archived", "original",
                   # The licence footer ("available under the Creative Commons Attribution-ShareAlike License;
                   # additional terms may apply"), seen as curiosity in the first live Gate 6 trial (2026-10-24).
                   "apply", "available", "attribution", "sharealike", "attribution-sharealike", "license", "licence", "licensed", "creative", "commons", "trademark", "registered", "non-profit", "agree", "disclaimers", "cookie"})


def is_idea(word: str) -> bool:
    """Whether a word is worth searching for (not a menu or filler word)."""
    from lantern.companion import FURNITURE, GENERIC
    return word not in FURNITURE and word not in GENERIC and word not in _MENU


def next_searches(journal_pages: list[dict[str, object]], known: set[str], limit: int = 3) -> tuple[str, ...]:
    """Curiosity: words that recur across what was read but have not been read about yet."""
    # Menu words ("account", "actions") and filler are not ideas; the phone filters them the same way.
    counts: dict[str, int] = {}
    for page in journal_pages:
        for word in set(concept_words(str(page.get("text", "")))):
            if not is_idea(word):
                continue
            counts[word] = counts.get(word, 0) + 1
    fresh = [w for w, n in counts.items() if n >= 2 and w not in known]
    fresh.sort(key=lambda w: (-counts[w], w))
    return tuple(fresh[:limit])


def reading_proposals(journal: dict[str, object]) -> list[dict[str, object]]:
    """Unapplied Gate 7 concept proposals from one journal, grouped by community.

    Only the first 5,000 characters of each page count: co-occurrence is quadratic
    in distinct words, and long chapters made the phone port freeze (2026-10-22).
    """
    by_community: dict[str, list[dict[str, object]]] = {}
    for page in journal["pages"]:
        by_community.setdefault(str(page["community"]), []).append(page)
    proposals = []
    for community, pages in sorted(by_community.items()):
        turns = tuple(OwnerTurn(str(p["sha256"]), str(p["text"])[:5_000], str(p["retrieved_at"])) for p in pages)
        for proposal in concept_proposals(f"Reading {community}", turns):
            after = dict(proposal.after)
            proposals.append({
                **asdict(proposal), "community": community,
                "reason": f"'{after['term']}' appeared in {after['mentions']} pages read from {community} sources.",
            })
    return proposals


def journal_digest(journal: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(journal, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
