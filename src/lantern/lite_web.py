"""The Lite Lantern in a web page (owner amendment, 2026-10-30; `LITE_NODE.md`).

The page (`lite-web/index.html`) runs Lantern's own Python in the browser (Pyodide). This module is everything the
page calls, so it is tested as ordinary Python:

- `WebLite` wraps `LiteLantern` with the browser's fetch (`get_text`, given by the page) and a save step suited to
  a page that holds no secrets: a batch becomes a **prefilled "new issue" link** that the visitor opens and
  submits under their own GitHub sign-in. A linked batch carries at most `LINK_RELATIONS` relations (the rest wait
  for the next save); one still too long for a link waits in the outbox, which the page offers as a download.
- Memory lives in the browser's own storage (the page mounts `/lite` on IndexedDB), so a returning visitor's
  browser remembers. A new browser starts from the public memory space.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable
import urllib.parse

from lantern.lite import LiteLantern

NEW_ISSUE = "https://github.com/Amnomnommer/Lantern-Lite/issues/new?title={title}&body={body}"
MAX_LINK = 7_500  # GitHub refuses much longer prefilled links
LINK_RELATIONS = 12  # relations per linked batch; the rest go with the next save


def issue_link(title: str, body: str) -> str | None:
    link = NEW_ISSUE.format(title=urllib.parse.quote(title, safe=""), body=urllib.parse.quote(body, safe=""))
    return link if len(link) <= MAX_LINK else None


class WebLite:
    """What the page talks to: say(text) for a reply, pending() for a save link waiting to be opened."""

    def __init__(self, folder: str, get_text: Callable[[str], str] | None) -> None:
        self.links: list[tuple[str, str, str]] = []  # (link, title, body) waiting for the visitor
        self.lite = LiteLantern(Path(folder), get_text=get_text, save=self._save)
        self.lite.batch_limit = LINK_RELATIONS

    def _save(self, title: str, body: str) -> str:
        link = issue_link(title, body)
        if link is None:  # kept in the outbox (and offered as a download) instead
            raise ValueError("too long for a link")
        self.links.append((link, title, body))
        return "ready: press Save to my public memory to send it under your own GitHub account"

    def say(self, text: str) -> str:
        return self.lite.say(text)

    def save_now(self) -> str:
        return self.lite.save() or "Nothing new to save yet."

    def pending(self) -> list[list[str]]:
        """Save links waiting for the visitor, oldest first: [link, title, body]."""
        return [list(item) for item in self.links]

    def outbox(self) -> list[list[str]]:
        """Batches that could not become a link: [file name, text], for the page to offer as downloads."""
        folder = self.lite.folder / "outbox"
        return [[p.name, p.read_text(encoding="utf-8")] for p in sorted(folder.glob("*.md"))] if folder.is_dir() else []

    def done(self, link: str) -> None:
        self.links = [item for item in self.links if item[0] != link]

    def personality(self) -> dict[str, int]:
        return dict(self.lite.levels)
