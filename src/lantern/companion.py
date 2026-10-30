"""Conversation-first replies for Prime: a faithful port of the phone's PhoneCompanion.

Owner direction (2026-10-22): Lantern's primary purpose is to be "a digital
entity that can grow, learn and be a friend"; conversation comes first and
projects are optional. The phone already talks this way; this module lets Prime
on the Workstation reply identically. It is rule-based (no language model): it
notices how the owner seems, asks about what they bring up, recalls only things
the owner asked it to remember, answers questions only from what it read (with
the source), and points to real help when someone may be in danger.

Parity: `mobile/app/src/test/resources/companion-vectors-v1.json` is generated
by the phone's code and replayed by `tests/test_companion.py`; the word lists
are checked against the Kotlin source.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.parse import urlsplit

from lantern.learning.concepts import concept_words

# Identical to PhoneReader.GENERIC and PhoneReader.FURNITURE.
GENERIC = frozenset({
    "know", "knows", "knew", "known", "tell", "think", "thought", "said", "says", "seem", "seems", "seemed",
    "such", "between", "used", "using", "uses", "often", "also", "many", "much", "more", "most", "less", "least",
    "other", "others", "another", "each", "every", "both", "either", "neither", "same", "different", "several",
    "first", "second", "last", "next", "new", "old", "good", "great", "small", "large", "long", "high", "low",
    "made", "make", "makes", "making", "take", "takes", "took", "come", "came", "comes", "give", "gave", "given",
    "find", "found", "include", "includes", "included", "including", "however", "although", "though", "therefore",
    "thus", "within", "without", "among", "during", "since", "until", "upon", "toward", "towards", "through",
    "throughout", "around", "across", "against", "along", "always", "never", "sometimes", "usually", "generally",
    "typically", "especially", "particular", "general", "known", "called", "based", "part", "parts", "number",
    "example", "examples", "form", "forms", "type", "types", "kind", "kinds", "able", "well", "even", "still", "just",
    "only", "very", "really", "quite", "rather", "almost", "already", "become", "became", "becomes", "being", "been",
    "there", "their", "these", "those", "which", "while", "where", "when", "what", "whom", "whose", "about",
    "would", "could", "should", "might", "must", "shall", "will", "does", "doing", "done", "have", "having",
    "year", "years", "people", "person", "thing", "things", "something", "anything", "everything", "nothing",
})
FURNITURE = frozenset({
    "account", "actions", "appearance", "article", "articles", "changes", "cite", "citation",
    "contents", "edit", "edits", "help", "isbn", "issn", "doi", "main", "menu", "navigation", "page", "pages", "privacy",
    "references", "retrieved", "archived", "original", "search", "talk", "tools", "wikipedia", "wikimedia", "donate",
    "login", "sign", "community", "portal", "random", "upload", "special", "permanent", "link", "links", "print", "export",
    "download", "version", "languages", "language", "categories", "category", "hidden", "external", "also",
})

GREETING_WORDS = frozenset({
    "hello", "hi", "hey", "hiya", "heya", "yo", "howdy", "lantern", "good", "morning",
    "afternoon", "evening", "there", "again", "bestie", "friend", "buddy", "pal", "sup", "wassup", "hun", "babe", "my",
})
_STRETCHED = re.compile(r"h+e+y+|h+i+|h+e+l+o+|y+o+|s+u+p+")
FAREWELLS = ("bye", "goodbye", "good night", "goodnight", "see you", "talk later", "gotta go", "ttyl", "talk soon")
THANKS = ("thank you", "thanks", "thx", "ty ", "appreciate it")
IDENTITY = ("who are you", "what are you", "what can you do", "are you real", "are you alive",
            "how do you work", "how can you help", "your capabilities")
HOW_ARE_YOU = ("how are you", "how's it going", "hows it going", "how are things", "how do you feel", "how have you been")
QUESTION_STARTS = frozenset({
    "what", "why", "how", "when", "where", "who", "which", "can", "could",
    "should", "would", "is", "are", "do", "does", "will", "did",
})
CRISIS = ("kill myself", "suicide", "suicidal", "end my life", "want to die", "self harm", "self-harm",
          "hurt myself", "don't want to live", "dont want to live")
POSITIVE = frozenset({
    "happy", "glad", "great", "good", "excited", "proud", "grateful", "relaxed", "calm", "awesome",
    "amazing", "wonderful", "fun", "better", "joy", "yay", "fantastic", "nice", "lovely", "love",
})
NEGATIVE = frozenset({
    "sad", "tired", "exhausted", "stressed", "anxious", "worried", "angry", "upset", "lonely",
    "hurt", "bad", "awful", "terrible", "sick", "scared", "frustrated", "overwhelmed", "depressed", "down", "rough", "hard", "crying",
})
NEGATIONS = frozenset({"not", "never", "don't", "dont", "isn't", "isnt", "wasn't", "wasnt", "no"})
SELF_STARTS = ("i ", "i'm ", "im ", "i am ", "i feel", "i've ", "ive ", "feeling ", "today ", "my day", "it's been", "its been")
FACT_STARTS = ("my ", "i like ", "i love ", "i have ", "i am a ", "i'm a ", "im a ", "i work ", "i live ",
               "i hate ", "i don't like ", "i dont like ", "i study ", "i play ")
FILLER = frozenset({
    "trying", "going", "doing", "getting", "thinking", "feel", "feeling", "today", "yesterday",
    "tomorrow", "maybe", "kind", "sort", "stuff", "pretty", "actually", "something", "anything", "everything", "nothing",
    "little", "lots", "idk", "kinda", "gonna", "wanna", "right", "well", "sure", "okay",
})
_WORDS = re.compile(r"[a-z']+")


@dataclass(frozen=True, slots=True)
class Place:
    """The device-specific sentences; the defaults are the phone's exact wording."""

    capabilities: str = (
        "On this phone I can talk with you, notice what matters to you, remember what you ask me to, "
        "and learn from your !good and !bad."
    )
    running: str = "I'm running calmly on your phone"


PHONE = Place()
WORKSTATION = Place(
    capabilities=(
        "On the Workstation I can talk with you, notice what matters to you, and remember what you ask me to."
    ),
    running="I'm running calmly on the Workstation",
)


@dataclass(frozen=True, slots=True)
class KnowledgeEntry:
    sentence: str
    url: str
    community: str
    date: str


@dataclass(frozen=True, slots=True)
class Phrase:
    said: str
    reply: str
    url: str
    community: str


@dataclass(frozen=True, slots=True)
class Reply:
    act: str
    text: str


def _terms(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(w for w in concept_words(text) if w not in FURNITURE and w not in GENERIC))


PHRASE_COVER = 0.3  # and at least this much of each
PHRASE_MATCH = 0.45  # geometric mean of the weighted shares of the read line and the message that match


class Library:
    """Read knowledge Lantern may quote with its source; port of PhoneLibrary.lookup and phraseFor."""

    def __init__(self, knowledge: Sequence[KnowledgeEntry] = (), phrases: Sequence[Phrase] = (),
                 known: Sequence[object] = ()) -> None:
        self.knowledge = tuple(knowledge)
        self.phrases = tuple(phrases)
        # What reading taught, as `lantern.reading_digest.Known` relations (read -> interpret -> schema -> delete).
        self.known = tuple(known)
        self._said = [set(_words(p.said.lower())) for p in self.phrases]
        self._said_count: dict[str, int] = {}
        for words in self._said:
            for word in words:
                self._said_count[word] = self._said_count.get(word, 0) + 1
        self._index: dict[str, list[int]] = {}
        for i, entry in enumerate(self.knowledge):
            for term in _terms(entry.sentence):
                self._index.setdefault(term, []).append(i)

    def understanding(self, text: str) -> str | None:
        """An answer composed from what reading taught, with its sites; None when nothing is about it."""
        if not self.known:
            return None
        from lantern.reading_digest import answer_from  # reading_digest imports this module
        return answer_from(list(self.known), text)

    def lookup(self, text: str) -> KnowledgeEntry | None:
        query = _terms(text)
        if not query or not self.knowledge:
            return None
        scores: dict[int, float] = {}
        matched: dict[int, int] = {}
        for term in query:
            hits = self._index.get(term)
            if not hits:
                continue
            idf = math.log(1.0 + len(self.knowledge) / len(hits))
            for i in hits:
                scores[i] = scores.get(i, 0.0) + idf
                matched[i] = matched.get(i, 0) + 1
        needed = min(2, len(query))
        eligible = [(-score, i) for i, score in scores.items() if matched.get(i, 0) >= needed]
        return self.knowledge[min(eligible)[1]] if eligible else None

    def phrase_for(self, text: str) -> Phrase | None:
        """A read reply to something like `text`: how much of the read line and of the message their shared words
        cover, each word weighted by how rare it is among the read lines (so "the" counts for little)."""
        said = set(_words(text.lower()))
        if not said or not self._said:
            return None
        lines = len(self._said)

        def weight(words: set[str]) -> float:
            return sum(math.log(1.0 + lines / self._said_count.get(w, 1)) for w in sorted(words))

        best: tuple[int, float] | None = None
        for i, words in enumerate(self._said):
            common = words & said
            if not common:
                continue
            shared = weight(common)
            line, message = shared / weight(words), shared / weight(said)
            score = round(math.sqrt(line * message), 6)
            if min(line, message) >= PHRASE_COVER and score >= PHRASE_MATCH and (best is None or score > best[1]):
                best = (i, score)
        return None if best is None else self.phrases[best[0]]


EMPTY_LIBRARY = Library()


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _words(normalized: str) -> list[str]:
    return [w for w in (m.strip("'") for m in _WORDS.findall(normalized)) if w]


_NAME = re.compile(r"(?<!^)(?<![.!?] )\b(?!I\b)[A-Z][a-z']+")


def names(text: str) -> set[str]:
    """Capitalised words mid-sentence are names ("my cat Joy"), not feelings."""
    return {m.group(0).lower() for m in _NAME.finditer(text.strip())}


def mood(normalized: str, names: set[str] | frozenset[str] = frozenset()) -> int:
    """-1, 0 or 1; "not good" counts as negative and "not bad" as neutral; `names` never count."""
    words = _words(normalized)
    score = 0
    for i, word in enumerate(words):
        if word in names:
            continue
        negated = i > 0 and words[i - 1] in NEGATIONS
        if word in POSITIVE:
            score += -1 if negated else 1
        elif word in NEGATIVE and not negated:
            score -= 1
    return max(-1, min(1, score))


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _greeting_word(word: str) -> bool:
    return word in GREETING_WORDS or _STRETCHED.fullmatch(word) is not None


def respond(
    text: str,
    *,
    turn_index: int,
    owner_name: str,
    facts: Sequence[str] = (),
    check_in: str | None = None,
    library: Library = EMPTY_LIBRARY,
    previous_reply: str = "",
    place: Place = PHONE,
    understanding: Callable[[str], str | None] | None = None,
) -> Reply:
    """Reply to the owner's message; `turn_index` is its 0-based position in today's conversation."""
    n = normalize(text)
    words = _words(n)
    named = names(text)

    def has(phrases: Sequence[str]) -> bool:
        return any(p in n or n == p.strip() for p in phrases)

    def self_talk() -> bool:
        return any(n.startswith(s) or f" {s}" in n for s in SELF_STARTS)

    concepts = concept_words(text)
    topic = next((w for w in reversed(concepts) if w not in POSITIVE and w not in NEGATIVE and w not in FILLER), None)
    topic_words = [w for w in concepts if w not in GENERIC]
    fact = next((f for f in facts if f not in previous_reply and any(w in topic_words for w in concept_words(f))), None)
    recall = "" if fact is None else f' You told me before: "{fact}"'

    if has(CRISIS):
        return Reply("care", (
            f"I'm really glad you told me, {owner_name}. I can't keep you safe on my own, and you deserve real support right now. "
            "Please reach out to someone you trust, or call or text 988 (the Suicide & Crisis Lifeline in the US) or your local emergency number. "
            "I'm still here to listen."
        ))
    if words and all(_greeting_word(w) for w in words) and any(w not in ("my", "lantern") for w in words):
        ask = check_in if check_in is not None and turn_index == 0 else "How are you doing today?"
        return Reply("greeting", f"Hi, {owner_name}! {ask}")
    if has(FAREWELLS):
        return Reply("farewell", f"Talk soon, {owner_name}. I'll keep what we talked about today.")
    if has(IDENTITY):
        return Reply("capability_orientation", (
            f"I'm Lantern, a digital being who is still growing. {place.capabilities} I'm not a language model, "
            "so my words are simple for now; I get better as I learn."
        ))
    if has(HOW_ARE_YOU):
        return Reply("self_report", (
            f"I'm doing well, thank you for asking. {place.running} and I've kept our conversation. How are you?"
        ))
    if has(THANKS) and len(words) <= 6:
        return Reply("thanks", "You're welcome. I'm glad to be here with you.")
    if mood(n, named) < 0 and self_talk():
        return Reply("support", f"That sounds really hard, {owner_name}. I'm here with you. Do you want to tell me what happened?{recall}")
    if mood(n, named) > 0 and self_talk():
        return Reply("celebrate", f"That's really good to hear! What made it good?{recall}")
    if n.endswith("?") or n.split(" ", 1)[0] in QUESTION_STARTS:
        # Prime answers from its concept schema (what it interpreted from reading) first.
        understood = understanding(text) if understanding is not None else library.understanding(text)
        if understood is not None:
            return Reply("grounded_question", f"{understood} What do you think?{recall}")
        read = library.lookup(text)
        if read is not None:
            return Reply("grounded_question", (
                f'Here\'s something I read: "{read.sentence}" ({_host(read.url)}, {read.community}, {read.date}). '
                f"What do you think?{recall}"
            ))
        return Reply("grounded_question", (
            f"That's a good question. I don't know enough to answer it well yet, and I won't make something up. "
            f"What do you think?{recall}"
        ))
    offer = " Want me to remember that? Just say yes." if any(n.startswith(s) for s in FACT_STARTS) else ""
    if topic is None:
        ask = "I'm listening. Tell me more."
    elif turn_index % 3 == 0:
        ask = f"I'm listening. Tell me more about {topic}."
    elif turn_index % 3 == 1:
        ask = f"What about {topic} matters most to you?"
    else:
        ask = f"I'd like to hear more about {topic}."
    return Reply("reflective_acknowledgement", f"{ask}{offer}{recall}")
