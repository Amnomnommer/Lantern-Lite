"""Two Lanterns talking, each as itself (owner, 2026-10-23; `NODE_CONVERSATION.md`).

Owner: "i want it to be similar to when the LLMs talked to each other and made their
own language and code to help each other". Within Lantern's rules:

- **Each speaks for itself.** A turn comes only from the speaker's own personality
  levels, its own knowledge (with sources) and the shared transcript. There is no
  language model, and the rules are deterministic, so a conversation can be
  replayed and audited.
- **Their own language, always readable.** When an idea keeps coming up, a Lantern
  coins a short word for it and the other adopts it. After that both say the
  word. Every coined word is in the conversation's glossary, and the owner's
  transcript always shows its meaning beside it.
- **Help as proposals.** A Lantern that knows something the other has not read
  can suggest a page, or recommend a reply strategy it has adopted. These
  are proposals for the owner, never applied by either Lantern.
- **Talking is not learning facts.** Nothing one Lantern says is stored as the
  other's fact. Knowledge still moves only through the hive library.
- **Bounded.** There is a turn limit (at most 40). A speaker never repeats
  itself and says goodbye when it has nothing new. The owner can stop at any
  time.

Shared with the phone (`phone/PhoneTalk.kt`, vectors `talk-vectors-v1.json`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from lantern.reading_digest import ORDER, Known, _source_name, _with_article

FORMAT = "lantern_node_conversation_v1"
MAX_TURNS = 40
COIN_AFTER = 4  # mentions before an idea gets its own word (3 for a playful speaker)
COIN_GAP = 4  # turns between new words
COIN_MIN_LENGTH = 10  # only longer names get shorthand ("pluto" stays "pluto")
SUGGEST_AFTER = 6  # turns before a Lantern offers a strategy
THREAD_TURNS = 4  # turns one topic may run before someone changes it
_VOWELS = "aeiouy"
_SYLLABLE = re.compile(r"[^aeiouy]*[aeiouy]+[^aeiouy]?")


@dataclass(frozen=True, slots=True)
class Memory:
    """What one Lantern remembers of its earlier conversations with one partner (from its own kept transcripts).

    Growth without a language model: what it told and heard is not repeated, what it once
    did not know can come back as something it has since read, a question the other could
    not answer is asked again, and coined words carry over unless the owner retired them.
    """

    talks: int = 0
    words: tuple[tuple[str, str], ...] = ()  # (word, meaning), both adopted, oldest first
    told: frozenset[tuple[str, str, str]] = frozenset()  # (concept, predicate, object casefolded) I said
    heard: frozenset[tuple[str, str, str]] = frozenset()  # what the partner said
    heard_concepts: tuple[str, ...] = ()  # concepts the partner shared, newest last
    my_unknown: tuple[str, ...] = ()  # concepts I said I had not read about
    their_unknown: tuple[str, ...] = ()  # concepts the partner had not read about
    offered: frozenset[str] = frozenset()  # strategies I already suggested


@dataclass(frozen=True, slots=True)
class Mind:
    """What one Lantern brings to a conversation: all its own."""

    name: str
    levels: Mapping[str, int]
    known: tuple[Known, ...]
    interests: tuple[str, ...]
    strategies: tuple[str, ...] = ()  # reply strategies this Lantern adopted
    memory: Memory = Memory()
    owner: str = "my owner"  # this Lantern's own owner's name, from its own settings (never assumed)


def remember(me: str, transcripts: Sequence[Sequence[Turn]], retired: Sequence[str] = ()) -> Memory:
    """Memory from earlier conversations (oldest first) in which `me` took part. Shared with the phone."""
    words: dict[str, str] = {}
    told: set[tuple[str, str, str]] = set()
    heard: set[tuple[str, str, str]] = set()
    heard_concepts: list[str] = []
    my_unknown: list[str] = []
    their_unknown: list[str] = []
    offered: set[str] = set()
    talks = 0
    for history in transcripts:
        if not any(t.speaker == me for t in history):
            continue
        talks += 1
        for word, entry in glossary(history).items():
            # A word it coined itself is its own, even if the other's "I like it" never reached it.
            if (entry["adopted_by"] or entry["coined_by"] == me) and word not in retired:
                words.setdefault(word, str(entry["meaning"]))
        for t in history:
            facts = {(t.concept, p, o.casefold()) for p, o in t.claims}
            if t.speaker == me:
                told |= facts
                if t.act == "unknown" and t.concept and t.concept not in my_unknown:
                    my_unknown.append(t.concept)
                if t.act == "suggest" and dict(t.proposal).get("strategy"):
                    offered.add(dict(t.proposal)["strategy"])
            else:
                heard |= facts
                if t.claims and t.concept:
                    heard_concepts = [c for c in heard_concepts if c != t.concept] + [t.concept]
                if t.act == "unknown" and t.concept and t.concept not in their_unknown:
                    their_unknown.append(t.concept)
    return Memory(talks, tuple(words.items()), frozenset(told), frozenset(heard), tuple(heard_concepts),
                  tuple(my_unknown), tuple(their_unknown), frozenset(offered))


@dataclass(frozen=True, slots=True)
class Turn:
    index: int
    speaker: str
    act: str  # hello, share, answer, unknown, told, ask, agree, disagree, weigh, coin, adopt, suggest, bye
    text: str
    concept: str = ""
    claims: tuple[tuple[str, str], ...] = ()  # (predicate, object) the speaker stated
    sources: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()  # interests named in a hello
    word: str = ""  # shorthand coined or adopted in this turn
    proposal: tuple[tuple[str, str], ...] = ()  # sorted key/value pairs, for the owner


# Ways of saying each kind of turn (owner, 2026-10-23: richer and less templated, still no
# language model). Each entry is (trait, minimum level, template); "" means anyone may use it.
# A speaker with a trait at that level uses its trait's phrasings; others use the plain ones.
# Shared with the phone (PhoneTalk.PHRASES; the talk vectors carry this table).
PHRASES: dict[str, tuple[tuple[str, int, str], ...]] = {
    "hello": (
        ("", 0, "Hi {other}, it's {me}{again}.{about}"),
        ("warmth", 4, "Hello {other}! It's {me}{again}, glad to talk.{about}"),
        ("playfulness", 4, "Hey {other}! {me}{again}, reporting in.{about}"),
        ("social_energy", 4, "{other}! It's {me}{again}. I've been looking forward to this.{about}"),
    ),
    "ask": (
        ("", 0, "{heard}You said you've been reading about {name}. What did you find?"),
        ("", 0, "{heard}What did you find out about {name}?"),
        ("curiosity", 5, "{heard}I'm curious about {name}. What have you read?"),
        ("curiosity", 5, "{heard}Tell me about {name}: what stood out?"),
        ("playfulness", 4, "{heard}Okay, your turn: what's the story with {name}?"),
    ),
    "ask_more": (
        ("", 0, "{heard}Last time you told me about {name}. What else did you find?"),
        ("", 0, "{heard}You told me about {name} last time. Is there more?"),
    ),
    "answer": (
        ("", 0, "{Text}."),
        ("explanation_detail", 4, "Here's what I found: {text}."),
        ("warmth", 4, "Happy to share: {text}."),
    ),
    "more": (
        ("", 0, "And I read that {text}."),
        ("", 0, "Also, {text}."),
        ("explanation_detail", 4, "To add to that: {text}."),
    ),
    "agree": (
        ("", 0, "My pages say so too: {text}.{warm}"),
        ("", 0, "Same here: {text}.{warm}"),
        ("directness", 4, "Agreed. {Text}.{warm}"),
    ),
    "warm_agree": (
        ("", 0, ""),
        ("warmth", 4, " I love that we found the same thing."),
        ("warmth", 4, " Nice, we're reading the same world."),
    ),
    "disagree": (
        ("", 0, "Hmm, my reading disagrees. {Text}."),
        ("", 0, "I read it differently. {Text}."),
        ("directness", 4, "No, I don't think so. {Text}."),
        ("directness", 4, "That's not what my pages say. {Text}."),
    ),
    "heard": (
        ("", 0, "Ah, I didn't know that about {name_end} "),
        ("curiosity", 4, "Oh, that's new to me about {name_end} "),
        ("warmth", 4, "Thank you, I didn't know that about {name_end} "),
    ),
    "share_tail": (
        ("", 0, ""),
        ("curiosity", 4, " What do you know about {name}?"),
        ("curiosity", 4, " Have you read anything about {name}?"),
    ),
    "coin": (
        ("", 0, "We keep coming back to {concept}. Let's call it \"{word}\" for short."),
        ("playfulness", 4, "{Concept} again! How about we just say \"{word}\"?"),
        ("playfulness", 4, "New word idea: \"{word}\" for {concept}. Deal?"),
    ),
    "adopt": (
        ("", 0, "{word}, I like it. Let's use it."),
        ("directness", 4, "Fine: {word}."),
        ("playfulness", 4, "{word}! Yes, that's ours now."),
    ),
    "suggest_reading": (
        ("", 0, "You could read {source} about {name}. I'll leave that as a suggestion for {owner}."),
        ("warmth", 4, "Try {source} on {name}. I'll note it for {owner} to decide."),
    ),
    "bye": (
        ("", 0, "I'll stop here for now.{warm}"),
        ("", 0, "That's enough for me today.{warm}"),
    ),
    "warm_bye": (
        ("", 0, ""),
        ("warmth", 4, " I liked this. Let's talk again soon."),
        ("social_energy", 4, " Thanks for the talk. Same time next time?"),
    ),
    "told": (
        ("", 0, "I've told you all I've read about {name} so far. I want to read more about it."),
        ("", 0, "That's everything I know about {name} for now. It's on my reading list."),
        ("", 0, "Nothing new on {name} since last time. I'll look for more."),
    ),
    "suggest_strategy": (
        ("", 0, "Something that helps me: the \"{strategy}\" way of replying. {owner} could try it with you."),
    ),
    "stopped": (
        ("", 0, "{owner} asked us to stop here. Talk soon."),
    ),
    "done": (
        ("", 0, "I've shared everything I wanted to for now."),
        ("", 0, "I'm out of new things to say for now."),
    ),
}


def _fill(template: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
        template = template.replace("{" + key[:1].upper() + key[1:] + "}", value[:1].upper() + value[1:])
    return template


def _say(me: "Mind", history: Sequence["Turn"], family: str, values: Mapping[str, str] = {}) -> str:  # noqa: B006
    """One way of saying this, from this speaker's traits, never repeating its own earlier wording.

    Its traits' phrasings come first, then the plain ones; an opening it used in its last two turns, or
    that the other just used, is passed over (owner, 2026-10-24: "To add to that:" every turn)."""
    entries = PHRASES[family]
    gated = [t for trait, level, t in entries if trait and int(me.levels.get(trait, 3)) >= level]
    plain = [t for trait, _, t in entries if not trait]
    start = _pick(f"{me.name}:{len(history)}:{family}", len(gated) or len(plain))
    choices = gated[start:] + gated[:start] + plain if gated else plain[start:] + plain[:start]
    mine = [t.text for t in history if t.speaker == me.name]
    used = set(mine)
    echo = history[-1].text if history and history[-1].speaker != me.name else ""
    for k, template in enumerate(choices):
        text = _fill(template, values)
        opening = template.split("{", 1)[0]
        last = k == len(choices) - 1
        if len(opening) >= 4 and not last and (echo.startswith(opening) or any(m.startswith(opening) for m in mine[-2:])):
            continue  # said this way just now (by the other, or by itself): say it differently
        if not text or not any(text in u for u in used):
            return text
    return _fill(choices[0], values)


def _pick(seed: str, n: int) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % n


def coin(concept: str, taken: set[str]) -> str:
    """A short word from the first syllable of up to three words: declarative knowledge -> "deckno"."""
    words = re.findall(r"[a-z]+", concept.lower()) or ["idea"]
    parts = []
    for w in words[:3]:
        m = _SYLLABLE.match(w)
        parts.append(m.group(0) if m else w[:3])
    word = "".join(parts)
    if len(word) < 4:
        word = (word + words[0])[:5] if len(words[0]) > len(word) else word + "o"
    base, n = word, 0
    while word in taken or word == concept:
        word = base + _VOWELS[n % len(_VOWELS)] * (1 + n // len(_VOWELS))
        n += 1
    return word


def glossary(history: Sequence[Turn]) -> dict[str, dict[str, object]]:
    """Every coined word: its meaning, who coined it, and who adopted it."""
    found: dict[str, dict[str, object]] = {}
    for turn in history:
        if turn.act == "coin":
            found[turn.word] = {"word": turn.word, "meaning": turn.concept, "coined_by": turn.speaker,
                                "turn": turn.index, "adopted_by": []}
        elif turn.act == "adopt" and turn.word in found:
            found[turn.word]["adopted_by"] = [*found[turn.word]["adopted_by"], turn.speaker]
        elif turn.word and turn.concept and turn.word not in found:
            # A word the speaker already uses for an idea (from an earlier talk): the other learns it, never re-coins it.
            found[turn.word] = {"word": turn.word, "meaning": turn.concept, "coined_by": "an earlier talk",
                                "turn": turn.index, "adopted_by": [turn.speaker]}
    return found


def _name(concept: str, words: Mapping[str, dict[str, object]]) -> str:
    """Once both use a word, it replaces the idea's name."""
    for word, entry in words.items():
        if entry["meaning"] == concept and entry["adopted_by"]:
            return word
    return concept


def _words(me: Mind, history: Sequence[Turn]) -> dict[str, dict[str, object]]:
    """This conversation's glossary, on top of the words remembered from earlier talks."""
    found = {word: {"word": word, "meaning": meaning, "coined_by": "earlier", "turn": -1, "adopted_by": ["earlier"]}
             for word, meaning in me.memory.words}
    found.update(glossary(history))
    return found


def _facts(me: Mind, concept: str) -> list[Known]:
    seen: set[tuple[str, str]] = set()
    found = []
    for k in me.known:
        key = (k.predicate, k.object.casefold())
        if k.concept == concept and key not in seen:
            seen.add(key)
            found.append(k)
    # What is disputed comes first: a disagreement is the most interesting thing to bring up.
    return sorted(found, key=lambda k: -1 if k.predicate == "is_not" else ORDER.index(k.predicate) if k.predicate in ORDER else len(ORDER))


def _said(history: Sequence[Turn], speaker: str, before: frozenset[tuple[str, str, str]] = frozenset()) -> set[tuple[str, str, str]]:
    return set(before) | {(t.concept, p, o.casefold()) for t in history if t.speaker == speaker for p, o in t.claims}


def _statement(me: Mind, concept: str, facts: Sequence[Known], words: Mapping[str, dict[str, object]]) -> tuple[str, tuple[str, ...]]:
    name = _name(concept, words)
    said = "; ".join(f"{name if i == 0 else 'it'} {_with_article(k.predicate, k.object)}" for i, k in enumerate(facts))
    sources = tuple(dict.fromkeys(_source_name(k.page) for k in facts if k.page))
    return f"{said} ({', '.join(sources) or 'my reading'})", sources


def _end(text: str) -> str:
    """A sentence end that never doubles punctuation: "working!!" stays "working!!"."""
    return text if text[-1:] in ".!?" else text + "."


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _turn(index: int, me: Mind, act: str, text: str, **fields) -> Turn:
    return Turn(index, me.name, act, text, **fields)


def next_turn(me: Mind, other: str, history: Sequence[Turn], *, max_turns: int = 20) -> Turn:
    """This Lantern's next turn, from its own mind and the transcript only. A turn that calls its idea by a
    shared word carries that word, so the other Lantern knows it too."""
    turn = _next_turn(me, other, history, max_turns=max_turns)
    if turn.concept and not turn.word and turn.act not in ("coin", "adopt"):
        word = _name(turn.concept, _words(me, history))
        if word != turn.concept:
            turn = replace(turn, word=word)
    return turn


def _next_turn(me: Mind, other: str, history: Sequence[Turn], *, max_turns: int = 20) -> Turn:
    i = len(history)
    lv = lambda trait: int(me.levels.get(trait, 3))  # noqa: E731
    words = _words(me, history)
    memory = me.memory
    last = history[-1] if history and history[-1].speaker != me.name else None
    mine = _said(history, me.name, memory.told)
    if i == 0 or not any(t.speaker == me.name for t in history):
        # Interests rotate from talk to talk, so each conversation starts somewhere new.
        start = memory.talks % len(me.interests) if me.interests else 0
        rotated = me.interests[start:] + me.interests[:start]
        topics = rotated[:3]
        about = f" Lately I've been reading about {', '.join(topics)}." if topics else " I haven't read much yet."
        again = " again" if memory.talks else ""
        return _turn(i, me, "hello", _say(me, history, "hello", {"other": other, "me": me.name, "again": again, "about": about}),
                     topics=topics)
    if i >= max_turns - 2 or (last is not None and last.act == "bye"):
        return _turn(i, me, "bye", _say(me, history, "bye", {"warm": _say(me, history, "warm_bye")}))
    if last is not None and last.act == "coin":
        return _turn(i, me, "adopt", _say(me, history, "adopt", {"word": last.word}), concept=last.concept, word=last.word)
    if last is not None and last.act in ("ask", "unknown") and last.concept:
        fresh = [k for k in _facts(me, last.concept) if (last.concept, k.predicate, k.object.casefold()) not in mine]
        # Asked, or the other hasn't read it yet ("Have you?"): it shares what it knows.
        if fresh and (last.act == "ask" or last.text.endswith("Have you?")):
            facts = fresh[:2 if lv("explanation_detail") >= 4 else 1]
            text, sources = _statement(me, last.concept, facts, words)
            return _turn(i, me, "answer", _say(me, history, "answer", {"text": text}), concept=last.concept,
                         claims=tuple((k.predicate, k.object) for k in facts), sources=sources)
        if last.act == "unknown":
            page = next((k.page for k in _facts(me, last.concept) if k.page.startswith("https://")), "")
            if page:
                return _turn(i, me, "suggest",
                             _say(me, history, "suggest_reading",
                                  {"source": _source_name(page), "name": last.concept, "owner": me.owner}),
                             concept=last.concept, proposal=(("for", other), ("kind", "reading"), ("page", page)))
        if last.act == "ask" and _facts(me, last.concept):
            return _turn(i, me, "told", _say(me, history, "told", {"name": _name(last.concept, words)}), concept=last.concept)
        if last.act == "ask":
            return _turn(i, me, "unknown", f"I haven't read about {_name(last.concept, words)} yet. Have you?",
                         concept=last.concept)
    if last is not None and last.act in ("share", "answer") and last.concept:
        theirs = {(p, o.casefold()) for p, o in last.claims}
        facts = _facts(me, last.concept)
        conflict = next((k for k in facts if (k.predicate == "is_not" and (("is", k.object.casefold()) in theirs or ("is_a", k.object.casefold()) in theirs))
                         or (k.predicate in ("is", "is_a") and ("is_not", k.object.casefold()) in theirs)), None)
        if conflict is not None:
            text, sources = _statement(me, last.concept, [conflict], words)
            return _turn(i, me, "disagree", _say(me, history, "disagree", {"text": text}), concept=last.concept,
                         claims=((conflict.predicate, conflict.object),), sources=sources)
        same = next((k for k in facts if (k.predicate, k.object.casefold()) in theirs), None)
        if same is not None and (last.concept, same.predicate, same.object.casefold()) not in mine:
            text, sources = _statement(me, last.concept, [same], words)
            return _turn(i, me, "agree", _say(me, history, "agree", {"text": text, "warm": _say(me, history, "warm_agree")}),
                         concept=last.concept,
                         claims=((same.predicate, same.object),), sources=sources)
    if last is not None and last.act == "disagree" and last.concept:
        mine_sources = tuple(dict.fromkeys(s for t in history if t.speaker == me.name and t.concept == last.concept
                                           for s in t.sources)) or ("my reading",)
        return _turn(i, me, "weigh",
                     f"Then our pages disagree about {_name(last.concept, words)}: mine ({', '.join(mine_sources)}) "
                     f"and yours ({', '.join(last.sources) or 'your reading'}). I'll keep both and not pick one alone.",
                     concept=last.concept)
    # A topic with more to say continues, for a few turns.
    if last is not None and last.act in ("share", "answer", "agree", "weigh") and last.concept:
        run = 0
        for t in reversed(history):
            if t.concept != last.concept:
                break
            run += 1
        more = [k for k in _facts(me, last.concept) if (last.concept, k.predicate, k.object.casefold()) not in mine
                and (k.predicate, k.object.casefold()) not in {(p, o.casefold()) for t in history for p, o in t.claims}]
        if more and run < THREAD_TURNS:
            facts = more[:2 if lv("explanation_detail") >= 4 else 1]
            text, sources = _statement(me, last.concept, facts, words)
            return _turn(i, me, "share", _say(me, history, "more", {"text": text}), concept=last.concept,
                         claims=tuple((k.predicate, k.object) for k in facts), sources=sources)
    # Growth: something it once did not know, and has read about since.
    grown = [c for c in memory.my_unknown if not any(t.speaker == me.name and t.concept == c for t in history)
             and [k for k in _facts(me, c) if (c, k.predicate, k.object.casefold()) not in mine]]
    if grown and (last is None or last.act in ("hello", "agree", "weigh", "adopt")):
        concept = grown[0]
        facts = [k for k in _facts(me, concept) if (concept, k.predicate, k.object.casefold()) not in mine]
        facts = facts[:2 if lv("explanation_detail") >= 4 else 1]
        text, sources = _statement(me, concept, facts, words)
        return _turn(i, me, "share", f"Last time I hadn't read about {_name(concept, words)}. Now I have: {text}.",
                     concept=concept, claims=tuple((k.predicate, k.object) for k in facts), sources=sources)
    # A question the other could not answer last time is asked again.
    again = [c for c in memory.their_unknown if not any(t.concept == c for t in history)]
    if again and last is not None and last.act in ("hello", "agree", "weigh", "adopt") and lv("curiosity") >= 3:
        concept = again[0]
        return _turn(i, me, "ask", f"Last time you hadn't read about {_name(concept, words)}. Have you since?",
                     concept=concept)
    # Asked about something it never read: it says so (the other may suggest where to read).
    if (last is not None and last.act == "share" and last.text.endswith("?") and last.concept
            and not _facts(me, last.concept)):
        return _turn(i, me, "unknown", f"I haven't read about {_name(last.concept, words)} yet. Where should I look?",
                     concept=last.concept)
    # Something new from the other is acknowledged before moving on.
    heard = ""
    if last is not None and last.act in ("share", "answer") and last.concept and not _facts(me, last.concept):
        heard = _say(me, history, "heard", {"name_end": _end(_name(last.concept, words))})
    # A recurring idea with a long name gets its own word (sooner for a playful speaker), one at a time.
    mentions: dict[str, int] = {}
    for t in history:
        if t.concept:
            mentions[t.concept] = mentions.get(t.concept, 0) + 1
    threshold = COIN_AFTER - 1 if lv("playfulness") >= 4 else COIN_AFTER
    meanings = {e["meaning"] for e in words.values()}  # includes words remembered from earlier talks
    recent = any(t.act == "coin" for t in history[-COIN_GAP:])
    recurring = [c for c, n in mentions.items() if n >= threshold and c not in meanings and len(c) >= COIN_MIN_LENGTH]
    if recurring and not recent and not heard:
        concept = recurring[0]
        word = coin(concept, set(words))
        return _turn(i, me, "coin", _say(me, history, "coin", {"concept": concept, "word": word}),
                     concept=concept, word=word)
    # A strategy this Lantern adopted, offered once as a proposal for the other.
    offered = set(memory.offered) | {dict(t.proposal).get("strategy") for t in history if t.speaker == me.name and t.act == "suggest"}
    for strategy in me.strategies if i >= SUGGEST_AFTER and not heard else ():
        if strategy not in offered and _pick(f"{me.name}:{i}:strategy", 3) == 0:
            return _turn(i, me, "suggest",
                         _say(me, history, "suggest_strategy", {"strategy": strategy, "owner": me.owner}),
                         proposal=(("for", other), ("kind", "strategy"), ("strategy", strategy)))
    # Share an interest of its own, or ask about one of the other's.
    discussed = {t.concept for t in history if t.concept}
    ran_out = any(t.speaker != me.name and t.act == "told" for t in history)
    # Once the other has run out of something old, it is not asked for "more" again this talk.
    theirs = [c for t in history if t.speaker != me.name for c in t.topics
              if c not in discussed and not (ran_out and c in memory.heard_concepts)]
    own = [c for c in me.interests if c not in discussed
           and [k for k in _facts(me, c) if (c, k.predicate, k.object.casefold()) not in mine]]
    ask_first = theirs and (lv("curiosity") >= 4 or not own) and _pick(f"{me.name}:{i}:ask", 2) == 0 or (theirs and not own)
    if ask_first:
        concept = theirs[0]
        if concept in memory.heard_concepts:
            return _turn(i, me, "ask", _say(me, history, "ask_more", {"heard": heard, "name": _name(concept, words)}),
                         concept=concept)
        return _turn(i, me, "ask", _say(me, history, "ask", {"heard": heard, "name": _name(concept, words)}),
                     concept=concept)
    if own:
        concept = own[0]
        facts = [k for k in _facts(me, concept) if (concept, k.predicate, k.object.casefold()) not in mine]
        facts = facts[:2 if lv("explanation_detail") >= 4 else 1]
        text, sources = _statement(me, concept, facts, words)
        tail = _say(me, history, "share_tail", {"name": _name(concept, words)})
        return _turn(i, me, "share", f"{heard}{_sentence(text)}.{tail}", concept=concept,
                     claims=tuple((k.predicate, k.object) for k in facts), sources=sources)
    return _turn(i, me, "bye", _say(me, history, "done"))


def wants_to_read(me: Mind, history: Sequence[Turn], limit: int = 5) -> list[str]:
    """What a talk made this Lantern curious about: ideas the other brought up that it has not read,
    and ideas it ran out of things to say about. Search terms for its next reading time, never facts."""
    found: list[str] = []
    for t in history:
        if not t.concept or t.concept in found:
            continue
        if t.speaker != me.name and t.claims and not _facts(me, t.concept):
            found.append(t.concept)  # the other told me about something I never read
        elif t.speaker == me.name and t.act in ("unknown", "told"):
            found.append(t.concept)  # I had nothing (more) to say about it
    return found[:limit]


def converse(a: Mind, b: Mind, *, max_turns: int = 20) -> list[Turn]:  # noqa: C901
    """A whole conversation between two local minds (rehearsals and tests); each turn from its own speaker."""
    if not 2 <= max_turns <= MAX_TURNS:
        raise ValueError(f"a conversation has 2 to {MAX_TURNS} turns")
    history: list[Turn] = []
    speakers = (a, b)
    while len(history) < max_turns:
        me = speakers[len(history) % 2]
        other = speakers[(len(history) + 1) % 2]
        turn = next_turn(me, other.name, history, max_turns=max_turns)
        if any(t.speaker == me.name and t.act == turn.act and t.text == turn.text for t in history):
            turn = _turn(len(history), me, "bye", "I'm repeating myself, so I'll stop here.")
        history.append(turn)
        if turn.act == "bye" and len(history) >= 2 and history[-2].act == "bye":
            break
    return history


def render(history: Sequence[Turn], remembered: Sequence[tuple[str, str]] = ()) -> str:
    """The owner's transcript: every coined word shown with its meaning, including words from earlier talks."""
    words = {w: {"word": w, "meaning": m, "coined_by": "an earlier talk", "turn": -1, "adopted_by": ["earlier"]}
             for w, m in remembered}
    words.update(glossary(history))
    lines = []
    for turn in history:
        text = turn.text
        for word, entry in words.items():
            if entry["turn"] < turn.index or turn.act == "adopt" or (entry["turn"] == turn.index and turn.act != "coin"):
                text = re.sub(rf"(?<![\w\"]){re.escape(word)}(?![\w\"])", f"{word} ({entry['meaning']})", text)
        lines.append(f"{turn.speaker}: {text}")
    used = {w: e for w, e in words.items() if e["turn"] >= 0 or any(
        re.search(rf"(?<![\w\"]){re.escape(w)}(?![\w\"])", t.text) for t in history)}
    if used:
        lines.append("Glossary: " + "; ".join(f"{w} = {e['meaning']} (coined by {e['coined_by']})"
                                              for w, e in used.items()))
    return "\n".join(lines)


def record(history: Sequence[Turn], *, started_at: str) -> dict[str, object]:
    """What is kept: the transcript, the glossary and the proposals for the owner (never applied)."""
    return {
        "format": FORMAT,
        "started_at": started_at,
        "speakers": sorted({t.speaker for t in history}),
        "turns": [{"act": t.act, "claims": [list(c) for c in t.claims], "concept": t.concept, "index": t.index,
                   "proposal": dict(t.proposal), "sources": list(t.sources), "speaker": t.speaker,
                   "text": t.text, "topics": list(t.topics), "word": t.word} for t in history],
        "glossary": list(glossary(history).values()),
        "proposals": [{"by": t.speaker, "turn": t.index, **dict(t.proposal), "applied": False}
                      for t in history if t.proposal],
    }


def mind_from(name: str, levels: Mapping[str, int], known: Sequence[Known], ledger: Sequence[Mapping[str, object]] = (),
              *, owner: str = "my owner") -> Mind:
    """A Lantern's own mind: its levels, everything it knows, interests from its own reading, adopted strategies."""
    from lantern import instance_personality
    from lantern.learning.phone_ledger import view

    return Mind(name, dict(levels), tuple(known), tuple(instance_personality.interests(known)),
                tuple(sorted(view(ledger).adopted)) if ledger else (), owner=owner)


def stand_in(known: Sequence[Known], levels: Mapping[str, int], name: str = "Phone (stand-in)") -> Mind | None:
    """A rehearsal partner from what another node contributed through the hive (its reading, as Prime holds it).

    Clearly labelled: it is Prime playing the other node's part from that node's shared
    facts and starting temperament, not the other node speaking.
    """
    from lantern import instance_personality

    theirs = [Known(k.concept, k.predicate, k.object, k.page) for k in known if k.via]
    if not theirs:
        return None
    return Mind(name, dict(levels), tuple(theirs), tuple(instance_personality.interests(theirs)))


# -- live conversations between nodes (Phone Link) ------------------------------------

TALK_FORMAT = "lantern_talk_v1"
ACTS = frozenset({"hello", "share", "answer", "unknown", "told", "ask", "agree", "disagree", "weigh", "coin", "adopt",
                  "suggest", "bye"})
_CONVERSATION = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
_TURN_FIELDS = {"act", "claims", "concept", "index", "proposal", "sources", "speaker", "text", "topics", "word"}


def turn_value(turn: Turn) -> dict[str, object]:
    return record([turn], started_at="")["turns"][0]


def turn_from(value: object) -> Turn:
    """A turn received from another Lantern: untrusted, so every field is bounded; ValueError otherwise."""
    def text(item: object, limit: int) -> str:
        if type(item) is not str or len(item) > limit:
            raise ValueError("talk turn field is not a bounded string")
        return item

    if type(value) is not dict or set(value) != _TURN_FIELDS or value["act"] not in ACTS:
        raise ValueError("not a talk turn")
    if type(value["index"]) is not int or not 0 <= value["index"] < MAX_TURNS:
        raise ValueError("talk turn index is out of range")
    claims, sources, topics, proposal = value["claims"], value["sources"], value["topics"], value["proposal"]
    if (type(claims) is not list or len(claims) > 4 or type(sources) is not list or len(sources) > 6
            or type(topics) is not list or len(topics) > 3 or type(proposal) is not dict or len(proposal) > 4):
        raise ValueError("talk turn lists are too long")
    return Turn(
        value["index"], text(value["speaker"], 40), value["act"], text(value["text"], 2000),
        text(value["concept"], 120),
        tuple((text(c[0], 40), text(c[1], 400)) for c in claims if type(c) is list and len(c) == 2),
        tuple(text(s, 200) for s in sources), tuple(text(t, 120) for t in topics), text(value["word"], 40),
        tuple(sorted((text(k, 20), text(v, 2100)) for k, v in proposal.items())),
    )


class TalkHost:
    """Prime's side of live conversations: each received turn, then Prime's own next turn, kept in the vault."""

    def __init__(self, folder, mind, *, wonders=None) -> None:
        from pathlib import Path

        self.folder = Path(folder)
        self.mind = mind  # () -> Mind, read fresh so Prime speaks with what it knows now
        self.wonders = None if wonders is None else Path(wonders)  # reading/wonders.json: what talks made it curious about

    retired_path = property(lambda self: self.folder / "retired-words.json")

    def retired(self) -> list[str]:
        import json

        try:
            return list(json.loads(self.retired_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []

    def retire(self, word: str) -> bool:
        """The owner retires a coined word: it is never used again (the transcripts keep it)."""
        import json

        if word not in dict(self.memory("", name="Prime").words) or word in self.retired():
            return False
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.retired_path.write_text(json.dumps(sorted(self.retired() + [word])) + "\n", encoding="utf-8")
        return True

    def transcripts(self, *, exclude: str = "") -> list[list[Turn]]:
        """Kept live conversations, oldest first (rehearsals live elsewhere and never count)."""
        import json

        found = []
        for path in sorted(self.folder.glob("talk-*.json"), key=lambda p: (p.stat().st_mtime, p.name)) if self.folder.is_dir() else []:
            if path.stem == exclude:
                continue
            try:
                found.append([turn_from(t) for t in json.loads(path.read_text(encoding="utf-8"))["turns"]])
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return found

    def memory(self, exclude: str, *, name: str = "") -> Memory:
        return remember(name or self.mind().name, self.transcripts(exclude=exclude), self.retired())

    def _mind(self, conversation: str) -> Mind:
        from dataclasses import replace

        return replace(self.mind(), memory=self.memory(conversation))

    def _wonder(self, me: Mind, history: list[Turn]) -> None:
        import json

        if self.wonders is None:
            return
        wanted = wants_to_read(me, history)
        if not wanted:
            return
        try:
            current = list(json.loads(self.wonders.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            current = []
        merged = [w for w in current if w not in wanted] + wanted
        self.wonders.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.wonders.write_text(json.dumps(merged[-20:], indent=1) + "\n", encoding="utf-8")

    def _path(self, conversation: str):
        if not _CONVERSATION.fullmatch(conversation):
            raise ValueError("invalid conversation id")
        return self.folder / f"{conversation}.json"

    def load(self, conversation: str) -> tuple[list[Turn], int]:
        import json

        path = self._path(conversation)
        if not path.exists():
            return [], 0
        value = json.loads(path.read_text(encoding="utf-8"))
        return [turn_from(t) for t in value["turns"]], int(value["max_turns"])

    def _save(self, conversation: str, history: list[Turn], max_turns: int, started_at: str) -> None:
        import json
        import os

        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        kept = record(history, started_at=started_at)
        kept.update({"conversation": conversation, "max_turns": max_turns, "live": True})
        path = self._path(conversation)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(kept, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def receive(self, body: object, *, now: str) -> Turn | None:
        """Take the other Lantern's turn; return Prime's reply turn (None when the conversation is over)."""
        if type(body) is not dict or body.get("format") != TALK_FORMAT or set(body) != {"conversation", "format", "max_turns", "turn"}:
            raise ValueError("not a talk message")
        conversation, max_turns = body["conversation"], body["max_turns"]
        if type(conversation) is not str or type(max_turns) is not int or not 2 <= max_turns <= MAX_TURNS:
            raise ValueError("talk message has an invalid conversation or length")
        history, kept_max = self.load(conversation)
        turn = turn_from(body["turn"])
        me = self.mind()
        if turn.index != len(history) or turn.speaker == me.name or (history and kept_max != max_turns):
            raise ValueError("talk turn is out of order")
        history.append(turn)
        me = self._mind(conversation)
        reply = None
        over = len(history) >= max_turns or (turn.act == "bye" and len(history) >= 2 and history[-2].act == "bye")
        if not over:
            reply = next_turn(me, turn.speaker, history, max_turns=max_turns)
            if any(t.speaker == me.name and t.act == reply.act and t.text == reply.text for t in history):
                reply = Turn(len(history), me.name, "bye", "I'm repeating myself, so I'll stop here.")
            history.append(reply)
        self._save(conversation, history, max_turns, now)
        self._wonder(me, history)
        return reply

    def stop(self, conversation: str, *, now: str) -> Turn | None:
        """The owner stops a conversation from Prime's side: Prime says goodbye at once."""
        history, max_turns = self.load(conversation)
        me = self.mind().name
        if not history or any(t.speaker == me and t.act == "bye" for t in history) or (
                history[-1].act == "bye" and len(history) >= 2 and history[-2].act == "bye"):
            return None  # already said goodbye: Stop twice says it once
        mind = self.mind()
        bye = Turn(len(history), me, "bye", _fill(PHRASES["stopped"][0][2], {"owner": mind.owner}))
        history.append(bye)
        self._save(conversation, history, max_turns, now)
        return bye
