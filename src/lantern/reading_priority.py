"""The priority searcher for internet time (owner, 2026-10-25): what a reading session reads, in tiers.

When the owner gives internet time without naming seeds or searches:
1. **Curiosity** — what Lantern saved to wonder about (`wonders.json`: gaps a conversation left and
   research threads). These are searched first, and each is taken off the list once it has been used.
2. **Discovery** — plain browsing, no API: a random site from those Lantern has seen linked but never
   visited. Its front page is read through the normal consent checks; when the site lets Lantern read
   and its page reads as English, the site joins Lantern's list of working sites. A site that refuses
   or is not English is remembered so it is not tried again.
3. **Return visits** — a random unread page on one of the working sites.

(The owner first suggested random IPv4 addresses for tier 2. Random addresses are mostly routers and
devices, HTTPS needs a site's name, and probing them is scanning; discovering sites through links Lantern
has seen gives the same "find something new at random" without any of that.)

Randomness comes from Python's `random.SystemRandom`, so no two sessions are alike; tests pass their own
`random.Random`. Discovery and return visits only happen at an open curriculum stage (the wider web),
because earlier stages admit only their named sources. Everything still goes through the explorer's
consent rules (robots.txt, AI-crawler rules, noai, TDMRep, plain-language policies) and budgets.
"""

from __future__ import annotations

import json
from pathlib import Path
import random
import re
from typing import Sequence

from lantern.explorer import _ENGLISH, is_idea

SITES = "sites.json"
CURIOSITY = 3
DISCOVER = 2
RETURN = 2
MAX_SEEN = 2000
MAX_WORKING = 500
MAX_TRIED = 4000


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_sites(root: Path) -> dict[str, object]:
    sites = _read(root / SITES, {})
    if not isinstance(sites, dict):
        sites = {}
    sites.setdefault("format", "lantern_sites_v1")
    sites.setdefault("seen", [])  # hosts seen linked, never visited
    sites.setdefault("working", {})  # host -> unread links on it
    sites.setdefault("tried", {})  # host -> why it is not a working site ("not English", a refusal)
    # Another language's edition of a wiki is never a working site (owner's report, 2026-10-30: Prime learned the
    # English citation titles on hy.wikipedia.org); it is set aside wherever it was listed.
    for host in [h for h in list(sites["working"]) + list(sites["seen"]) if other_language_wiki(str(h))]:
        sites["working"].pop(host, None)  # type: ignore[union-attr]
        sites["tried"][host] = "another language's edition"  # type: ignore[index]
    sites["seen"] = [h for h in sites["seen"] if not other_language_wiki(str(h))]  # type: ignore[union-attr]
    return sites


_WIKI = re.compile(r"(?P<language>[a-z]{2,3}(?:-[a-z]+)*)\.(?:m\.)?(?:wikipedia|wiktionary|wikibooks|wikiquote|wikisource|"
                   r"wikinews|wikiversity|wikivoyage)\.org")


def other_language_wiki(host: str) -> bool:
    match = _WIKI.fullmatch(host)
    return match is not None and match["language"] not in ("en", "simple")


def plan(root: Path, known_searches: set[str], *, open_web: bool, rng: random.Random | None = None
         ) -> dict[str, object]:
    """The tiers for one session: searches (tier 1) and seeds (tiers 2 and 3), with why each was chosen."""
    rng = rng or random.SystemRandom()
    wonders = [w for w in _read(root / "wonders.json", []) if isinstance(w, str)]
    curiosity = [w for w in dict.fromkeys(wonders)
                 if w not in known_searches and w not in _ENGLISH and is_idea(w.split()[-1])][:CURIOSITY]
    sites = load_sites(root)
    discover: list[str] = []
    revisit: list[str] = []
    if open_web:
        unvisited = [h for h in sites["seen"] if h not in sites["working"] and h not in sites["tried"]]
        discover = rng.sample(unvisited, min(DISCOVER, len(unvisited)))
        working = sorted(h for h, links in sites["working"].items() if links)
        for host in rng.sample(working, min(RETURN, len(working))):
            revisit.append(rng.choice(sites["working"][host]))
    return {"searches": curiosity, "seeds": [f"https://{h}/" for h in discover] + revisit,
            "tiers": {"curiosity": curiosity, "discovery": discover, "return": revisit}}


def after_reading(root: Path, journal: dict[str, object], used_searches: Sequence[str]) -> dict[str, object]:
    """Take used curiosity off the list, and learn which sites work from what this session read."""
    wonders_path = root / "wonders.json"
    wonders = [w for w in _read(wonders_path, []) if isinstance(w, str)]
    used = set(used_searches)
    if used and any(w in used for w in wonders):
        _write(wonders_path, [w for w in wonders if w not in used])
    sites = load_sites(root)
    working: dict[str, list[str]] = sites["working"]  # type: ignore[assignment]
    tried: dict[str, str] = sites["tried"]  # type: ignore[assignment]
    read_urls = set()
    added = []
    from urllib.parse import urlsplit
    for page in journal.get("pages", []):
        url = str(page.get("url", ""))
        read_urls.add(url)
        host = urlsplit(url).hostname or ""
        if not host or page.get("suspicious_instructions"):
            continue
        if page.get("english") is True:
            if host not in working and len(working) < MAX_WORKING:
                working[host] = []
                added.append(host)
            tried.pop(host, None)
        elif page.get("english") is False:
            working.pop(host, None)  # a working site whose pages turn out not to be English is set aside
            tried[host] = "not English"
    for item in journal.get("skipped", []):
        host = urlsplit(str(item.get("url", ""))).hostname or ""
        reason = str(item.get("reason", ""))
        front = urlsplit(str(item.get("url", ""))).path in ("", "/")
        if host and front and host not in working and any(word in reason for word in
                                                          ("robots", "AI readers", "policy", "mining", "noai")):
            tried[host] = reason[:120]
    for host, links in dict(journal.get("site_links", {})).items():
        if host in working:
            fresh = [link for link in links if link not in read_urls and link not in working[host]]
            working[host] = (working[host] + fresh)[-20:]
    for host in working:
        working[host] = [link for link in working[host] if link not in read_urls]
    seen = [h for h in sites["seen"] if h not in working and h not in tried]
    for host in journal.get("hosts_seen", []):
        if isinstance(host, str) and host not in working and host not in tried and host not in seen:
            seen.append(host)
    sites["seen"] = seen[-MAX_SEEN:]
    sites["tried"] = dict(list(tried.items())[-MAX_TRIED:])
    _write(root / SITES, sites)
    return {"working_added": added, "working": len(working), "seen": len(sites["seen"])}


def explain(root: Path) -> str:
    sites = load_sites(root)
    working = sites["working"]
    lines = [f"My sites: {len(working)} working (English, and they let me read), "
             f"{len(sites['seen'])} seen but not visited yet, {len(sites['tried'])} tried and set aside."]
    if working:
        lines.append("Some I go back to: " + ", ".join(sorted(working)[:8]) + ".")
    wonders = [w for w in _read(root / "wonders.json", []) if isinstance(w, str)]
    lines.append("What I'll search first next time: " + (", ".join(wonders[:CURIOSITY]) if wonders else "nothing saved yet") + ".")
    return "\n".join(lines)
