"""Research threads (owner, 2026-10-25): deepen what Lantern knows instead of only widening it, and be
rewarded for going out to more sources and looping back to Wikipedia to check.

After each reading is digested, Lantern opens a few research threads ("research nodes"), each one an
idea worth reading about again, of one of four kinds:
- **dispute:** its sources disagree about the idea;
- **thin:** many ideas are a kind of it, yet it knows little about it (what it learns passes down to
  all of them when it sleeps);
- **widen:** it knows the idea only from Wikimedia's sites, so it should hear other voices;
- **check:** it knows the idea only from sources outside Wikimedia, so it loops back to Wikipedia.

The ideas join `wonders.json`, after anything a conversation made it wonder about, so the next
reading searches them. When a later reading has searched a thread's idea, the thread closes with a
**reward** measured from evidence only (never ratings): facts gained about the idea, plus twice the
new independent origins that now speak about it. Each kind keeps its average reward, and the next
threads favour the kinds that have paid off (UCB1, so a kind that has not been tried yet still gets
its turn). It changes only what Lantern chooses to read; it never writes memory, goals or permissions.
`!research` explains the open threads and what each kind has earned.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from lantern import reading_digest, sleep_mind
from lantern.explorer import is_idea, origin
from lantern.memory import MemoryStore

KINDS = ("dispute", "thin", "widen", "check")
DESCRIPTIONS = {
    "dispute": "my sources disagree about it",
    "thin": "many things are a kind of it, but I know little about it",
    "widen": "I only know it from Wikipedia and its sister sites",
    "check": "I only know it from outside Wikipedia, so I'll check it there",
}
THREADS_PER_READING = 3
MAX_OPEN = 12
THIN_MAX_FACTS = 2
THIN_MIN_MEMBERS = 2
ORIGIN_BONUS = 2
MAX_REWARD = 20.0
EXPLORE = 1.0  # how strongly an untried or rarely tried kind is favoured
WIKIMEDIA = "wikimedia"
FILE = "research.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _origin_of(page: str) -> str:
    if page.startswith("file:"):
        return "owner file"
    return origin(urlsplit(page).hostname or page)


def survey(memory: MemoryStore) -> dict[str, dict[str, object]]:
    """Per idea: how many facts Lantern holds, which origins they came from, and how many ideas are a kind of it."""
    relations = reading_digest.known_relations(memory)
    ideas: dict[str, dict[str, object]] = {}
    for known in relations:
        entry = ideas.setdefault(known.concept, {"facts": 0, "origins": set(), "members": 0})
        entry["facts"] = int(entry["facts"]) + 1
        if known.page:
            entry["origins"].add(_origin_of(known.page))  # type: ignore[union-attr]
    by_normal = {sleep_mind.normal(c): c for c in sorted(ideas, reverse=True)}
    members: dict[str, set[str]] = {}
    for known in relations:
        if known.predicate == "is_a":
            category = sleep_mind.category_of(known.object, known.concept, by_normal)
            if category is not None:
                members.setdefault(category, set()).add(known.concept)
    for category, names in members.items():
        ideas[category]["members"] = len(names)
    return ideas


def candidates(memory: MemoryStore, ideas: dict[str, dict[str, object]] | None = None) -> dict[str, list[str]]:
    """Ideas worth a research thread, by kind, most promising first."""
    ideas = survey(memory) if ideas is None else ideas
    usable = {name: info for name, info in ideas.items() if is_idea(name.split()[-1])}
    found = {kind: [] for kind in KINDS}
    found["dispute"] = [c for c in dict.fromkeys(str(d["concept"]) for d in reading_digest.disputes(memory, limit=40))
                        if c in usable]
    found["thin"] = sorted((n for n, i in usable.items()
                            if int(i["members"]) >= THIN_MIN_MEMBERS and int(i["facts"]) <= THIN_MAX_FACTS),
                           key=lambda n: (-int(usable[n]["members"]), n))
    found["widen"] = sorted((n for n, i in usable.items() if i["origins"] == {WIKIMEDIA} and int(i["facts"]) >= 2),
                            key=lambda n: (-int(usable[n]["facts"]), n))
    found["check"] = sorted((n for n, i in usable.items()
                             if i["origins"] and WIKIMEDIA not in i["origins"] and "owner file" not in i["origins"]),
                            key=lambda n: (-int(usable[n]["facts"]), n))
    return found


class Research:
    """The research record in the reading workspace: open threads, closed ones, and each kind's earnings."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.path = self.folder / FILE
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("format", "lantern_research_v1")
        self.state.setdefault("open", [])
        self.state.setdefault("closed", [])
        self.state.setdefault("kinds", {kind: {"tried": 0, "reward": 0.0} for kind in KINDS})

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def _score(self, kind: str) -> float:
        """UCB1: the kind's average reward, plus a bonus that shrinks as it is tried more."""
        stats = self.state["kinds"].setdefault(kind, {"tried": 0, "reward": 0.0})
        if not stats["tried"]:
            return math.inf
        total = sum(int(s["tried"]) for s in self.state["kinds"].values()) or 1
        return float(stats["reward"]) / int(stats["tried"]) + EXPLORE * MAX_REWARD / 4 * math.sqrt(
            math.log(total + 1) / int(stats["tried"]))

    def close(self, memory: MemoryStore, searched: set[str], ideas: dict[str, dict[str, object]] | None = None,
              now: str | None = None) -> list[dict[str, object]]:
        """Close the open threads whose idea a reading has now searched, rewarding each kind by what it brought."""
        ideas = survey(memory) if ideas is None else ideas
        closed, still = [], []
        for thread in self.state["open"]:
            if thread["idea"] not in searched:
                still.append(thread)
                continue
            after = ideas.get(thread["idea"], {"facts": 0, "origins": set()})
            gained = max(0, int(after["facts"]) - int(thread["facts"]))
            new_origins = sorted(set(after["origins"]) - set(thread["origins"]))
            reward = min(MAX_REWARD, gained + ORIGIN_BONUS * len(new_origins))
            stats = self.state["kinds"].setdefault(thread["kind"], {"tried": 0, "reward": 0.0})
            stats["tried"] = int(stats["tried"]) + 1
            stats["reward"] = round(float(stats["reward"]) + reward, 3)
            closed.append({**thread, "closed": now or _now(), "gained": gained, "new_origins": new_origins,
                           "reward": reward})
        self.state["open"] = still
        self.state["closed"] = (self.state["closed"] + closed)[-100:]
        return closed

    def plan(self, memory: MemoryStore, known_searches: set[str], ideas: dict[str, dict[str, object]] | None = None,
             now: str | None = None) -> list[dict[str, object]]:
        """Open new threads (at most THREADS_PER_READING, MAX_OPEN in all), favouring the kinds that paid off."""
        ideas = survey(memory) if ideas is None else ideas
        found = candidates(memory, ideas)
        taken = {t["idea"] for t in self.state["open"]} | known_searches
        opened: list[dict[str, object]] = []
        order = sorted(KINDS, key=lambda k: (-self._score(k), KINDS.index(k)))
        while len(opened) < THREADS_PER_READING and len(self.state["open"]) + len(opened) < MAX_OPEN:
            progress = False
            for kind in order:
                idea = next((i for i in found[kind] if i not in taken), None)
                if idea is None:
                    continue
                taken.add(idea)
                info = ideas.get(idea, {"facts": 0, "origins": set()})
                opened.append({"idea": idea, "kind": kind, "why": DESCRIPTIONS[kind], "opened": now or _now(),
                               "facts": int(info["facts"]), "origins": sorted(info["origins"])})
                progress = True
                if len(opened) >= THREADS_PER_READING:
                    break
            if not progress:
                break
        self.state["open"] = self.state["open"] + opened
        return opened


def after_reading(memory: MemoryStore, reading: Path, searched: set[str], now: str | None = None) -> dict[str, object]:
    """Close the threads a reading searched, open new ones, and queue their ideas in wonders.json."""
    research = Research(reading)
    ideas = survey(memory)
    closed = research.close(memory, searched, ideas, now)
    try:
        known = set(json.loads((reading / "state.json").read_text(encoding="utf-8")).get("known_searches", []))
    except (OSError, ValueError, AttributeError):
        known = set()
    opened = research.plan(memory, known | searched, ideas, now)
    research.save()
    wonders_path = reading / "wonders.json"
    try:
        wonders = [w for w in json.loads(wonders_path.read_text(encoding="utf-8")) if isinstance(w, str)]
    except (OSError, ValueError, TypeError):
        wonders = []
    added = [t["idea"] for t in opened if t["idea"] not in wonders]
    if added:
        reading.mkdir(parents=True, exist_ok=True, mode=0o700)
        reading_digest._write_json(wonders_path, (wonders + added)[-20:])
    return {"closed": closed, "opened": opened}


def explain(reading: Path) -> str:
    research = Research(reading)
    lines = ["My research threads (what I'm reading about again, and why):"]
    for thread in research.state["open"]:
        lines.append(f"• {thread['idea']}: {thread['why']}")
    if not research.state["open"]:
        lines.append("• none open; they start after my next reading.")
    paid = [(k, s) for k, s in research.state["kinds"].items() if int(s["tried"])]
    if paid:
        lines.append("What each kind of thread has brought me (facts gained, plus 2 for each new source that agrees):")
        lines += [f"• {k}: {round(float(s['reward']) / int(s['tried']), 1)} on average over {s['tried']} thread(s)"
                  for k, s in sorted(paid, key=lambda p: -float(p[1]["reward"]) / int(p[1]["tried"]))]
    for thread in research.state["closed"][-3:]:
        origins = f", new sources: {', '.join(thread['new_origins'])}" if thread["new_origins"] else ""
        lines.append(f"Last: {thread['idea']} ({thread['kind']}) gave {thread['gained']} fact(s){origins}.")
    return "\n".join(lines)
