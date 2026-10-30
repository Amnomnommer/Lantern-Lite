"""The Lite Lantern: one public Lantern anyone can open from GitHub with nothing installed (owner amendment,
2026-10-30; `LITE_NODE.md`).

It is the lightest Lantern that is still a hive node:

- **Chat with personality.** It uses Lantern's own companion conversation (`LocalTextInterface`). There is one
  shared personality, the owner's `LITE_SEED`, which visitors' voice ratings can move by at most one step per
  trait (`lite_memory.levels`).
- **Hive library.** On start it reads the public memory space and learns what the reading rules keep. Every
  `SAVE_EVERY` messages it saves a batch: what it read since the last save, a diary note in its own words, and
  the voice tallies. Saving is a callback: Codespaces opens an issue with `gh`; the web page offers a prefilled
  issue link; without either the batch waits in `outbox/`.
- **Reading on its own.** On "!read TOPIC", or when asked about an idea it does not know, it reads that idea's
  summary from English Wikipedia's public API (HTTPS GET only, at most `MAX_READS` pages a session). It learns
  what the reading rules keep and lets the page text go.

What a visitor says is never saved to the public space: batches carry only what Lite read, its own notes and the
tallies. Memory changes only from evidence (its reading and the memory space), never from what a visitor claims.
Nothing here can post, log in, or change permissions; the public space is untrusted data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable
import urllib.parse
import urllib.request

from lantern import lite_memory as lm
from lantern import instance_personality, reading_digest
from lantern.hive_library import PrimeLibrary
from lantern.learning.phone_ledger import RULE_TRAITS
from lantern.memory import MemoryStore, StagingStore
from lantern.reading_digest import Known, Relation
from lantern.text_interface import LocalTextInterface

SAVE_EVERY = 5
MAX_READS = 5
SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
PAGE = "https://en.wikipedia.org/wiki/{title}"
LISTING = "https://api.github.com/repos/Amnomnommer/Lantern-Lite/issues?state=all&sort=created&direction=desc&per_page=100&page={page}"
_TOPIC = re.compile(r"^(?:!read|read about|can you read about|look up)\s+(?P<topic>[A-Za-z0-9][A-Za-z0-9 '\-]{0,60}?)[.!?]*$",
                    re.IGNORECASE)
_ASKED = re.compile(r"^(?:what|who) (?:is|are|was|were) (?:an? |the )?(?P<topic>[a-z0-9][a-z0-9 '\-]{1,60}?)\??$")


def _get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Lantern-Lite (github.com/Amnomnommer/Lantern-Lite)"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed public HTTPS addresses
        return response.read(2_000_000).decode("utf-8", "replace")


class LiteLantern:
    """One visitor's session with the shared Lite Lantern."""

    def __init__(self, folder: Path, *, get_text: Callable[[str], str] | None = _get_text,
                 save: Callable[[str, str], str] | None = None, issues: list[dict[str, object]] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.get_text = get_text  # None: offline (no reading, no public space)
        self.save_batch = save
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.memory = MemoryStore(self.folder / "memory.db")
        self.staging = StagingStore(self.folder / "staging.db")
        self.space = self._space(issues)
        self.levels = lm.levels(self.space)
        personality = self.folder / "personality" / "levels.json"
        personality.parent.mkdir(exist_ok=True)
        instance_personality.save(personality, self.levels)
        self.chat = LocalTextInterface(self.memory, self.staging, companion=True, owner_name="friend",
                                       journals=self.folder / "journals", ledger_path=self.folder / "ledger.json",
                                       personality_path=personality)
        self.chat.levels = dict(self.levels)  # the one shared personality, for every visitor
        self.read_now: list[Known] = []  # what this session read, for the next save
        self.notes: list[str] = []
        self.voice: dict[str, list[int]] = {}
        self.messages = 0
        self.reads = 0
        self.saved: list[str] = []
        self.batch_limit = lm.MAX_RELATIONS  # the web page lowers it so a batch fits a link; the rest waits

    def close(self) -> None:
        self.memory.close()
        self.staging.close()

    # -- the public memory space ---------------------------------------------------------------------------

    def _space(self, issues: list[dict[str, object]] | None) -> list[lm.Batch]:
        """Read the public memory space and learn what the reading rules keep (each batch its own session)."""
        if issues is None and self.get_text is not None:
            issues = []
            try:
                for page in range(1, 4):  # the newest 300 issues
                    listing = json.loads(self.get_text(LISTING.format(page=page)))
                    issues += listing if isinstance(listing, list) else []
                    if not isinstance(listing, list) or len(listing) < 100:
                        break
            except (OSError, ValueError):
                issues = []  # offline: Lite still talks, from what this browser already holds
        found, _skipped = lm.from_issues(issues or [])
        library = PrimeLibrary(self.memory)
        for _number, batch in found:
            library.receive("lite-public", batch.digest, batch.relations)
        return [batch for _number, batch in found]

    # -- one message ---------------------------------------------------------------------------------------

    def say(self, text: str) -> str:
        text = text.strip()
        lowered = text.lower()
        if lowered in ("!good voice", "!bad voice"):
            reply = self._rate(lowered == "!good voice")
        elif (topic := _TOPIC.match(text)) is not None:
            reply = self.read(topic["topic"])
        else:
            reply = self.chat.handle(text).text
            asked = _ASKED.match(lowered.rstrip("?").strip() + "?") if lowered.endswith("?") else None
            if asked and reading_digest.understanding(self.memory, text) is None and self.get_text is not None \
                    and self.reads < MAX_READS:
                learned = self.read(asked["topic"])
                if learned.startswith("I read"):
                    reply = f"{learned} {reading_digest.understanding(self.memory, text) or ''}".strip()
        self.messages += 1
        if self.messages % SAVE_EVERY == 0:
            note = self.save()
            if note:
                reply = f"{reply}\n({note})"
        return reply

    def _rate(self, good: bool) -> str:
        last = self.chat._exchanges[-1] if self.chat._exchanges else None
        traits = sorted({RULE_TRAITS[r] for r in (last.rules if last else ()) if r in RULE_TRAITS})
        if not traits:
            return "Thanks! That reply didn't use any of my voice, so there's nothing to tally."
        for trait in traits:
            tally = self.voice.setdefault(trait, [0, 0])
            tally[0 if good else 1] += 1
        return (f"Thanks, noted for {', '.join(t.replace('_', ' ') for t in traits)}. Ratings from everyone can move "
                "my personality a little, never far from where my owner set it.")

    def read(self, topic: str) -> str:
        """Read one idea's Wikipedia summary; learn what the reading rules keep; let the page text go."""
        if self.get_text is None:
            return "I can't read right now: I'm offline."
        if self.reads >= MAX_READS:
            return f"I've read {MAX_READS} pages this session, which is my limit; ask me again next time."
        title = urllib.parse.quote(" ".join(topic.split()).replace(" ", "_"), safe="")
        try:
            summary = json.loads(self.get_text(SUMMARY.format(title=title)))
        except (OSError, ValueError):
            return f"I couldn't read about {topic} just now."
        self.reads += 1
        text = str(summary.get("extract") or "") if isinstance(summary, dict) else ""
        page = PAGE.format(title=title)
        if not text:
            return f"I couldn't find a page about {topic}."
        session = f"lite-read-{self.clock().strftime('%Y%m%dT%H%M%S')}"
        sources: dict[str, str] = {}
        learned = [r for r in reading_digest.relations_in(page, text)
                   if reading_digest.learn(self.memory, session, page, r, sources)]
        self.read_now += [Known(r.subject, r.predicate, r.object, page) for r in learned]
        if learned:
            self.notes.append(f"I read about {topic} on en.wikipedia.org and learned {len(learned)} thing(s).")
        return (f"I read about {topic} on en.wikipedia.org and learned {len(learned)} thing(s)."
                if learned else f"I read about {topic}, but nothing in it passed my reading rules.")

    # -- saving to the public space ------------------------------------------------------------------------

    def save(self) -> str:
        """Save what this session read, a diary note and the voice tallies; returns what happened."""
        if not self.read_now and not self.voice:
            return ""
        batch = lm.make(self.read_now[:self.batch_limit], self.notes[-lm.MAX_DIARY:], self.voice)
        later = self.read_now[self.batch_limit:]
        title, body = lm.issue(batch, at=self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"))
        where = ""
        if self.save_batch is not None:
            try:
                where = self.save_batch(title, body)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                where = f"not saved ({type(error).__name__}); kept in my outbox"
        if not where or where.startswith("not saved"):
            outbox = self.folder / "outbox"
            outbox.mkdir(exist_ok=True)
            (outbox / f"{batch.digest}.md").write_text(f"{title}\n\n{body}", encoding="utf-8")
            where = where or "kept in my outbox until it can be saved"
        self.saved.append(batch.digest)
        self.read_now, self.notes, self.voice = later, [], {}
        return f"Saved what I read to my public memory: {where}"


def gh_save(title: str, body: str) -> str:
    """Codespaces: open the batch as an issue under the visitor's own GitHub sign-in (`gh` is signed in there)."""
    if shutil.which("gh") is None:
        raise OSError("gh is not available")
    done = subprocess.run(["gh", "issue", "create", "--repo", "Amnomnommer/Lantern-Lite", "--title", title, "--body", body],
                          capture_output=True, text=True, timeout=60, check=True)
    return done.stdout.strip().splitlines()[-1] if done.stdout.strip() else "saved"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lantern.lite", description="Talk with the public Lite Lantern")
    parser.add_argument("--folder", default=str(Path.home() / ".lantern-lite"))
    parser.add_argument("--offline", action="store_true", help="no reading and no public memory space")
    parser.add_argument("--no-save", action="store_true", help="keep batches in the outbox instead of opening issues")
    arguments = parser.parse_args(argv)
    lite = LiteLantern(Path(arguments.folder), get_text=None if arguments.offline else _get_text,
                       save=None if arguments.no_save else gh_save)
    print("Lite Lantern: one Lantern for everyone. Say anything; !read TOPIC; !good voice / !bad voice; !quit.")
    print(f"(My personality now: {', '.join(f'{k} {v}' for k, v in sorted(lite.levels.items()))}.)")
    try:
        while True:
            try:
                line = input("you> ")
            except EOFError:
                break
            if line.strip().lower() in ("!quit", "!exit"):
                break
            print("lite>", lite.say(line))
    finally:
        print(lite.save() or "Nothing new to save.")
        lite.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
