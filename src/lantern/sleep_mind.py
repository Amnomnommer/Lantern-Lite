"""The sleep mind (owner amendment, 2026-10-24, `MULTIPLE_MINDS.md`): Prime's nightly consolidation.

It runs once a day, at the first opening of the day; there is no background schedule. It works
only on reading-derived knowledge, and never on the owner's words or memories, goals,
permissions, identity or personality.

One night has four steps:
1. Merge: facts about one idea that say the same thing in different words are reworded to the
   commonest wording. Each keeps its own page, so agreement between pages is kept.
2. Refine: categories come from "is a" facts. What a category has or can do passes down to its
   members as *worked out* knowledge ("a sparrow is a bird; a bird has feathers"), kept here and
   never in the fact memory, so it can never pass as read. Properties shared by many members of a
   category are noted as patterns for the owner to review.
3. Condense: each well-read idea gets a short volume (its key facts, with pages, disagreements and
   what was worked out).
4. Trim: ideas unused for a long time, with few facts and no disagreement, move to cold storage.
   They can still be found (`!cold`), and the owner can restore them (`!restore`). Nothing is erased.

Every night is one undoable session (`!sleep undo`). Originals of reworded or trimmed facts are
kept in cold storage with their page and reading session. Undoing or restoring re-learns them
through the normal reading rules, which recreates their agreement and disagreement links.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from lantern import belief_mind, concept_forest, reading_digest
from lantern.memory import MemoryStore

# The concept forest (#224) is built during sleep only when this is on. Off by owner decision (2026-10-26,
# the Schema-v2 and Lens specs: "do not re-enable Concept Forest organization"; no global rebuild during
# startup, chat, reading, digestion or sleep). The bounded build (#229: 12,000 ideas in 1.6 s) stays in
# concept_forest for review; Schema v2 replaces it with incremental indexes.
ORGANIZE_IN_SLEEP = False
TRIM_AFTER_DAYS = 30
TRIM_MAX_FACTS = 2  # ideas with more facts are not trimmed, however quiet
MAX_TRIM_PER_NIGHT = 500
MAX_MERGES_PER_NIGHT = 2000
VOLUME_MIN_FACTS = 5
VOLUME_FACTS = 8
PATTERN_MIN_MEMBERS = 4
PATTERN_SHARE = 0.6
INHERITED = ("has", "can", "is_a", "consists_of", "requires")
_ARTICLE = re.compile(r"^(?:the|a|an)\s+")
_PUNCTUATION = re.compile(r"[^\w\s'-]")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def normal(text: str) -> str:
    """One idea however it is worded: case, articles, punctuation and plural endings aside."""
    words = _PUNCTUATION.sub(" ", _ARTICLE.sub("", text.casefold().strip())).split()
    return " ".join(reading_digest.singular(w) if len(w) > 3 else w for w in words)


class SleepStore:
    """The sleep mind's own store (in the vault, beside Prime's memory)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS nights (night TEXT PRIMARY KEY, started TEXT, report TEXT, undone INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cold (id INTEGER PRIMARY KEY, night TEXT, why TEXT, concept TEXT, predicate TEXT,
                object TEXT, reference TEXT, restored INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS usage (concept TEXT PRIMARY KEY, uses INTEGER, last_used TEXT);
            CREATE TABLE IF NOT EXISTS inferred (concept TEXT, predicate TEXT, object TEXT, because TEXT, night TEXT);
            CREATE TABLE IF NOT EXISTS volumes (concept TEXT PRIMARY KEY, body TEXT, night TEXT);
            CREATE TABLE IF NOT EXISTS patterns (category TEXT, predicate TEXT, object TEXT, members INTEGER, share REAL,
                night TEXT);
            CREATE TABLE IF NOT EXISTS reviewed (category TEXT, predicate TEXT, object TEXT, verdict TEXT, at TEXT,
                PRIMARY KEY (category, predicate, object));
        """)

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def __enter__(self) -> "SleepStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def touch(self, concepts: Iterable[str], *, at: datetime | None = None) -> None:
        """An idea was used: asked about, answered from, or talked about."""
        stamp = (at or _now()).isoformat()
        for concept in set(concepts):
            self.connection.execute(
                "INSERT INTO usage(concept, uses, last_used) VALUES (?, 1, ?) "
                "ON CONFLICT(concept) DO UPDATE SET uses = uses + 1, last_used = excluded.last_used", (concept, stamp))
        self.connection.commit()

    def last_night(self) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT night, report, undone FROM nights ORDER BY night DESC LIMIT 1").fetchone()
        return None if row is None else {"night": row[0], **json.loads(row[1] or "{}"), "undone": bool(row[2])}

    def worked_out(self, concept: str, limit: int = 2) -> list[tuple[str, str, str]]:
        return [tuple(r) for r in self.connection.execute(
            "SELECT predicate, object, because FROM inferred WHERE concept = ? LIMIT ?", (concept, limit))]

    def volume(self, concept: str) -> dict[str, object] | None:
        row = self.connection.execute("SELECT body FROM volumes WHERE concept = ?", (concept,)).fetchone()
        return None if row is None else json.loads(row[0])

    def cold_matches(self, concept: str) -> list[dict[str, object]]:
        return [{"id": r[0], "concept": r[1], "predicate": r[2], "object": r[3], "page": reading_digest._page_of(r[4]),
                 "why": r[5]} for r in self.connection.execute(
            "SELECT id, concept, predicate, object, reference, why FROM cold WHERE restored = 0 AND concept = ? "
            "ORDER BY id", (concept,))]


def _rows(memory: MemoryStore) -> list[tuple[str, str, str, str, str, str]]:
    """Active reading-derived facts: (claim id, concept, predicate, object, reference, created)."""
    return [tuple(str(v or "") for v in r) for r in memory._connection.execute(
        "SELECT c.id, e.canonical_name, c.predicate, c.object_text, s.reference, c.created_at FROM claims AS c "
        "JOIN entities AS e ON e.id = c.subject_entity_id LEFT JOIN sources AS s ON s.id = c.source_id "
        "WHERE e.kind = ? AND c.status = 'active' AND c.evidence_type = ? AND s.kind = ? ORDER BY c.created_at, c.rowid",
        (reading_digest.CONCEPT_KIND, reading_digest.EVIDENCE, reading_digest.SOURCE_KIND))]


def _session(reference: str) -> str:
    return reference.split(":", 2)[1] if reference.count(":") >= 2 else "restored"


def _to_cold(memory: MemoryStore, store: SleepStore, night: str, why: str,
             row: tuple[str, str, str, str, str, str]) -> None:
    claim, concept, predicate, obj, reference, _created = row
    store.connection.execute("INSERT INTO cold(night, why, concept, predicate, object, reference) VALUES (?, ?, ?, ?, ?, ?)",
                             (night, why, concept, predicate, obj, reference))
    memory.forget("claim", claim, requested_by="lantern-sleep", reason_code="correction")


def _relearn(memory: MemoryStore, concept: str, predicate: str, obj: str, reference: str, *, session: str | None = None) -> bool:
    """Back through the normal reading rules, which recreate agreement and disagreement links."""
    page = reading_digest._page_of(reference)
    return reading_digest.learn(memory, session or _session(reference), page,
                                reading_digest.Relation(concept, predicate, obj), {})


def _merge(memory: MemoryStore, store: SleepStore, night: str, rows: list) -> int:
    groups: dict[tuple[str, str, str], list] = {}
    for row in rows:
        if _session(row[4]).startswith("hive-"):
            continue  # what the phone read keeps its own wording and batch
        groups.setdefault((row[1], row[2], normal(row[3])), []).append(row)
    merged = 0
    for (concept, predicate, _), items in groups.items():
        wordings = {r[3] for r in items}
        if len(wordings) < 2:
            continue
        counts = {w: sum(1 for r in items if r[3] == w) for w in wordings}
        canonical = min(wordings, key=lambda w: (-counts[w], len(w), w))
        for row in items:
            if row[3] == canonical or merged >= MAX_MERGES_PER_NIGHT:
                continue
            _to_cold(memory, store, night, f"merged into \"{canonical}\"", row)
            _relearn(memory, concept, predicate, canonical, row[4], session=f"sleep-{night}")
            merged += 1
    return merged


CHAIN_DEPTH = 3  # sparrow -> bird -> animal -> living thing
MAX_WORKED_PER_IDEA = 6
MAX_WORKED_PER_NIGHT = 20_000
# Where a category's own name ends: "key sanskrit text that explores ..." is a "key sanskrit text".
_CATEGORY_END = re.compile(r"\s(?:that|which|who|whose|where|when|of|in|on|at|for|with|from|by|to|as|dedicated|located|"
                           r"used|known|based|made|found|named|called|consisting|including)\s")
# Too general to pass anything down ("X is a term ..." says nothing a term has).
NOT_CATEGORIES = frozenset({"term", "word", "phrase", "expression", "name", "thing", "something", "one", "part", "type",
                            "kind", "form", "way", "example", "member", "set", "list", "number", "use", "case", "piece"})


def category_of(category: str, concept: str, known: dict[str, str]) -> str | None:
    """The known idea a category names: the whole of it, else its own name and that name's shorter endings
    ("important pilgrimage site dedicated to …" -> "important pilgrimage site", "pilgrimage site", "site").
    `known` maps each idea's normal form to its name."""
    whole = normal(category)
    match = _CATEGORY_END.search(" " + whole + " ")
    head = whole if match is None else (" " + whole + " ")[:match.start()].strip()
    words = head.split()
    for candidate in [whole] + [" ".join(words[i:]) for i in range(len(words))]:
        if candidate and candidate != normal(concept) and candidate in known and candidate not in NOT_CATEGORIES:
            return known[candidate]
    return None


def work_out(facts: dict[str, set[tuple[str, str]]]) -> list[tuple[str, str, str, str]]:
    """What categories pass down to their members, along "is a" chains up to CHAIN_DEPTH long:
    (idea, predicate, object, because). Same as the phone's PhoneSleep.workOut (`sleep-vectors-v1.json`)."""
    known = {normal(c): c for c in sorted(facts, reverse=True)}  # the first-sorted name wins a shared normal form
    found: list[tuple[str, str, str, str]] = []
    for concept in sorted(facts):
        own = {(p, normal(o)) for p, o in facts[concept]}
        itself = normal(concept)
        given: set[tuple[str, str]] = set()
        frontier = [(concept, [])]
        visited = {concept}
        for _depth in range(CHAIN_DEPTH):
            following = []
            for member, steps in frontier:
                for predicate, category in sorted(facts.get(member, ())):
                    if predicate != "is_a":
                        continue
                    parent = category_of(category, member, known)
                    if parent is None or parent in visited:
                        continue
                    visited.add(parent)
                    chain = steps + [f"{member} is {reading_digest._with_article('is_a', category)[3:]}"]
                    following.append((parent, chain))
                    for inherited, obj in sorted(facts[parent]):
                        key = (inherited, normal(obj))
                        if (inherited not in INHERITED or key in own or key in given or normal(obj) == itself
                                or len(given) >= MAX_WORKED_PER_IDEA):
                            continue
                        given.add(key)
                        found.append((concept, inherited, obj,
                                      "; ".join(chain + [f"{parent} {reading_digest._with_article(inherited, obj)}"])))
                        if len(found) >= MAX_WORKED_PER_NIGHT:
                            return found
            frontier = following
    return found


def _refine(store: SleepStore, night: str, rows: list) -> tuple[int, int]:
    facts: dict[str, set[tuple[str, str]]] = {}
    for _id, concept, predicate, obj, _ref, _created in rows:
        facts.setdefault(concept, set()).add((predicate, obj))
    store.connection.execute("DELETE FROM inferred")
    store.connection.execute("DELETE FROM patterns")
    worked = work_out(facts)
    store.connection.executemany("INSERT INTO inferred VALUES (?, ?, ?, ?, ?)",
                                 [(c, p, o, because, night) for c, p, o, because in worked])
    inferred = len(worked)
    members: dict[str, list[str]] = {}
    by_normal = {normal(c): c for c in sorted(facts, reverse=True)}
    for concept, known in facts.items():
        for predicate, category in known:
            if predicate == "is_a":
                parent = category_of(category, concept, by_normal)
                if parent is not None:
                    members.setdefault(parent, []).append(concept)
    patterns = 0
    for category, names in members.items():
        names = sorted(set(names))
        if len(names) < PATTERN_MIN_MEMBERS:
            continue
        shared: dict[tuple[str, str], int] = {}
        for name in names:
            for predicate, obj in {(p, normal(o)) for p, o in facts[name] if p != "is_a"}:
                shared[(predicate, obj)] = shared.get((predicate, obj), 0) + 1
        for (predicate, obj), count in shared.items():
            done = store.connection.execute("SELECT 1 FROM reviewed WHERE category = ? AND predicate = ? AND object = ?",
                                            (category, predicate, obj)).fetchone()
            if count / len(names) >= PATTERN_SHARE and count >= PATTERN_MIN_MEMBERS - 1 and done is None:
                store.connection.execute("INSERT INTO patterns VALUES (?, ?, ?, ?, ?, ?)",
                                         (category, predicate, obj, len(names), round(count / len(names), 2), night))
                patterns += 1
    return inferred, patterns


def _condense(memory: MemoryStore, store: SleepStore, night: str, rows: list) -> int:
    by_concept: dict[str, dict[tuple[str, str], set[str]]] = {}
    for _id, concept, predicate, obj, reference, _created in rows:
        by_concept.setdefault(concept, {}).setdefault((predicate, normal(obj)), set()).add(reading_digest._page_of(reference))
    disputed = {str(d["concept"]): reading_digest.dispute_line(d) for d in reading_digest.disputes(memory, limit=200)}
    store.connection.execute("DELETE FROM volumes")
    written = 0
    for concept, facts in by_concept.items():
        if len(facts) < VOLUME_MIN_FACTS:
            continue
        wording = {(p, normal(o)): o for _i, c, p, o, _r, _t in rows if c == concept}
        order = sorted(facts, key=lambda k: (-len(facts[k]), reading_digest.ORDER.index(k[0])
                                             if k[0] in reading_digest.ORDER else len(reading_digest.ORDER)))
        body = {
            "concept": concept,
            "facts": [{"says": f"{concept} {reading_digest._with_article(p, wording[(p, o)])}",
                       "pages": sorted(facts[(p, o)])[:5]} for p, o in order[:VOLUME_FACTS]],
            "more": max(0, len(facts) - VOLUME_FACTS),
            "worked_out": [f"{concept} {reading_digest._with_article(p, o)} (because {b})"
                           for p, o, b in store.worked_out(concept, limit=3)],
            "disagreement": disputed.get(concept),
        }
        store.connection.execute("INSERT INTO volumes VALUES (?, ?, ?)", (concept, json.dumps(body), night))
        written += 1
    return written


def _trim(memory: MemoryStore, store: SleepStore, night: str, rows: list, now: datetime,
          keep: frozenset[str] = frozenset()) -> int:
    cutoff = (now - timedelta(days=TRIM_AFTER_DAYS)).isoformat()
    used = {r[0]: r[1] for r in store.connection.execute("SELECT concept, last_used FROM usage")}
    disputed = {str(d["concept"]) for d in reading_digest.disputes(memory, limit=1000)}
    by_concept: dict[str, list] = {}
    for row in rows:
        by_concept.setdefault(row[1], []).append(row)
    trimmed = 0
    for concept, items in by_concept.items():
        latest = max([used.get(concept, "")] + [r[5] for r in items])
        if (latest >= cutoff or len(items) > TRIM_MAX_FACTS or concept in disputed or concept in keep
                or concept.split()[-1] in keep or trimmed + len(items) > MAX_TRIM_PER_NIGHT):
            continue
        for row in items:
            _to_cold(memory, store, night, "not used for a long time", row)
            trimmed += 1
    return trimmed


def sleep(memory: MemoryStore, store: SleepStore, *, now: datetime | None = None, force: bool = False,
          keep: frozenset[str] = frozenset()) -> dict[str, object] | None:
    """`keep`: ideas another mind marked as salient (the expectation mind's surprises); never trimmed."""
    """One night's sleep, at most once a day; None when today's sleep already happened."""
    now = now or _now()
    night = now.date().isoformat()
    last = store.last_night()
    if last is not None and last["night"] >= night and not force:
        return None
    store.connection.execute("DELETE FROM nights WHERE night = ?", (night,))
    report: dict[str, object] = {"merged": _merge(memory, store, night, _rows(memory))}
    rows = _rows(memory)
    report["worked_out"], report["patterns"] = _refine(store, night, rows)
    report["volumes"] = _condense(memory, store, night, rows)
    report["set_aside"] = _trim(memory, store, night, rows, now, keep)
    # Evidence-driven memory (owner amendment, 2026-10-24): beliefs re-weighed from evidence only, each change logged.
    weighed = belief_mind.weigh(memory, store.connection, night)
    report["beliefs_new"], report["beliefs_rose"], report["beliefs_fell"] = weighed["new"], weighed["rose"], weighed["fell"]
    active_rows = _rows(memory)
    report["facts"] = len(active_rows)
    if not ORGANIZE_IN_SLEEP:
        report["organization"] = "off: the concept forest is too slow for this memory yet (see LANTERN_HANDOFF.md)"
    else:
        try:
            receipt = concept_forest.atomic_rebuild(
                store.path.with_name("concept-forest.json"),
                (concept_forest.Assertion(row[1], row[2], row[3], row[4]) for row in active_rows),
                night=night,
            )
            report["organized"] = receipt["concepts"]
            report["concept_forest_sha256"] = receipt["sha256"]
        except Exception as error:  # consolidation remains rollback-safe; an integrity failure is visible to debug
            report["organization_integrity_warning"] = type(error).__name__
    store.connection.execute("INSERT INTO nights(night, started, report) VALUES (?, ?, ?)",
                             (night, now.isoformat(), json.dumps(report, sort_keys=True)))
    store.connection.commit()
    return {"night": night, **report}


def undo(memory: MemoryStore, store: SleepStore, night: str | None = None) -> dict[str, int]:
    """Reverse one night: its rewordings go, and its originals come back through the reading rules."""
    last = store.last_night()
    night = night or (last["night"] if last else None)
    if night is None:
        raise ValueError("I haven't slept yet")
    removed = reading_digest.unlearn_session(memory, f"sleep-{night}")
    belief_mind.undo(store.connection, night)
    rows = store.connection.execute(
        "SELECT id, concept, predicate, object, reference FROM cold WHERE night = ? AND restored = 0", (night,)).fetchall()
    for cold_id, concept, predicate, obj, reference in rows:
        _relearn(memory, concept, predicate, obj, reference)
        store.connection.execute("UPDATE cold SET restored = 1 WHERE id = ?", (cold_id,))
    store.connection.execute("UPDATE nights SET undone = 1 WHERE night = ?", (night,))
    store.connection.execute("DELETE FROM inferred WHERE night = ?", (night,))
    store.connection.execute("DELETE FROM patterns WHERE night = ?", (night,))
    store.connection.execute("DELETE FROM volumes WHERE night = ?", (night,))
    store.connection.commit()
    return {"rewordings_removed": removed, "restored": len(rows)}


def restore(memory: MemoryStore, store: SleepStore, concept: str) -> int:
    """The owner brings an idea back from cold storage."""
    rows = store.cold_matches(concept)
    for row in rows:
        reference = store.connection.execute("SELECT reference FROM cold WHERE id = ?", (row["id"],)).fetchone()[0]
        _relearn(memory, row["concept"], row["predicate"], row["object"], reference)
        store.connection.execute("UPDATE cold SET restored = 1 WHERE id = ?", (row["id"],))
    store.connection.commit()
    store.touch([concept])
    return len(rows)


def pending_patterns(store: SleepStore, limit: int = 20) -> list[tuple[str, str, str, int, float]]:
    """Patterns sleep noticed that the owner has not reviewed, biggest first (numbered for !accept / !reject)."""
    return [tuple(r) for r in store.connection.execute(
        "SELECT p.category, p.predicate, p.object, p.members, p.share FROM patterns AS p LEFT JOIN reviewed AS r "
        "ON r.category = p.category AND r.predicate = p.predicate AND r.object = p.object WHERE r.verdict IS NULL "
        "ORDER BY p.members DESC, p.category, p.predicate, p.object LIMIT ?", (limit,))]


def review_pattern(memory: MemoryStore, store: SleepStore, pattern: tuple, verdict: str) -> None:
    """The owner's review (roadmap: owner review command). Accepted: a generalization in Prime's memory,
    labelled worked out (inferred) and owner-reviewed, never as read. Rejected: never proposed again."""
    category, predicate, obj = pattern[:3]
    store.connection.execute("INSERT OR REPLACE INTO reviewed VALUES (?, ?, ?, ?, ?)",
                             (category, predicate, obj, verdict, _now().isoformat()))
    store.connection.commit()
    if verdict == "accepted":
        entity = reading_digest._concept(memory, category)
        source = memory.add_source(REVIEWED_PATTERN, f"pattern:{category}:{predicate}:{obj}")
        memory.add_claim(entity, predicate, obj, evidence_type="inferred", confidence=0.7, source_id=source,
                         consequential=False)


REVIEWED_PATTERN = "owner-reviewed-pattern"


def generalizations(memory: MemoryStore, concept: str) -> list[tuple[str, str]]:
    """Patterns the owner accepted about a category (or about the category an idea belongs to)."""
    rows = memory._connection.execute(
        "SELECT e.canonical_name, c.predicate, c.object_text FROM claims AS c JOIN entities AS e ON e.id = c.subject_entity_id "
        "JOIN sources AS s ON s.id = c.source_id WHERE s.kind = ? AND c.status = 'active'", (REVIEWED_PATTERN,)).fetchall()
    return [(str(p), str(o)) for name, p, o in rows if name == concept]


def morning(report: dict[str, object] | None) -> str:
    """What the owner hears when the day starts."""
    if not report:
        return ""
    parts = []
    if report.get("merged"):
        parts.append(f"merged {report['merged']} fact(s) that said the same thing")
    if report.get("worked_out"):
        parts.append(f"worked out {report['worked_out']} thing(s) from what I know")
    if report.get("volumes"):
        parts.append(f"wrote {report['volumes']} short volume(s)")
    if report.get("beliefs_new") or report.get("beliefs_rose") or report.get("beliefs_fell"):
        parts.append(f"weighed the evidence again ({report.get('beliefs_new', 0)} new belief(s), "
                     f"{report.get('beliefs_rose', 0)} stronger, {report.get('beliefs_fell', 0)} weaker)")
    if report.get("set_aside"):
        parts.append(f"set aside {report['set_aside']} fact(s) I haven't used in a long time (!cold finds them)")
    if not parts:
        return "I slept; nothing needed tidying."
    return "While I slept I " + ", ".join(parts) + ". !sleep shows more; !sleep undo reverses it."


def named(memory: MemoryStore, question: str) -> list[str]:
    """Ideas Prime knows that a message names (for usage)."""
    text = " ".join(question.lower().replace("?", " ").split())
    names = [str(r[0]) for r in memory._connection.execute(
        "SELECT DISTINCT e.canonical_name FROM entities AS e JOIN claims AS c ON c.subject_entity_id = e.id "
        "WHERE e.kind = ? AND c.status = 'active'", (reading_digest.CONCEPT_KIND,))]
    return [n for n in names if len(n) > 2 and re.search(rf"(?<![a-z]){re.escape(n)}(?:s|es)?(?![a-z])", text)]
