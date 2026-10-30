"""Lantern's own language (owner amendment, 2026-10-24): grounded composition, learned from its own reading.

The owner: the boundary is reaching outside Lantern to generate. Lantern's own generative minds are
welcome, as long as every factual statement it composes traces to its memory with a source.

How it learns:
- **Frames.** From every trusted sentence that states a relation, Lantern keeps the sentence's shape:
  the idea becomes {s} and what is said about it becomes {o} ("{s}, also known as {o}"; "the {s} is a
  {o}"). A frame may hold only grammar and connecting words, so a frame can never carry a fact of its
  own.
- **Connectors.** It also keeps the openers writers use between sentences ("also", "in fact",
  "however").

How it speaks:
- It fills frames with its own sourced facts.
- It prefers frames it has met often, but never reuses one it just used.
- It links sentences with learned connectors.

Every composed sentence therefore consists of frame words (grammar only) and fact strings from
memory: grounded by construction, and checked again before it is said.

Its language grows with its reading. Until it knows enough frames it keeps the plain phrasing.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from lantern import reading_digest

MIN_FRAMES = 12
RECENT = 8
MAX_FRAME_WORDS = 8
# Grammar and connecting words: the only words a frame may hold besides its two slots.
GRAMMAR = frozenset("""
a an the this that these those it its they their there here which who whom whose what where when while
is are was were be been being has have had do does did can could may might will would shall should must
of in on at by for with from to into onto upon about as like than and or but nor so yet also often usually
generally commonly typically sometimes always mostly mainly largely chiefly especially known called named
termed referred refers kind type form sort part member example one ones some any many much most more such
not no both either neither each every all other another same very quite rather just only even still too
""".split())
PREDICATE_WORDS = frozenset(w for verb, _ in reading_digest.VERBS for w in verb.split())
CONNECTORS = ("also", "in fact", "indeed", "however", "moreover", "besides", "for example", "in addition",
              "notably", "interestingly", "meanwhile", "still", "even so", "likewise", "similarly")
# Joining one more fact to the last is adding, never contrasting ("however") or giving an example.
ADDITIVE = frozenset({"also", "in fact", "indeed", "moreover", "besides", "in addition", "notably", "interestingly",
                      "likewise", "similarly"})
_WORD = re.compile(r"[a-z][a-z'-]*")
_SINGULAR = {"are": "is", "were": "was", "have": "has", "include": "includes", "contain": "contains",
             "help": "helps", "cause": "causes", "require": "requires", "produce": "produces", "describe": "describes",
             "involve": "involves", "study": "studies"}


class LanguageStore:
    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA cache_size = -16000")  # 16 MB: fewer disk reads while reading
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS frames (predicate TEXT, frame TEXT, count INTEGER NOT NULL, PRIMARY KEY (predicate, frame));
            CREATE TABLE IF NOT EXISTS connectors (connector TEXT PRIMARY KEY, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS recent (frame TEXT, at INTEGER);
        """)

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    # -- learning --------------------------------------------------------------------------

    def observe(self, text: str, community: str = "reading") -> int:
        """Learn frames and connectors from text; returns how many frames it met."""
        met = 0
        for sentence in (s.strip() for line in text.split("\n") for s in reading_digest._SENTENCE_END.split(line)):
            if not sentence:
                continue
            lowered = sentence[0].lower() + sentence[1:]
            opener = lowered.split(",", 1)[0].strip().lower() if "," in lowered else ""
            if opener in CONNECTORS:
                self.connection.execute(
                    "INSERT INTO connectors VALUES (?, 1) ON CONFLICT(connector) DO UPDATE SET count = count + 1", (opener,))
            for relation in reading_digest.relations_in("", sentence):
                frame = frame_of(lowered, relation)
                if frame is not None:
                    self.connection.execute(
                        "INSERT INTO frames VALUES (?, ?, 1) ON CONFLICT(predicate, frame) DO UPDATE SET count = count + 1",
                        (relation.predicate, frame))
                    met += 1
        self.connection.commit()
        return met

    def known(self) -> int:
        """Distinct sentence shapes learned."""
        return int(self.connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0])

    def ready(self) -> bool:
        """Enough met in reading (MIN_FRAMES sightings, across two or more kinds of fact) to speak in its own shapes."""
        met, kinds = self.connection.execute("SELECT COALESCE(SUM(count), 0), COUNT(DISTINCT predicate) FROM frames").fetchone()
        return int(met) >= MIN_FRAMES and int(kinds) >= 2

    # -- speaking ----------------------------------------------------------------------------

    def _frame(self, predicate: str, avoid: set[str]) -> str | None:
        """The most-met shape for this kind of fact that does not read like one just used."""
        rows = self.connection.execute("SELECT frame FROM frames WHERE predicate = ? ORDER BY count DESC, frame",
                                       (predicate,)).fetchall()
        for (frame,) in rows:
            if surface(str(frame)) not in avoid:
                return str(frame)
        return str(rows[0][0]) if rows else None

    def _connector(self, index: int) -> str:
        rows = [r for r in self.connection.execute("SELECT connector FROM connectors ORDER BY count DESC, connector")
                if r[0] in ADDITIVE][:5]
        return str(rows[index % len(rows)][0]) if rows else "also"

    def compose(self, concept: str, facts: list[tuple[str, str]]) -> str | None:
        """Sentences about an idea from its sourced facts, in Lantern's own learned phrasing; None if it can't yet."""
        if not facts or not self.ready():
            return None
        turn = int(self.connection.execute("SELECT COALESCE(MAX(at), 0) FROM recent").fetchone()[0]) + 1
        recent = {surface(str(r[0])) for r in self.connection.execute("SELECT frame FROM recent WHERE at > ?", (turn - RECENT,))}
        sentences = []
        for index, (predicate, obj) in enumerate(facts):
            frame = self._frame(predicate, recent)
            if frame is None:  # never met this kind of fact in writing: say it plainly
                sentence = f"{concept if index == 0 else 'it'} {reading_digest._with_article(predicate, obj)}"
            else:
                recent.add(surface(frame))
                self.connection.execute("INSERT INTO recent VALUES (?, ?)", (frame, turn))
                sentence = fill(frame, concept if index == 0 or frame.startswith("{o}") else "it", obj)
            if not grounded(sentence, concept, obj):
                return None  # never say something whose words are not all grammar or the fact itself
            if index > 0 and frame is not None:
                sentence = f"{self._connector(turn + index - 1)}, {sentence}"
            sentences.append(sentence[0].upper() + sentence[1:])
        self.connection.commit()
        return ". ".join(s.rstrip(".") for s in sentences)

    def explain(self) -> str:
        total = self.known()
        lines = [f"My own language: {total} sentence shape(s) learned from what I read"
                 + ("" if self.ready() else f" (I start using them once I've met {MIN_FRAMES} in my reading)") + "."]
        for predicate, frame, count in self.connection.execute(
                "SELECT predicate, frame, count FROM frames ORDER BY count DESC, frame LIMIT 8"):
            lines.append(f"• \"{frame}\" ({predicate}, met {count} time{'s' if count != 1 else ''})")
        connectors = [r[0] for r in self.connection.execute("SELECT connector FROM connectors ORDER BY count DESC LIMIT 5")]
        if connectors:
            lines.append("Ways I link ideas: " + ", ".join(connectors))
        return "\n".join(lines)


def frame_of(lowered: str, relation: reading_digest.Relation) -> str | None:
    """A sentence's shape around one relation, or None when the shape would carry content of its own."""
    obj = relation.object.lower()
    text = lowered.lower().rstrip(".!? ")
    at = text.rfind(obj)
    if at < 0:
        return None
    subject = next((form for form in (relation.subject + "es", relation.subject + "s", relation.subject)
                    if re.search(rf"(?<![a-z]){re.escape(form)}(?![a-z])", text[:at] + " " + text[at + len(obj):])), None)
    if subject is None:
        return None
    shaped = text[:at] + "{o}" + text[at + len(obj):]
    shaped = re.sub(rf"(?<![a-z]){re.escape(subject)}(?![a-z])", "{s}", shaped, count=1)
    if "{s}" not in shaped:
        return None
    words = _WORD.findall(shaped.replace("{s}", " ").replace("{o}", " "))
    words = [_SINGULAR.get(w, w) for w in words]
    if len(words) > MAX_FRAME_WORDS or any(w not in GRAMMAR and w not in PREDICATE_WORDS for w in words):
        return None
    for plural, single in _SINGULAR.items():
        shaped = re.sub(rf"(?<![a-z]){plural}(?![a-z])", single, shaped)
    return " ".join(shaped.split())


def surface(frame: str) -> str:
    """How a shape reads once filled (articles before the idea dropped, a/an alike)."""
    return re.sub(r"(?<![a-z])an(?= )", "a", re.sub(r"(?<![a-z])(?:the|a|an) \{s\}", "{s}", frame))


def fill(frame: str, subject: str, obj: str) -> str:
    # No article before the idea itself ("the René Descartes", "the it"): names and "it" stand alone.
    frame = re.sub(r"(?<![a-z])(?:the|a|an) \{s\}", "{s}", frame)
    sentence = frame.replace("{s}", subject).replace("{o}", obj)
    # "a" or "an" by the next word's sound.
    return re.sub(r"(?<![a-z])(a|an) ([a-z])", lambda m: ("an " if m.group(2) in "aeiou" else "a ") + m.group(2), sentence)


def grounded(sentence: str, concept: str, obj: str) -> bool:
    """Every word is grammar, a predicate word, or part of the idea or the fact itself."""
    allowed = (GRAMMAR | PREDICATE_WORDS | {w for c in ADDITIVE for w in c.split()}
               | set(_WORD.findall(concept.lower())) | set(_WORD.findall(obj.lower())))
    return all(w in allowed for w in _WORD.findall(sentence.lower()))
