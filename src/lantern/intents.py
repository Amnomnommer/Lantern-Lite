"""No commands needed on Prime: the phone's PhoneIntents phrasings, shared word for word.

Plain sentences that clearly ask for something become the existing command, so each
effect stays the same tested, reversible operation. Only whole, explicit phrasings
match; a bare "yes" counts only right after Lantern offered to remember something.
The phrase sets are identical to `PhoneIntents.kt` (checked by tests/test_intents.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OFFER = "Want me to remember that?"

REMEMBER_LAST = frozenset({"remember that", "remember this", "remember it", "please remember that",
    "please remember", "yes remember that", "yes please remember that", "yes remember it", "yes please remember",
    "can you remember that", "save that", "keep that in mind", "don't forget that", "dont forget that"})
YES = frozenset({"yes", "yes please", "yeah", "yep", "sure", "please do", "ok", "okay", "yes do", "do it"})
FORGET_LAST = frozenset({"forget that", "forget it", "please forget that", "never mind forget that",
    "nevermind forget that", "forget what i just told you", "don't remember that", "dont remember that",
    "delete that memory", "forget that memory"})
GOOD = frozenset({"good answer", "good reply", "great answer", "great reply", "nice answer", "nice reply",
    "good job", "well said", "that was a good answer", "that was a good reply", "that was a great answer",
    "perfect answer", "good response", "that was a good response", "i liked that answer"})
BAD = frozenset({"bad answer", "bad reply", "bad response", "that was a bad answer", "that was a bad reply",
    "that wasn't a good answer", "that wasnt a good answer", "wrong answer", "not a good answer", "that answer was bad"})
KEEP = frozenset({"keep doing that", "keep replying like that", "keep talking like that", "i like when you do that",
    "i like it when you do that", "do that more", "do that more often", "more of that"})
STOP = frozenset({"stop doing that", "don't do that", "dont do that", "don't reply like that",
    "dont reply like that", "stop replying like that", "please stop doing that", "less of that"})
MEMORIES = frozenset({"what do you remember about me", "what do you know about me", "what have i told you",
    "what do you remember", "show me what you remember", "what are your memories of me"})
LIBRARY = frozenset({"what have you been reading", "what did you read", "what have you read",
    "what have you learned from reading", "what did you learn from reading", "what do you know from reading",
    "what are you reading", "show me your library"})
LEARNING = frozenset({"what are you learning", "how are you learning", "what have you been learning",
    "show me what you're learning", "show me what you are learning"})
READING = frozenset({"go read", "go read something", "read something", "you can read", "you can read now",
    "go learn something", "go and read", "go explore", "you can go read", "time to read", "go read for a bit",
    "do some reading", "go online and read", "you can go online"})
FILES = frozenset({"read a file", "read my file", "read this file", "can you read a file", "read a book",
    "read my book", "can you read my book"})

_OPENER = re.compile(r"^(?:(?:hey|hi|ok|okay|lantern),? )+", re.IGNORECASE)
_REMEMBER_THAT = re.compile(r"^(?:please,? )?(?:remember|don'?t forget),? that (.{3,500})$", re.IGNORECASE)
_FORGET_ABOUT = re.compile(r"^(?:please,? )?forget (?:about |what i said about )(.{2,200})$", re.IGNORECASE)
_CORRECTION = re.compile(r"^(?:no,? i meant|correction:?|change that to|update that to|correct that to|fix that:?) (.{2,500})$",
                         re.IGNORECASE)
_CALL_ME = re.compile(r"^(?i:(?:please,? )?call me) ([^\W\d_][\w'-]{0,30}(?: [^\W\d_][\w'-]{0,30}){0,2})$")


@dataclass(frozen=True, slots=True)
class Route:
    action: str  # remember, remember_words, forget_newest, forget_about, correct, good, bad, keep, stop, name,
    #              memories, library, learning, say
    text: str = ""


def route(text: str, *, last_reply: str = "") -> Route | None:
    """What a plain sentence asks for, or None when it is ordinary conversation."""
    original = _OPENER.sub("", text.strip().rstrip(".!? ")).strip()
    original = " ".join(original.split())
    n = original.lower().replace(",", "")
    if n in REMEMBER_LAST or (n in YES and OFFER in last_reply):
        return Route("remember")
    for words, action in ((FORGET_LAST, "forget_newest"), (GOOD, "good"), (BAD, "bad"), (KEEP, "keep"), (STOP, "stop"),
                          (MEMORIES, "memories"), (LIBRARY, "library"), (LEARNING, "learning")):
        if n in words:
            return Route(action)
    if n in ("help", "what can i say", "what commands are there"):
        return Route("help")
    if n in FILES:
        return Route("say", 'Press "Read a file" in my window and choose a text file; I keep no copy of it.')
    if n in READING:
        return Route("say", 'I\'d love to. Press "Give internet time" in my window, or run: lantern-prime.sh read 15')
    if (match := _REMEMBER_THAT.fullmatch(original)) is not None:
        return Route("remember_words", match.group(1))
    if (match := _FORGET_ABOUT.fullmatch(original)) is not None:
        return Route("forget_about", match.group(1).lower())
    if (match := _CORRECTION.fullmatch(original)) is not None:
        return Route("correct", match.group(1))
    if (match := _CALL_ME.fullmatch(original)) is not None and match.group(1)[0].isupper():
        return Route("name", match.group(1))
    return None
