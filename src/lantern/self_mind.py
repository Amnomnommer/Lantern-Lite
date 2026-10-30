"""Lantern's self (owner amendment, 2026-10-24): a life story, inner goals and a quiet inner life.

- **Life story.** An autobiographical memory of Lantern's own experience, day by day:
  - what it read and learned;
  - what surprised it;
  - what you talked about (ideas only, never your words);
  - its talks with other Lanterns;
  - its sleep, its goals and its thoughts.

  It can tell you about any day (`!story`, `!yesterday`).
- **Inner goals.** Lantern sets its own goals for its inner life, and they feed what it reads next:
  - *learn* what it is curious about (from the expectation mind);
  - *settle* where its sources disagree;
  - *understand* what you keep coming back to;
  - *ask* you about something it wonders about.

  It tracks each goal until it is reached. Goals that would act on the world or use the network
  beyond granted internet time are never set here; those stay the owner's. You can drop any goal.
- **Inner life.** While Lantern is open and idle it reflects: it updates its goals, notices
  connections between recent ideas (from the association mind) and keeps a thought to share when
  you return. Reflection never uses the network and never acts, and you can pause it (`!reflect off`).

All of it is this Lantern's own personality state: kept in the vault, never shared, and never
rewriting the owner's words.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

MAX_OPEN_GOALS = 5
IDLE_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class SelfMind:
    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS events (day TEXT, at TEXT, kind TEXT, text TEXT, shared INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS goals (id INTEGER PRIMARY KEY, kind TEXT, about TEXT, why TEXT, status TEXT,
                created TEXT, done TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    # -- the life story ----------------------------------------------------------------------

    def record(self, kind: str, text: str, *, at: datetime | None = None) -> None:
        moment = at or _now()
        day = moment.date().isoformat()
        if self.connection.execute("SELECT 1 FROM events WHERE day = ? AND kind = ? AND text = ?", (day, kind, text)).fetchone():
            return
        self.connection.execute("INSERT INTO events(day, at, kind, text) VALUES (?, ?, ?, ?)",
                                (day, moment.isoformat(), kind, text))
        self.connection.commit()

    def days(self) -> list[str]:
        return [str(r[0]) for r in self.connection.execute("SELECT DISTINCT day FROM events ORDER BY day")]

    def story(self, day: str) -> str | None:
        """One day of Lantern's life, told in its own words."""
        rows = self.connection.execute("SELECT kind, text FROM events WHERE day = ? ORDER BY at, rowid", (day,)).fetchall()
        if not rows:
            return None
        by: dict[str, list[str]] = {}
        for kind, text in rows:
            by.setdefault(str(kind), []).append(str(text))
        parts = []
        if "slept" in by:
            parts.append(f"I woke up after sleeping on what I knew: {by['slept'][0][0].lower()}{by['slept'][0][1:]}")
        if "read" in by:
            parts.append("I read " + "; then ".join(by["read"]) + ".")
        if "topic" in by:
            topics = list(dict.fromkeys(by["topic"]))[:5]
            parts.append(f"We talked about {', '.join(topics[:-1]) + ' and ' + topics[-1] if len(topics) > 1 else topics[0]}.")
        if "talk" in by:
            parts.append("I talked with another Lantern: " + "; ".join(by["talk"]) + ".")
        if "surprise" in by:
            parts.append("Something surprised me: " + "; ".join(by["surprise"][:3]) + ".")
        if "goal_set" in by:
            parts.append("I set myself a goal: " + "; ".join(by["goal_set"]) + ".")
        if "goal_done" in by:
            parts.append("I reached a goal: " + "; ".join(by["goal_done"]) + ".")
        if "thought" in by:
            parts.append("I was thinking: " + " ".join(by["thought"][-2:]))
        return " ".join(parts) if parts else None

    def tell(self, which: str | None, *, today: str | None = None) -> str:
        days = self.days()
        if not days:
            return "My story hasn't started yet: it begins as I read, talk and sleep."
        today = today or _now().date().isoformat()
        if which in (None, "", "week"):
            chosen = days[-7:]
        elif which == "yesterday":
            yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
            chosen = [yesterday] if yesterday in days else []
        else:
            chosen = [which] if which in days else []
        if not chosen:
            return "I don't have a story for that day."
        return "\n".join(f"{day}: {self.story(day)}" for day in chosen if self.story(day))

    # -- inner goals ---------------------------------------------------------------------------

    def goals(self, status: str = "open") -> list[dict[str, object]]:
        return [{"id": r[0], "kind": r[1], "about": r[2], "why": r[3], "created": r[4]} for r in self.connection.execute(
            "SELECT id, kind, about, why, created FROM goals WHERE status = ? ORDER BY id", (status,))]

    def set_goal(self, kind: str, about: str, why: str) -> bool:
        if kind not in ("learn", "settle", "understand", "ask"):
            raise ValueError("inner goals are about learning, settling, understanding or asking, never acting")
        open_goals = self.goals()
        if len(open_goals) >= MAX_OPEN_GOALS or any(g["about"] == about for g in open_goals):
            return False
        if self.connection.execute("SELECT 1 FROM goals WHERE about = ? AND status IN ('done', 'dropped') AND done > ?",
                                   (about, (_now() - timedelta(days=7)).isoformat())).fetchone():
            return False  # not again so soon
        self.connection.execute("INSERT INTO goals(kind, about, why, status, created) VALUES (?, ?, ?, 'open', ?)",
                                (kind, about, why, _now().isoformat()))
        self.connection.commit()
        self.record("goal_set", f"{goal_words(kind, about)} ({why})")
        return True

    def finish(self, goal_id: int, note: str, *, status: str = "done") -> None:
        row = self.connection.execute("SELECT kind, about FROM goals WHERE id = ?", (goal_id,)).fetchone()
        self.connection.execute("UPDATE goals SET status = ?, done = ?, note = ? WHERE id = ?",
                                (status, _now().isoformat(), note, goal_id))
        self.connection.commit()
        if row is not None and status == "done":
            self.record("goal_done", f"{goal_words(row[0], row[1])} ({note})")

    def explain_goals(self) -> str:
        found = self.goals()
        lines = ["My own goals (for my inner life; anything that acts in the world stays yours to decide):"]
        lines += [f"{i}. {goal_words(g['kind'], g['about'])} — {g['why']}" for i, g in enumerate(found, 1)]
        done = self.goals("done")[-3:]
        if done:
            lines.append("Reached lately: " + "; ".join(goal_words(g["kind"], g["about"]) for g in done))
        if not found:
            lines.append("None right now; I set them as I grow curious.")
        else:
            lines.append("!drop N lets one go.")
        return "\n".join(lines)

    # -- inner life ----------------------------------------------------------------------------

    def reflecting(self) -> bool:
        row = self.connection.execute("SELECT value FROM settings WHERE key = 'reflect'").fetchone()
        return row is None or row[0] == "on"

    def set_reflecting(self, on: bool) -> None:
        self.connection.execute("INSERT OR REPLACE INTO settings VALUES ('reflect', ?)", ("on" if on else "off",))
        self.connection.commit()

    def unshared_thought(self) -> str | None:
        """The latest thought Lantern has not told the owner yet (marked as told)."""
        row = self.connection.execute(
            "SELECT rowid, text FROM events WHERE kind = 'thought' AND shared = 0 ORDER BY at DESC, rowid DESC LIMIT 1").fetchone()
        if row is None:
            return None
        self.connection.execute("UPDATE events SET shared = 1 WHERE kind = 'thought'")
        self.connection.commit()
        return str(row[1])


def goal_words(kind: str, about: str) -> str:
    return {"learn": f"learn more about {about}", "settle": f"find out the truth about {about}",
            "understand": f"understand {about}, which you care about", "ask": f"ask you about {about}"}[kind]


def reflect(self_mind: SelfMind, *, memory=None, associations=None, expectations=None, wonders: Path | None = None,
            topics: list[str] | None = None) -> dict[str, object]:
    """One quiet reflection: update goals, set new ones, notice a connection. No network, no actions."""
    if not self_mind.reflecting():
        return {"reflected": False}
    from lantern import reading_digest

    done, new, thought = [], [], None
    known: dict[str, int] = {}
    if memory is not None:
        for concept, count in memory._connection.execute(
                "SELECT e.canonical_name, COUNT(*) FROM claims AS c JOIN entities AS e ON e.id = c.subject_entity_id "
                "WHERE e.kind = ? AND c.status = 'active' GROUP BY e.id", (reading_digest.CONCEPT_KIND,)):
            known[str(concept)] = int(count)
    disputed = set() if memory is None else {str(d["concept"]) for d in reading_digest.disputes(memory, limit=200)
                                             if d.get("verdict") is None}
    talked = set(topics or [])
    # Goals reached?
    for goal in self_mind.goals():
        about = str(goal["about"])
        if goal["kind"] in ("learn", "understand") and known.get(about, 0) >= 5:
            self_mind.finish(int(goal["id"]), f"I now know {known[about]} things about it")
            done.append(about)
        elif goal["kind"] == "settle" and about not in disputed:
            self_mind.finish(int(goal["id"]), "the disagreement is settled")
            done.append(about)
        elif goal["kind"] == "ask" and about in talked:
            self_mind.finish(int(goal["id"]), "we talked about it")
            done.append(about)
    # New goals from its own minds.
    if expectations is not None:
        for item in expectations.curious(limit=2):
            if self_mind.set_goal("learn", str(item["idea"]), str(item["why"])):
                new.append(str(item["idea"]))
    for concept in sorted(disputed)[:1]:
        if self_mind.set_goal("settle", concept, "my sources disagree about it"):
            new.append(concept)
    for topic in list(dict.fromkeys(topics or []))[:1]:
        if known.get(topic, 0) < 3 and self_mind.set_goal("understand", topic, "you brought it up and I know little about it"):
            new.append(topic)
    # Goals lead its reading.
    if wonders is not None and new:
        try:
            current = [w for w in json.loads(wonders.read_text(encoding="utf-8")) if isinstance(w, str)]
        except (OSError, ValueError, TypeError):
            current = []
        wanted = [g["about"] for g in self_mind.goals() if g["kind"] in ("learn", "settle", "understand")]
        merged = current + [w for w in wanted if w not in current]
        wonders.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        wonders.write_text(json.dumps(merged[-20:], indent=1) + "\n", encoding="utf-8")
    # A connection worth wondering about: two ideas that both go with a recent one, but not with each other.
    if associations is not None:
        busiest = sorted(known, key=lambda c: (-known[c], c))[:3]
        for idea in list(dict.fromkeys((topics or []) + busiest))[:5]:
            near = [str(n["idea"]) for n in associations.neighbours(idea, limit=4) if n["strength"] > 0]
            pairs = [(a, b) for i, a in enumerate(near) for b in near[i + 1:]
                     if not any(n["idea"] == b for n in associations.neighbours(a, limit=20))]
            if pairs:
                a, b = pairs[0]
                thought = f"{idea} goes with both {a} and {b}, yet I've never seen {a} and {b} together. I wonder how they connect."
                self_mind.record("thought", thought)
                if self_mind.set_goal("ask", f"{a} and {b}", f"they both go with {idea}"):
                    new.append(f"{a} and {b}")
                break
    if expectations is not None:
        for surprise in expectations.surprises(limit=2):
            after = "the start" if surprise["after"] == "|" else surprise["after"]
            self_mind.record("surprise", f"{surprise['idea']} right after {after}")
    return {"reflected": True, "goals_done": done, "goals_new": new, "thought": thought}


def talk_interests(base: tuple[str, ...], *, known: set[str], associations=None, expectations=None,
                   self_mind: SelfMind | None = None, recent: list[str] | None = None, limit: int = 8) -> tuple[str, ...]:
    """What this Lantern wants to talk about in a Lantern talk, chosen by its minds (the engine itself is unchanged).

    Its own goals come first, then what it is curious about (expectation), then its reading interests, re-ranked
    by how strongly they go with what was talked about lately (association). Only ideas it knows something
    about can be shared, so the rest wait for its reading.
    """
    wanted: list[str] = []
    if self_mind is not None:
        wanted += [str(g["about"]) for g in self_mind.goals() if g["kind"] in ("learn", "understand", "settle")]
    if expectations is not None:
        wanted += [str(c["idea"]) for c in expectations.curious(limit=3)]
    pull: dict[str, float] = {}
    if associations is not None:
        for topic in (recent or [])[-5:]:
            for n in associations.neighbours(topic.split()[-1], limit=10):
                pull[str(n["idea"])] = pull.get(str(n["idea"]), 0.0) + float(n["strength"])
    ranked = sorted(base, key=lambda c: (-pull.get(c.split()[-1], 0.0), base.index(c)))
    chosen = [c for c in dict.fromkeys(wanted + ranked) if c in known]
    return tuple(chosen[:limit])
