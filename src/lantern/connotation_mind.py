"""The connotation mind (owner amendment, 2026-10-24, `MULTIPLE_MINDS.md`): how ideas feel, and to whom.

The fact mind is denotation: "cheap" and "inexpensive" mean the same. This mind learns that they feel
different, from the words around an idea in each sentence:
- valence: warm or harsh;
- arousal: calm or intense;
- register: formal or casual.

The feeling words are small, inspectable lists (below), and the owner can add to them in
`connotation-words.json` in the vault.

Feelings are kept per community, because every page carries one. Lantern learns *whose* feeling a
word carries instead of flattening voices into one (owner, 2026-10-22: minority voices first, so
they are not drowned out). The owner's own words count as the community "you".

Pure counting, so every feeling explains itself ("warm in 12 of 15 sentences"). It never presents a
feeling as a fact about the world. Shared with the phone (`PhoneConnotation.kt`, parity vectors).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

from lantern.association_mind import sentence_ideas

WARM = frozenset({
    "love", "loved", "loving", "kind", "kindness", "gentle", "warm", "joy", "joyful", "happy", "hope", "hopeful",
    "beautiful", "safe", "safety", "trust", "friend", "friends", "friendly", "care", "caring", "proud", "pride",
    "celebrate", "celebrated", "welcome", "welcoming", "comfort", "peace", "peaceful", "brave", "courage",
    "support", "supportive", "healing", "heal", "thrive", "thriving", "free", "freedom", "dignity", "respect",
    "wonderful", "delight", "delightful", "grateful", "gratitude", "community", "together", "belong", "belonging",
    "success", "successful", "bright", "fun", "laugh", "laughter", "smile", "playful", "cozy", "honest", "fair",
})
HARSH = frozenset({
    "hate", "hated", "hatred", "cruel", "cruelty", "violent", "violence", "kill", "killed", "death", "dead",
    "war", "fear", "afraid", "danger", "dangerous", "threat", "attack", "attacked", "abuse", "abused", "pain",
    "painful", "suffer", "suffering", "angry", "anger", "disgust", "disgusting", "evil", "wrong", "failure",
    "failed", "poor", "poverty", "sick", "disease", "crime", "criminal", "illegal", "banned", "ban", "slur",
    "harass", "harassment", "discrimination", "oppression", "oppressed", "persecution", "persecuted", "shame",
    "ugly", "lonely", "loss", "grief", "sad", "sadness", "broken", "destroy", "destroyed", "threatened",
})
INTENSE = frozenset({
    "crisis", "urgent", "explosive", "explosion", "shocking", "shock", "furious", "rage", "massive", "extreme",
    "radical", "revolution", "riot", "chaos", "disaster", "catastrophe", "emergency", "scream", "terrifying",
    "thrilling", "passionate", "passion", "fierce", "wild", "intense", "dramatic", "battle", "fight", "storm",
})
CALM = frozenset({
    "calm", "quiet", "gentle", "slow", "steady", "stable", "gradual", "gradually", "soft", "still", "rest",
    "routine", "ordinary", "simple", "patient", "patience", "peaceful", "mild", "balanced", "moderate", "relaxed",
})
FORMAL = frozenset({
    "therefore", "however", "furthermore", "moreover", "thus", "hence", "whereas", "pursuant", "regarding",
    "approximately", "subsequently", "consequently", "nevertheless", "notwithstanding", "accordingly", "shall",
    "herein", "aforementioned", "respectively", "significant", "significantly", "methodology", "analysis",
})
CASUAL = frozenset({
    "yeah", "yep", "nope", "gonna", "wanna", "gotta", "kinda", "sorta", "okay", "lol", "awesome", "cool",
    "stuff", "guys", "hey", "wow", "totally", "super", "pretty", "basically", "literally", "dude", "yay", "omg",
})
_WORD = re.compile(r"[a-z][a-z'-]+")
_SENTENCES = re.compile(r"[.!?\n]+")
MIN_SENTENCES = 5
NOTABLE = 0.25


def tone(sentence: str, lists: dict[str, frozenset[str]] | None = None) -> tuple[int, int, int]:
    """(valence, arousal, formality) of one sentence: counts of feeling words, each from -n to +n."""
    lists = lists or LISTS
    words = _WORD.findall(sentence.lower())
    def score(up: str, down: str) -> int:
        return sum(w in lists[up] for w in words) - sum(w in lists[down] for w in words)
    return score("warm", "harsh"), score("intense", "calm"), score("formal", "casual")


LISTS = {"warm": WARM, "harsh": HARSH, "intense": INTENSE, "calm": CALM, "formal": FORMAL, "casual": CASUAL}


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def describe(n: int, valence: int, arousal: int, formality: int) -> str:
    """How an idea feels in plain words, from signed sentence counts."""
    def side(total: int, up: str, down: str) -> str | None:
        share = total / n
        return up if share >= NOTABLE else down if share <= -NOTABLE else None
    parts = [p for p in (side(valence, "warm", "harsh"), side(arousal, "intense", "calm"),
                         side(formality, "formal", "casual")) if p]
    return ", ".join(parts) if parts else "neutral"


class ConnotationStore:
    """Prime's feelings about ideas, per community (in the vault)."""

    def __init__(self, path: Path | str, *, words: Path | None = None) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA cache_size = -16000")  # 16 MB: fewer disk reads while reading
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS feel (idea TEXT, community TEXT, n INTEGER, valence INTEGER, arousal INTEGER, "
            "formality INTEGER, PRIMARY KEY (idea, community))")
        self.lists = dict(LISTS)
        if words is not None and words.is_file():  # the owner's own additions to the feeling words
            try:
                extra = json.loads(words.read_text(encoding="utf-8"))
                for key in LISTS:
                    self.lists[key] = self.lists[key] | {str(w).lower() for w in extra.get(key, [])}
            except (OSError, ValueError, AttributeError):
                pass

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def observe(self, text: str, community: str = "reading") -> int:
        """Each sentence's tone counts toward every idea in it (sign only: one loud word is one vote)."""
        counted = 0
        for sentence in _SENTENCES.split(text):
            ideas = sentence_ideas(sentence)
            if not ideas:
                continue
            valence, arousal, formality = (_sign(v) for v in tone(sentence, self.lists))
            for idea in ideas[0]:
                self.connection.execute(
                    "INSERT INTO feel VALUES (?, ?, 1, ?, ?, ?) ON CONFLICT(idea, community) DO UPDATE SET "
                    "n = n + 1, valence = valence + excluded.valence, arousal = arousal + excluded.arousal, "
                    "formality = formality + excluded.formality", (idea, community or "reading", valence, arousal, formality))
            counted += 1
        self.connection.commit()
        return counted

    def feelings(self, idea: str) -> list[dict[str, object]]:
        """Per community, most-read first: how the idea feels there."""
        rows = self.connection.execute(
            "SELECT community, n, valence, arousal, formality FROM feel WHERE idea = ? ORDER BY n DESC, community",
            (idea,)).fetchall()
        return [{"community": c, "sentences": n, "feels": describe(n, v, a, f), "valence": round(v / n, 2)}
                for c, n, v, a, f in rows]

    def overall(self, idea: str) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT SUM(n), SUM(valence), SUM(arousal), SUM(formality) FROM feel WHERE idea = ?", (idea,)).fetchone()
        if not row or not row[0] or row[0] < MIN_SENTENCES:
            return None
        n, v, a, f = row
        return {"sentences": n, "feels": describe(n, v, a, f), "valence": round(v / n, 2)}


def explain(store: ConnotationStore, idea: str) -> str:
    idea = " ".join(idea.lower().split())
    found = [f for f in store.feelings(idea) if f["sentences"] >= 2]
    if not found:
        return f"I haven't met {idea} often enough to know how it feels."
    lines = [f"How {idea} feels (from the words around it; a feeling, not a fact):"]
    lines += [f"• in {f['community']}: {f['feels']} ({f['sentences']} sentences)" for f in found[:6]]
    valences = [f["valence"] for f in found if f["sentences"] >= MIN_SENTENCES]
    if len(valences) >= 2 and max(valences) - min(valences) >= 0.5:
        warmest = max(found, key=lambda f: (f["valence"] if f["sentences"] >= MIN_SENTENCES else -9))
        coldest = min(found, key=lambda f: (f["valence"] if f["sentences"] >= MIN_SENTENCES else 9))
        lines.append(f"It feels warmer in {warmest['community']} than in {coldest['community']}: "
                     "whose voice it is changes how it feels.")
    return "\n".join(lines)


def mood_line(store: ConnotationStore, idea: str) -> str | None:
    """One sentence for an answer, only when the feeling is clear."""
    overall = store.overall(idea)
    if overall is None or overall["feels"] == "neutral":
        return None
    return f"Writers usually sound {overall['feels']} about it."
