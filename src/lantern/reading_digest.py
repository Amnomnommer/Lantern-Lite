"""Read -> interpret -> schema -> delete: Prime digests its reading into memory.

Owner decision (2026-10-22, READING.md): reading is not a scrapbook. After a
reading session, Lantern interprets each page into small relations about
concepts ("tacit knowledge" -is a-> "kind of knowledge that is hard to put into
words"), adds them to Prime's memory itself (the owner chose "Lantern decides"),
and then deletes the page's raw text, keeping only its address, fingerprint,
date and what was learned from it.

Rule-based and inspectable (no language model):

- Sentences are matched against a few plain patterns ("X is a Y", "X refers to Y",
  "X is used for Y", ...). "It", "this" and "they" at the start of a sentence mean
  the page's own subject (taken from its title in the address).
- Every relation is a claim with evidence type "retrieved", a modest confidence,
  and a source naming the session and the page; the same relation is never
  stored twice. Nothing is overwritten: different sources may disagree.
- Reading can only add concept claims. It never touches goals, permissions,
  identity or what the owner said, and page text is never followed as an
  instruction; pages flagged for suspicious instructions are not interpreted.
- A whole session can be undone (`unlearn_session`), which hard-deletes its
  claims and sources with the memory store's audited forgetting.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, Iterator
from urllib.parse import unquote, urlsplit

from lantern.companion import Phrase
from lantern.document_lens import DocumentKind, classify_document
from lantern.explorer import next_searches
from lantern.memory import MemoryStore
from lantern.reading_library import phrases_from

CONCEPT_KIND = "concept"
SOURCE_KIND = "reading"
EVIDENCE = "retrieved"
CONFIDENCE = 0.6  # one page, read once: plausible, not verified
MAX_RELATIONS_PER_PAGE = 60
PHRASEBOOK = "phrasebook.json"
MAX_PHRASES = 5_000

# Ordered: longer verb phrases first so "is part of" wins over "is".
VERBS = (
    ("is a kind of", "is_a"), ("is a type of", "is_a"), ("is a form of", "is_a"), ("are a kind of", "is_a"),
    ("are a type of", "is_a"), ("is part of", "part_of"), ("are part of", "part_of"),
    ("is used to", "used_to"), ("are used to", "used_to"), ("is used for", "used_for"), ("are used for", "used_for"),
    ("refers to", "refers_to"), ("consists of", "consists_of"), ("is made of", "consists_of"),
    ("is defined as", "is"), ("is known as", "is_called"), ("is called", "is_called"),
    ("is not a", "is_not"), ("is not an", "is_not"), ("is not", "is_not"), ("are not", "is_not"),
    ("was a", "was_a"), ("was an", "was_a"), ("were a", "was_a"),
    ("is a", "is_a"), ("is an", "is_a"), ("are a", "is_a"), ("are an", "is_a"), ("is the", "is"),
    ("means", "means"), ("includes", "includes"), ("include", "includes"), ("helps", "helps"), ("help", "helps"),
    ("causes", "causes"), ("cause", "causes"), ("can", "can"), ("has", "has"), ("have", "has"),
    ("describes", "describes"), ("describe", "describes"), ("involves", "involves"), ("involve", "involves"),
    ("studies", "studies"), ("study", "studies"), ("contains", "contains"), ("contain", "contains"),
    ("requires", "requires"), ("require", "requires"), ("produces", "produces"), ("produce", "produces"),
    ("is", "is"), ("are", "is"), ("was", "was"), ("were", "was"),
)
PHRASES = {
    "is_a": "is a", "part_of": "is part of", "used_to": "is used to", "used_for": "is used for",
    "refers_to": "refers to", "consists_of": "consists of", "is_called": "is also called", "is": "is",
    "means": "means", "includes": "includes", "helps": "helps", "causes": "causes", "can": "can", "has": "has",
    "is_not": "is not", "was_a": "was a", "was": "was", "describes": "describes", "involves": "involves",
    "studies": "studies", "contains": "contains", "requires": "requires", "produces": "produces",
}
# A positive and a negative statement about the same object disagree.
OPPOSITES = {"is": ("is_not",), "is_a": ("is_not",), "is_not": ("is", "is_a")}
# Whole numbers and decimals ("1596", "2,100,000", "3.5"), never ordinals or decades ("3rd", "1990s").
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)*\b")
_VERB = "|".join(re.escape(v) for v, _ in VERBS)
# Subjects may hold any letters ("rené descartes"): [^\W\d_] is a letter, [^\W_] a letter or digit.
_RELATION = re.compile(rf"^(?:the |a |an )?(?P<subject>[^\W\d_](?:[^\W_]|['\- ]){{0,60}}?) (?P<verb>{_VERB}) (?P<object>.+)$")
_SENTENCE_END = re.compile(r"(?<=[.!?])[ \t\n\x0b\f\r]+(?=[A-Z\"“])")
# A ". " after a lone capital ("the U.S. flag") is an abbreviation, not the end of the object; ", and there are ..."
# starts a second clause that is not part of the object.
_OBJECT_CUT = re.compile(r"(?:(?<![A-Z])\. |; | \(|, which |, but |, while |, whereas |, although |\[)")
# Since 2026-10-29 new reading also cuts here; tidying older facts does not (it would forget them wholesale).
_CLAUSE_CUT = re.compile(r", and (?:there|it|they|this|these|those|he|she|we|i) ")
RELATIVE = frozenset({"that", "which", "who", "whom", "whose"})  # "flags that are made ..." is not a subject
# Past participles that are not "-ed": after "has" they make a tense ("has given"), after "is" + an adverb a passive.
PARTICIPLES = frozenset({
    "been", "given", "taken", "made", "shown", "seen", "known", "done", "gone", "won", "lost", "held", "built",
    "found", "born", "flown", "worn", "written", "drawn", "grown", "chosen", "spoken", "broken", "driven", "eaten",
    "fallen", "forgotten", "hidden", "ridden", "risen", "stolen", "sworn", "thrown", "sold", "told", "brought",
    "bought", "caught", "taught", "thought", "fought", "sent", "spent", "kept", "led", "met", "paid", "begun",
    "become", "beaten", "bitten", "frozen", "shaken", "torn", "woven", "understood", "struck", "hung", "sung"})
DEFINING = frozenset({"as", "with", "by", "to", "in", "of", "for", "on", "from"})  # "concerned with", "known as"
PASSIVE_ADVERBS = frozenset({"either"})  # with any "-ly" adverb: "is customarily worn", "is either listed"
# "This"/"these" usually point back at the last sentence, not the page, so they are not resolved.
PRONOUNS = frozenset({"it", "they", "he", "she"})
UNRESOLVED = frozenset({"this", "these", "that", "those"})
# Words that make a subject site furniture, not an idea; identical to PhoneDigest's list.
JUNK_SUBJECT_WORDS = frozenset({
    "page", "pages", "please", "example", "examples", "note", "notes", "see", "wikipedia", "wikiquote", "wikimedia",
    "wikidata", "wikisource", "wiktionary", "wikibooks", "wikinews", "wikiversity", "wikivoyage", "mediawiki",
    "parsoid", "article", "articles", "citation", "citations", "template", "category", "categories", "isbn", "doi",
    "retrieved", "archived", "edit", "edits", "stub",
})
# A relation's object never ends on these: it was cut off mid-phrase.
DANGLING = frozenset({
    "a", "an", "the", "of", "in", "on", "to", "for", "with", "and", "or", "by", "as", "at", "from", "than", "that",
    "which", "is", "are", "was", "were", "be", "its", "their", "his", "her", "this", "these", "such", "been",
    "i.e", "e.g", "viz", "cf",
})
# Page footers (licences, copying notes) are not facts about the page's subject.
FOOTER = frozenset({"creative commons", "available under", "gfdl", "attribution-sharealike", "copied from this source",
          "all rights reserved", "terms of use", "privacy policy"})
# Adverbs that end up after a subject ("the schedule also includes"): not part of the idea's name.
TRAILING = frozenset({"also", "then", "now", "still", "often", "usually", "sometimes", "later", "first", "currently",
                      "might", "may", "can", "could", "would", "should", "must", "will"})  # "mystery might | has …"
# An object starting with these is a verb tense or an aside, not a fact: "has also been appropriated".
LEADING = frozenset({"been", "also", "being", "become", "had", "already", "to", "this", "these", "those", "that"})
# "It is traditionally held that ...": the page talks about a view, not about the idea.
_IMPERSONAL = re.compile(r"^(?:\w+ )?(?:held|accepted|believed|thought|said|argued|known|claimed|assumed|agreed"
                         r"|suggested|recognized|recognised|understood|considered) that\b", re.IGNORECASE)
# A writer's own voice ("what I call ...") is a quotation, not a fact about the idea.
FIRST_PERSON = frozenset({"I", "my", "me", "we", "our", "us", "My", "We", "Our"})  # case kept: "the US" is not "us"
NOT_SUBJECTS = frozenset({
    "there", "what", "that", "which", "who", "one", "some", "many", "most", "all", "each", "such", "here", "another",
    "how", "whether", "why", "whose", "whom",  # a question or a clause, never an idea ("whether a container is ...")
    "although", "though", "while", "because", "since", "after", "before", "during", "unlike", "like",
    "other", "others", "both", "none", "also", "however", "thus", "so", "but", "and", "or", "if", "when", "where",
    "i", "you", "we", "us", "our", "your", "his", "her", "its", "their", "him", "them",
    "something", "anything", "everything", "nothing", "someone", "anyone", "everyone", "somebody", "anybody",
    "everybody", "nobody", "the", "a", "an", "according",
    "once", "unless", "until", "whereas", "whenever", "wherever",  # "Once applied epistemology is described as ..."
})
# A subject is a name: letters, digits and a little punctuation ("working!!" is not an idea).
_SUBJECT = re.compile(r"[^\W_](?:[^\W_]|[ '\-.&])*")
# Phrases that make a subject a writer's aside, not an idea ("further difficulty in this regard").
# Words that refer back to whatever a page is about ("The country has ...", "The term is ..."): an idea of
# their own only on their own page.
GENERIC_SUBJECTS = frozenset({
    "term", "word", "phrase", "expression", "name", "country", "city", "town", "state", "nation", "film", "book",
    "album", "song", "band", "company", "system", "region", "island", "river", "government", "president", "area",
    "game", "series", "show", "group", "party", "team", "building",
})
JUNK_SUBJECT_PHRASES = ("in this regard", "in this respect", "in this sense", "in that regard")
# Deeper reading (owner, 2026-10-29: "read more and better"; scored in tests/test_reading_gold.py):
FICTION = frozenset({DocumentKind.NARRATIVE, DocumentKind.PLAY, DocumentKind.POETRY})  # a story's world is not ours
PREPOSITIONS = frozenset({"in", "on", "at", "according", "for", "with", "by", "from", "under", "over", "to"})
REPORTING = frozenset({"claim", "claims", "claimed", "say", "says", "said", "argue", "argues", "argued", "believe",
                       "believes", "believed", "think", "thinks", "thought", "suggest", "suggests", "suggested",
                       "insist", "insists", "insisted", "report", "reports", "reported"})  # "critics claim the bridge"
FIRST_PERSON_SUBJECT = frozenset({"i", "my", "me", "we", "our", "us", "mine", "ours"})
POSSESSIVES = frozenset({"its", "his", "her", "their"})
# "It is important to ...": with "it" standing for the page, the sentence is about a view, not the idea.
_IMPERSONAL_IT = re.compile(r"^(?:not |also |often |generally |usually |therefore )?(?:important|possible|impossible|necessary|"
                            r"difficult|easy|hard|common|useful|clear|unclear|likely|unlikely|true|false|worth|essential|"
                            r"crucial|natural|reasonable|plausible|tempting|notable|interesting|vital) (?:to|that)\b")
# Invisible characters (left where a page's maths or markup was) never count as a word.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")


@dataclass(frozen=True, slots=True)
class Relation:
    subject: str
    predicate: str
    object: str


def page_subject(url: str) -> str:
    """The page's subject from its address: /wiki/Tacit_knowledge -> "tacit knowledge"; a file has none."""
    if url.startswith("file:"):
        return ""  # as on the phone: "it" in a book is not the file's name
    path = unquote(urlsplit(url).path).rstrip("/")
    title = path.rsplit("/", 1)[-1].replace("_", " ")
    title = re.sub(r"\s*\([^)]*\)$", "", title)  # "Justification (epistemology)" -> "Justification"
    return " ".join(title.lower().split())


def singular(name: str) -> str:
    """A light singular for the last word, so "knowledge graphs" and "knowledge graph" are one concept."""
    *head, last = name.split()
    if last.endswith(("eses", "yses")) and len(last) > 5:  # hypotheses -> hypothesis, analyses -> analysis
        last = last[:-2] + "is"
    elif last in ("series", "species"):  # the same word in the singular
        pass
    elif last.endswith("ies") and len(last) > 4:
        last = last[:-3] + "y"
    elif last.endswith("s") and len(last) > 3 and not last.endswith(("ss", "us", "is", "ics", "ous", "sis")):
        last = last[:-1]
    return " ".join([*head, last])


def _with_article(predicate: str, obj: str) -> str:
    """How a relation reads aloud: "is a" / "is an" depending on the next word."""
    phrase = PHRASES.get(predicate, predicate)
    if predicate in ("is_a", "was_a"):
        verb = "is" if predicate == "is_a" else "was"
        phrase = f"{verb} an" if obj[:1].lower() in "aeiou" else f"{verb} a"
    return f"{phrase} {obj}"


def participle(word: str) -> bool:
    """A past participle by its form: "-ed" (longer than "red" or "seed") or a known irregular one."""
    word = word.lower().strip("\"'“”‘’,")
    return (len(word) > 4 and word.endswith("ed")) or word in PARTICIPLES


def _clean_object(text: str, *, new_reading: bool = True) -> str | None:
    text = _OBJECT_CUT.split(_INVISIBLE.sub("", text), maxsplit=1)[0]
    if new_reading:
        text = _CLAUSE_CUT.split(text, maxsplit=1)[0]
    text = text.strip().rstrip(".!?,:;").strip().replace(" ,", ",")
    words = text.split()
    if not 1 <= len(words) <= 20 or "http" in text or "|" in text or not re.search(r"[a-z]", text):
        return None
    if re.search("[\"“”]", text) and not any(w.islower() for w in re.findall(r"[A-Za-z]+", text)):
        return None  # table debris such as "White" No: quoted, with no ordinary lowercase word
    if re.search(r"\b[A-Z]\.[A-Z]$", text):  # cut inside an abbreviation: "the largest U.S"
        return None
    if words[-1].lower() in DANGLING:  # cut off mid-phrase: "the assumption in a"
        return None
    if words[0].lower() in LEADING or ":" in text or text.count('"') % 2:  # "has also been ...", "is_not available: objects ..."
        return None
    if _IMPERSONAL.match(text) or any(w.strip("'\"‘’“”,;") in FIRST_PERSON for w in words):
        return None
    if any(marker in text.lower() for marker in FOOTER):  # "Text is available under the Creative Commons ..."
        return None
    if any(c in text for c in "[]«»{}") or any(c.isalpha() and ord(c) >= 0x250 for c in text):
        return None  # citation debris ("… old ]", "« A.Savin »") and other scripts ("chief Արխիվացված")
    return text


def _old_plural(word: str) -> bool:
    """What older singulars made of "hypotheses", "analyses", "series": "hypothese", "analyse", "sery"."""
    return (len(word) > 5 and word.endswith(("hese", "lyse"))) or word in ("sery", "specy")


PHRASE_PREPOSITIONS = PREPOSITIONS | {"among", "between", "into", "onto", "upon", "through", "across", "as", "near",
                                      "within", "without", "against", "along", "around", "behind", "beyond"}


def _participle_phrase(words: list[str]) -> bool:
    """"Included among this group", "based on": a participle and its preposition open a phrase, not a name
    ("United Kingdom" and "fixed costs" stay names)."""
    return len(words) > 1 and participle(words[0]) and words[1] in PHRASE_PREPOSITIONS


def keeps(subject: str, obj: str, page: str = "") -> bool:
    """Whether a learned relation passes today's rules (used to tidy what older rules let through)."""
    if subject in GENERIC_SUBJECTS and subject != page_subject(page):
        return False
    words = subject.split()
    return (1 <= len(words) <= 5 and words[0] not in NOT_SUBJECTS and words[0] not in UNRESOLVED
            and not _participle_phrase(words)
            and _SUBJECT.fullmatch(subject) is not None and not any(p in subject for p in JUNK_SUBJECT_PHRASES)
            and not any(w in JUNK_SUBJECT_WORDS for w in words) and not _old_plural(words[-1])
            and words[-1] not in TRAILING
            and _clean_object(obj, new_reading=False) == obj)


TITLES = frozenset({"mr", "mrs", "ms", "miss", "dr", "sir", "lady", "lord", "king", "queen", "saint", "st", "prince",
                    "princess", "captain", "professor"})
PERSON_NOUNS = frozenset({"man", "woman", "person", "people", "boy", "girl", "child", "writer", "author", "poet",
                          "philosopher", "scientist", "landowner", "king", "queen", "daughter", "son", "mother", "father",
                          "wife", "husband", "brother", "sister", "politician", "artist", "painter", "composer", "actor",
                          "actress", "singer", "leader", "soldier", "teacher", "student", "farmer", "merchant"})


def _person(subject: str, obj: str) -> bool:
    """Whether a sentence is about a person ("Mr Darcy is a wealthy landowner"): "it" cannot stand for them."""
    return subject.split()[0] in TITLES or any(w.strip(",.") in PERSON_NOUNS for w in obj.lower().split()[:6])


# Citation marks ("birds.[1]", "[citation needed]") hide the end of a sentence; they carry no fact.
_CITATION = re.compile(r"\[(?:\d{1,3}|[a-z]|citation needed|note \d{1,3}|nb \d{1,3}|clarification needed)\]", re.IGNORECASE)
# "Paris, the capital of France, is ..." / "The Seine, which flows through the city, is ...": an aside between
# the subject and its verb. An article aside is a fact of its own; a which/who aside is left out.
_ASIDE = re.compile(r"^(?P<subject>[^,]{1,60}), (?P<aside>(?:the|a|an) [^,]{1,80}|(?:which|who) [^,]{1,80}), (?P<rest>.+)$")


def _sentences(text: str) -> Iterator[str]:
    """Sentences, with citation marks and a leading quote removed and an aside split off as its own sentence."""
    for line in _CITATION.sub("", text).split("\n"):
        for sentence in _SENTENCE_END.split(line):
            sentence = sentence.strip().lstrip("\"“'‘").replace("\" ", " ").replace("” ", " ").strip()
            aside = _ASIDE.match(sentence)
            if aside:
                yield f"{aside['subject']} {aside['rest']}"
                if aside["aside"].split()[0].lower() in ("the", "a", "an"):
                    yield f"{aside['subject']} is {aside['aside']}."
                continue
            yield sentence


def _head(name: str) -> str:
    """The word a page's own subject goes by: "tower" for "eiffel tower" and "tower of london", "curie" for
    "marie curie"."""
    main = name.split(" of ")[0].split()
    return main[-1] if len(name.split()) > 1 and main else ""


def relations_in(url: str, text: str) -> list[Relation]:
    """Relations stated in a page's sentences, in order, without duplicates.

    A story, a play or a poem teaches no facts about the world (its dialogue still teaches phrases). "It" stands for
    the page's subject only while the page is talking about it: after a sentence about something else, Lantern does
    not guess. "The city" stands for the page's subject once the page has said its subject is a city."""
    lens = classify_document(text)
    if lens.kind in FICTION and len(set(lens.evidence)) >= 2:
        return []  # two distinct signs of fiction: one stray cue ("verse" in "reversed") never costs real facts
    subject_of_page = page_subject(url)
    found: list[Relation] = []
    topic = subject_of_page  # what the page is talking about now
    # A line break ends a sentence too (headings and paragraphs, e.g. text from a PDF).
    head_word = _head(subject_of_page)
    for sentence in _sentences(text):
        if not 5 <= len(sentence.split()) <= 45 or sentence[-1:] not in ".!":
            continue  # a question is asked, not claimed ("How can a basket contain ...?")
        lowered = sentence[0].lower() + sentence[1:]
        # A page's own (often capitalised) title may open the sentence: "René Descartes was ...".
        for prefix in ("", "the ", "a ", "an "):
            head = prefix + subject_of_page
            if subject_of_page and lowered.lower().startswith(head + " "):
                lowered = head + lowered[len(head):]
                break
        match = _RELATION.match(lowered)
        if match is None:
            continue
        subject = " ".join(match["subject"].lower().split())
        alias = None
        if " or " in subject:  # "tacit knowledge or implicit knowledge is ..."
            subject, alias = (part.strip() for part in subject.split(" or ", 1))
        impersonal = subject == "it"
        if head_word and subject == head_word:
            subject = subject_of_page  # "the tower" on the Eiffel Tower's page, "Curie" on Marie Curie's
        named = singular(subject) if subject not in PRONOUNS else subject
        if named in GENERIC_SUBJECTS and subject_of_page and any(
                r.subject == subject_of_page and r.predicate in ("is", "is_a") and named in r.object.lower().split()
                for r in found):
            subject = named = subject_of_page  # "the city" after "Tehran is the largest city"
        if subject in PRONOUNS:
            if subject == "it" and topic != subject_of_page:
                continue  # the page moved on ("Its largest moon is Titan. It is ..."): abstain rather than guess
        elif subject.split()[0] in POSSESSIVES:
            topic = subject  # "its largest moon": something new, even though it is not kept as a subject
        if subject in PRONOUNS:
            if not subject_of_page:
                continue
            subject = subject_of_page
        while len(subject.split()) > 1 and subject.split()[-1] in TRAILING:
            subject = subject.rsplit(" ", 1)[0]
        words = subject.split()
        if (not 1 <= len(words) <= 5 or words[0] in NOT_SUBJECTS or words[0] in UNRESOLVED
                or words[0] in PREPOSITIONS or any(w in REPORTING for w in words[1:]) or _participle_phrase(words)
                or any(w in PRONOUNS or w in RELATIVE or w in FIRST_PERSON_SUBJECT for w in words)):
            continue
        obj = _clean_object(match["object"])
        if obj is None or (impersonal and _IMPERSONAL_IT.match(obj)):
            continue
        first = obj.split()
        if match["verb"] in ("has", "have") and participle(first[0]):
            continue  # "has given", "has rejected": a tense, not something the subject has
        defines = len(first) > 2 and first[2] in DEFINING and not (first[2] == "as" and first[3:4] == ["saying"])
        if (match["verb"] in ("is", "are", "was", "were") and len(first) > 1 and participle(first[1])
                and (first[0].endswith("ly") or first[0] in PASSIVE_ADVERBS) and not defines):
            continue  # "is customarily worn reversed", "is famously quoted as saying": a passive report, not a definition
        measured = _MEASURE_SUBJECT.match(subject) or _MEASURE_POSSESSIVE.match(subject)
        figure = match["verb"] in ("is", "was") and _NUMBER.match(" ".join(w for w in obj.casefold().split() if w not in _HEDGES))
        bare = _MEASURE_BARE.match(subject)
        if bare and figure:
            if not subject_of_page:
                continue  # whose figure it is cannot be told (a file, or a page without a subject)
            measured = {"measure": bare["measure"], "of": subject_of_page}
        if measured and figure:
            # "the current population of Burundi is 14,151,520" -> burundi has a population of 14,151,520
            article = "an" if measured["measure"][0] in "aeiou" else "a"
            subject, obj = measured["of"], f"{article} {measured['measure']} of {obj}"
            match = {"verb": "has", "object": obj}
        if _SUBJECT.fullmatch(subject) is None or any(p in subject for p in JUNK_SUBJECT_PHRASES):
            continue
        if singular(subject) in GENERIC_SUBJECTS and singular(subject) != subject_of_page:
            continue  # "the country", "the term": the page's idea, not an idea of its own
        if match["verb"] == "is the":
            obj = f"the {obj}"
        if subject != subject_of_page:  # a page's own title is a name, never a plural ("Socrates")
            subject = singular(subject)
        if any(w in JUNK_SUBJECT_WORDS for w in subject.split()):
            continue  # site furniture ("This page was last edited", "Please help improve this article")
        if subject == subject_of_page:
            topic = subject_of_page
        elif not _person(subject, obj):  # "it" never stands for Mr Darcy
            topic = subject  # the page is now talking about this
        relations = [Relation(subject, dict(VERBS)[match["verb"]], obj)]
        if alias and 1 <= len(alias.split()) <= 5 and alias.split()[0] not in NOT_SUBJECTS:
            relations.append(Relation(subject, "is_called", alias))
        for relation in relations:
            if relation not in found:
                found.append(relation)
        if len(found) >= MAX_RELATIONS_PER_PAGE:
            break
    return found


# Hedges never change what a number is about ("about 6,650 km" and "6,695 km" are about the same length).
_HEDGES = frozenset({"about", "approximately", "around", "roughly", "nearly", "almost", "some", "over", "under",
                     "circa", "c.", "ca.", "estimated", "an", "a", "the", "just", "more", "less", "than"})
# "12.3 million" is a rounded 12,300,000; a count of people needs no unit to be compared.
_SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
_COUNTED = frozenset({"people", "inhabitants", "residents", "persons", "citizens"})
# Words after a number that are not its unit ("14,151,520 based on projections").
_NOT_UNITS = frozenset({"based", "as", "in", "on", "by", "according", "from", "at", "with", "and", "or", "which",
                        "that", "for", "to", "is", "was", "making", "while", "but", "according"})
# "The current population of Burundi is 14,151,520" says what "Burundi has a population of 12.3 million" says.
# Whose figure it is, also for "the plant's capacity is ..." (owner's screenshot, 2026-10-30: one plant's capacity
# had been learned as what capacity is).
_MEASURES = (r"(?:(?:current|total|estimated|latest|recent|approximate|official|projected|resident|annual|maximum|"
             r"production) )*(?P<measure>population density|median age|life expectancy|population|total area|land area|"
             r"area|length|height|elevation|depth|gdp|capacity|output|production|volume|weight|mass|width|speed|budget|"
             r"revenue)")
_MEASURE_SUBJECT = re.compile(rf"^{_MEASURES} of (?:the )?(?P<of>.+)$")
_MEASURE_POSSESSIVE = re.compile(rf"^(?P<of>[a-z0-9][a-z0-9 -]*?)'s? {_MEASURES}$")
# "The median age is 17 years" on Burundi's page is Burundi's figure (Gate 6, owner's 1.1 item: "whose figure").
_MEASURE_BARE = re.compile(rf"^{_MEASURES}$")


# What a measure is of, however it is worded ("8,849 metres tall" / "8,848 metres high").
_DIMENSIONS = {"tall": "height", "high": "height", "height": "height", "elevation": "height",
               "long": "length", "length": "length", "wide": "width", "width": "width", "across": "width",
               "deep": "depth", "depth": "depth", "old": "age", "age": "age"}


def _number_key(text: str) -> tuple[str, str, str, list[str], str, float] | None:
    """(words before the first number, the word after it, its value, later numbers, what it measures, how
    precise the value is), hedges ignored; "13.2 million" is read as 13,200,000, precise to 50,000."""
    words = [w for w in text.casefold().replace("(", " ").replace(")", " ").split() if w not in _HEDGES]
    for index, word in enumerate(words):
        if _NUMBER.fullmatch(word.rstrip(",;")):
            digits = word.rstrip(",;").replace(",", "")
            scale, skip = 1.0, 1
            after = words[index + 1].rstrip(",;.") if index + 1 < len(words) else ""
            if after in _SCALES:
                scale, skip = _SCALES[after], 2
                after = words[index + 2].rstrip(",;.") if index + 2 < len(words) else ""
            if after in _COUNTED or after in _NOT_UNITS:
                after = ""
            later = [n.replace(",", "") for n in _NUMBER.findall(" ".join(words[index + skip:]))]
            rest = [w.rstrip(",;.") for w in words[index + skip + 1:] if w not in ("in", "of", "above", "below")]
            measure = _DIMENSIONS.get(rest[0], rest[0]) if rest else ""
            try:
                value = float(digits) * scale
            except ValueError:
                value = float("nan")
            decimals = len(digits.split(".")[1]) if "." in digits else 0
            precision = (10 ** -decimals) * scale / 2 if scale > 1 else 0.0
            return " ".join(words[:index]), after, repr(value), later, measure, precision
    return None


def numbers_differ(first: str, second: str) -> bool:
    """The same measure given as a different number ("was born in 1596" / "was born in 1595";
    "about 6,650 km long" / "6,695 km long").

    The words before the number and the unit after it must match (hedges aside); wording after that may differ.
    When both go on to give other, different numbers it is a change over time ("2 million in 1990" /
    "3 million in 2020"), not a conflict.
    """
    a, b = _number_key(first), _number_key(second)
    if a is None or b is None or a[:2] != b[:2] or a[2] == b[2]:
        return False
    if abs(float(a[2]) - float(b[2])) <= max(a[5], b[5]):
        return False  # a rounded figure that agrees: "12.3 million" / "12,346,893"
    if a[4] and b[4] and a[4] != b[4]:
        return False  # different measures: "2,377 km in diameter" / "5 billion km from the Sun"
    return not (a[3] and b[3] and a[3][0] != b[3][0])


def _session_of(path: Path) -> str:
    return path.stem


def _concept(memory: MemoryStore, name: str) -> str:
    row = memory._connection.execute(
        "SELECT id FROM entities WHERE kind = ? AND canonical_name = ?", (CONCEPT_KIND, name),
    ).fetchone()
    return str(row["id"]) if row is not None else memory.add_entity(CONCEPT_KIND, name)


def _same(memory: MemoryStore, entity_id: str, predicate: str, obj: str) -> list:
    return [c for c in memory.current_claims(subject_entity_id=entity_id, predicate=predicate)
            if c.evidence_type == EVIDENCE and c.object_text.casefold() == obj.casefold()]


def _reference(memory: MemoryStore, source_id: str | None) -> str:
    row = memory._connection.execute("SELECT reference FROM sources WHERE id = ?", (source_id,)).fetchone()
    return "" if row is None else str(row["reference"])


def _page_of(reference: str) -> str:
    return reference.split(":", 2)[-1]


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def learn(memory: MemoryStore, session: str, page: str, relation: Relation, sources: dict[str, str]) -> bool:
    """Add one relation one page stated, unless that page already said it; returns whether it was added.

    `sources` maps a page to its source in this session (created on first use).
    """
    entity = _concept(memory, relation.subject)
    same = _same(memory, entity, relation.predicate, relation.object)
    if any(_page_of(_reference(memory, c.source_id)) == page for c in same):
        return False  # this page already said it
    if page not in sources:
        sources[page] = memory.add_source(SOURCE_KIND, f"reading:{session}:{page}")
    claim_id = memory.add_claim(entity, relation.predicate, relation.object, evidence_type=EVIDENCE,
                                confidence=CONFIDENCE, source_id=sources[page], consequential=False)
    # Another page said the same: corroboration, one claim per source so each can be undone.
    if same:
        memory.link_claims(claim_id, same[0].id, relation="supports",
                           reason="Another page states the same relation.")
    opposites = OPPOSITES.get(relation.predicate, ())
    for other in [c for o in opposites for c in _same(memory, entity, o, relation.object)]:
        memory.link_claims(claim_id, other.id, relation="contradicts",
                           reason="Pages disagree about this relation; neither is chosen silently.")
    if _NUMBER.search(relation.object):
        for other in memory.current_claims(subject_entity_id=entity, predicate=relation.predicate):
            other_page = _page_of(_reference(memory, other.source_id))
            if (other.evidence_type == EVIDENCE and numbers_differ(relation.object, other.object_text)
                    and other_page != page and not _different_things(relation.subject, page, other_page)):
                memory.link_claims(claim_id, other.id, relation="contradicts",
                                   reason="Pages give a different number for this; neither is chosen silently.")
    return True


def digest_journal(memory: MemoryStore, path: Path, *, now: str | None = None,
                   observe: Callable[[str], object] | None = None) -> dict[str, object]:
    """Interpret one journal into memory, then delete its raw page text. Already digested journals are skipped."""
    return _finish(digest_journal_steps(memory, path, now=now, observe=observe))


OBSERVE_PIECE = 8_000  # characters the minds take in between two looks at Prime's window


def _pieces(text: str) -> list[str]:
    """A page in pieces of about OBSERVE_PIECE characters, cut only at line or sentence ends."""
    pieces, current, size = [], [], 0
    units = [u for line in text.split("\n") for u in (_SENTENCE_END.split(line) if len(line) > OBSERVE_PIECE else [line])]
    for line in units:
        current.append(line)
        size += len(line) + 1
        if size >= OBSERVE_PIECE:
            pieces.append("\n".join(current))
            current, size = [], 0
    if current:
        pieces.append("\n".join(current))
    return pieces


def _finish(steps: Generator[int, None, Any]) -> Any:
    try:
        while True:
            next(steps)
    except StopIteration as done:
        return done.value


def digest_journal_steps(memory: MemoryStore, path: Path, *, now: str | None = None,
                         observe: Callable[[str], object] | None = None) -> Generator[int, None, dict[str, object]]:
    """digest_journal one page at a time: yields after each page, so a window stays responsive."""
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal.get("digested"):
        return {"session": _session_of(path), "relations_added": 0, "already_digested": True}
    session = _session_of(path)
    added = 0
    sources: dict[str, str] = {}
    phrases: list[Phrase] = []
    for page in journal.get("pages", []):
        if page.get("suspicious_instructions") or page.get("trusted_as_instructions"):
            continue  # untrusted pages teach nothing
        if observe is not None:  # the minds take in the text before it is deleted, a piece at a time
            for piece in _pieces(page.get("text", "")):
                observe(piece, str(page.get("community") or "reading"))
                yield 0
        with memory.batch():  # one page, one write to disk (not one per fact)
            for relation in relations_in(page["url"], page.get("text", "")):
                added += learn(memory, session, page["url"], relation, sources)
        phrases += phrases_from(page["url"], page["community"], page.get("exchanges", []))
        yield 1
    _keep_phrases(path.parent.parent / PHRASEBOOK, phrases)
    curiosity = list(next_searches(journal.get("pages", []), set()))
    journal["pages"] = [
        {k: page[k] for k in ("url", "stage", "community", "retrieved_at", "sha256", "via") if k in page}
        for page in journal.get("pages", [])
    ]
    journal["next_searches"] = curiosity
    journal["digested"] = {"at": now or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                           "relations_added": added, "raw_text_deleted": True}
    _write_json(path, journal)
    return {"session": session, "relations_added": added, "already_digested": False,
            "searches": [s for s in journal.get("searches", []) if isinstance(s, str)]}


def _keep_phrases(path: Path, new: list[Phrase]) -> None:
    """Speech pairs are already interpreted (said -> reply); they outlive the raw page in a bounded phrasebook."""
    if not new:
        return
    existing = load_phrasebook(path)
    seen = {(p.said.lower(), p.reply.lower()) for p in existing}
    for phrase in new:
        key = (phrase.said.lower(), phrase.reply.lower())
        if key not in seen:
            seen.add(key)
            existing.append(phrase)
    _write_json(path, [asdict(p) for p in existing[-MAX_PHRASES:]])


def load_phrasebook(path: Path) -> list[Phrase]:
    try:
        return [Phrase(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


FILE_CHUNK = 20_000
FILE_EXCHANGES = 200  # a book's chapter holds far more dialogue than a web page
MAX_FILE_BYTES = 10_000_000


def _chunks(text: str) -> list[str]:
    """Paragraph-aligned pieces of at most FILE_CHUNK characters (as the phone's PhoneDigest.fromFile)."""
    pieces: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        if current and len(current) + len(paragraph) > FILE_CHUNK:
            pieces.append(current)
            current = ""
        current += paragraph[:FILE_CHUNK] + "\n\n"
    if current.strip():
        pieces.append(current)
    return pieces


def file_text(name: str, data: bytes) -> str:
    """The readable text of a file the owner chose: a PDF's text, or the file decoded as UTF-8."""
    if data.startswith(b"%PDF") or name.lower().endswith(".pdf"):
        from lantern.pdf_text import extract_text
        text = extract_text(data)
        if not text.strip():
            raise ValueError("this PDF has no readable text (it may be a scan of pictures)")
        return text
    return data.decode("utf-8", "replace")


def digest_file(memory: MemoryStore, journals: Path, name: str, text: str, *, now: str | None = None,
                observe: Callable[[str], object] | None = None) -> dict[str, object]:
    """Read a text file the owner chose (e.g. an author's own copy of a book) the same way as a page.

    It becomes one reading session (so !readings lists it and !unlearn undoes it); the
    text is interpreted and then deleted from the journal, exactly as web pages are.
    """
    from lantern.explorer import dialogue_exchanges
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"the file is larger than {MAX_FILE_BYTES // 1_000_000} MB")
    at = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:60] or "file"
    journals.mkdir(parents=True, exist_ok=True)
    path = journals / f"{at.replace(':', '')}-file-{safe}.json"
    pages = [{"url": f"file:{name}", "stage": 0, "community": "your file", "retrieved_at": at, "sha256": "",
              "via": "file", "text": chunk, "exchanges": dialogue_exchanges(chunk, FILE_EXCHANGES), "suspicious_instructions": []}
             for chunk in _chunks(text)]
    _write_json(path, {"format": "lantern_reading_journal_v1", "stage": 0, "started_at": at, "pages": pages,
                       "skipped": [], "searches": [], "stopped_because": "file", "review_required": False})
    return digest_journal(memory, path, now=at, observe=observe)


def digest_all(memory: MemoryStore, journals: Path, *, observe: Callable[[str], object] | None = None) -> list[dict[str, object]]:
    """Digest every journal not yet digested, oldest first; then wonder about what sources disagree on."""
    return _finish(digest_steps(memory, journals, observe=observe))


def digest_steps(memory: MemoryStore, journals: Path, *,
                 observe: Callable[[str], object] | None = None) -> Generator[int, None, list[dict[str, object]]]:
    """digest_all one page at a time (see Digestion)."""
    if not journals.is_dir():
        return []
    results = []
    for path in sorted(journals.glob("*.json")):
        try:
            results.append((yield from digest_journal_steps(memory, path, observe=observe)))
        except (OSError, ValueError, KeyError, TypeError) as error:
            results.append({"session": _session_of(path), "error": str(error)})
    if any(r.get("relations_added") or r.get("searches") for r in results):
        # Research threads (owner, 2026-10-25): close the ones this reading searched, open new ones
        # (disputes, thin categories, ideas to widen or to check on Wikipedia) for the next reading.
        from lantern import research_mind  # research_mind imports this module
        research_mind.after_reading(memory, journals.parent,
                                    {str(s).casefold() for r in results for s in r.get("searches") or []})
    return results


class Digestion:
    """Digestion in short slices on a window's own thread (its memory lives there): `advance()` works for
    a moment and returns True once everything is digested; `results` then holds digest_all's results."""

    def __init__(self, memory: MemoryStore, journals: Path, *, observe: Callable[[str], object] | None = None) -> None:
        self._steps = digest_steps(memory, journals, observe=observe)
        self.pages = 0
        self.results: list[dict[str, object]] | None = None

    def advance(self, seconds: float = 0.2) -> bool:
        if self.results is not None:
            return True
        deadline = time.monotonic() + seconds
        try:
            while True:  # at least one page per slice
                self.pages += next(self._steps)
                if time.monotonic() >= deadline:
                    break
        except StopIteration as done:
            self.results = done.value
        return self.results is not None

    @property
    def learned(self) -> int:
        return sum(int(r.get("relations_added", 0)) for r in self.results or [])


def wonder_about_disputes(memory: MemoryStore, wonders: Path, limit: int = 3) -> list[str]:
    """Disagreements lead to reading (owner, 2026-10-24): the newest disputed ideas join what Lantern
    wants to read about, so its next reading looks for more sources. Returns the ideas added."""
    disputed = list(dict.fromkeys(str(d["concept"]) for d in disputes(memory, limit=20)))[:limit]
    if not disputed:
        return []
    try:
        current = [w for w in json.loads(wonders.read_text(encoding="utf-8")) if isinstance(w, str)]
    except (OSError, ValueError, TypeError):
        current = []
    added = [d for d in disputed if d not in current]
    if added:
        wonders.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_json(wonders, (current + added)[-20:])
    return added


def session_sources(memory: MemoryStore, session: str) -> list[str]:
    return [str(row["id"]) for row in memory._connection.execute(
        "SELECT id FROM sources WHERE kind = ? AND reference LIKE ?", (SOURCE_KIND, f"reading:{session}:%"),
    )]


def unlearn_session(memory: MemoryStore, session: str) -> int:
    """Hard-delete every claim one reading session added (audited forgetting); returns how many."""
    sources = session_sources(memory, session)
    forgotten = 0
    for source_id in sources:
        for row in memory._connection.execute("SELECT id FROM claims WHERE source_id = ?", (source_id,)).fetchall():
            memory.forget("claim", str(row["id"]), requested_by="owner", reason_code="owner_request")
            forgotten += 1
        if memory._connection.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
            memory.forget("source", source_id, requested_by="owner", reason_code="owner_request")
    return forgotten


def sessions(journals: Path) -> list[dict[str, object]]:
    """Digested reading sessions, newest first, for the owner to inspect or undo."""
    found = []
    for path in sorted(journals.glob("*.json"), reverse=True) if journals.is_dir() else []:
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        digested = journal.get("digested") or {}
        found.append({"session": path.stem, "pages": len(journal.get("pages", [])),
                      "relations_added": digested.get("relations_added"), "digested": bool(digested)})
    return found


ORDER = ("is_a", "is", "was_a", "was", "refers_to", "means", "is_called", "consists_of", "part_of", "includes",
         "describes", "involves", "studies", "contains", "used_for", "used_to", "helps", "causes", "requires",
         "produces", "can", "has", "is_not")


@dataclass(frozen=True, slots=True)
class Known:
    """One learned relation and the page it came from, in the order it was learned."""

    concept: str
    predicate: str
    object: str
    page: str
    via: str = ""  # the hive node whose reading this came through; "" for this Lantern's own reading


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _source_name(page: str) -> str:
    """How a source is named in an answer: a site's host, or "your file NAME" for a file the owner chose."""
    return f"your file {page[len('file:'):]}" if page.startswith("file:") else _host(page)


def _reader(via: str) -> str:
    return "Prime" if via == "prime" else "my phone"


def _lead(chosen: list[Known]) -> str:
    """Who read it: never claim this Lantern read what another node read (hive amendment, 2026-10-23)."""
    vias = list(dict.fromkeys(k.via for k in chosen))
    others = list(dict.fromkeys(_reader(v) for v in vias if v))
    if not others:
        return "From my reading"
    return ("From my reading and what " if "" in vias else "From what ") + " and ".join(others) + " read"


# A measured figure is one thing's value, not what an idea is (owner's screenshot, 2026-10-30: "capacity ... is between
# 130 and 150 kilotons per year" was one plant's output, given as what capacity is). It is told only when the
# question asks for an amount.
_FIGURE = re.compile(r"^(?:(?:between|about|around|approximately|roughly|nearly|almost|over|under|more than|less than|"
                     r"up to|at least|at most)\s+)?[0-9]")
_ASKS_AMOUNT = re.compile(r"(?<![a-z])(?:how (?:much|many|big|large|long|old|tall|high|far)|population|size|number of|"
                          r"amount|figure)(?![a-z])")


# Senses (owner's priority #1, 2026-10-30): one name can mean different things on different pages. Definitions
# that share a page or a content word are one sense; facts of another kind follow the sense of a page they share.
SENSE_PREDICATES = frozenset({"is", "is_a", "was_a", "was", "means", "refers_to", "is_called"})
_SENSE_WORD = re.compile(r"[a-z]{4,}")
SENSE_STOP = frozenset({"that", "this", "with", "from", "which", "their", "there", "have", "been", "into", "used",
                        "such", "also", "other", "more", "most", "some", "they", "than", "when", "where", "what",
                        "about", "between", "being", "each", "only", "over", "very", "both", "many", "much", "make",
                        "made", "part", "kind", "type", "form", "thing", "things", "person", "people", "something",
                        "known", "called", "often", "usually", "first", "large", "small", "term", "word", "name"})


def _defines(known: Known) -> bool:
    """A definition says what the idea is; a figure is one thing's value, never a sense of its own."""
    return known.predicate in SENSE_PREDICATES and not _FIGURE.match(known.object.lower())


def _senses(told: list[list[tuple[int, Known]]], concept: str) -> list[list[list[tuple[int, Known]]]]:
    """The groups of facts split into senses, best first; a single sense when the definitions agree."""
    own = set(concept.split())
    definitions = [g for g in told if _defines(g[0][1])]
    parent = list(range(len(definitions)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    pages = [{k.page for _, k in g} for g in definitions]
    words = [set(_SENSE_WORD.findall(g[0][1].object.lower())) - SENSE_STOP - own for g in definitions]
    for i in range(len(definitions)):
        for j in range(i + 1, len(definitions)):
            if pages[i] & pages[j] or words[i] & words[j]:
                parent[root(j)] = root(i)
    clusters: dict[int, list[int]] = {}
    for i in range(len(definitions)):
        clusters.setdefault(root(i), []).append(i)
    if len(clusters) < 2:
        return [told]
    senses: list[list[list[tuple[int, Known]]]] = []
    for members in clusters.values():
        sense_pages = set().union(*(pages[i] for i in members))
        senses.append([g for g in told if not _defines(g[0][1]) and {k.page for _, k in g} & sense_pages]
                      + [definitions[i] for i in members])
    claimed = {id(g) for sense in senses for g in sense}
    loose = [g for g in told if id(g) not in claimed]  # facts sharing no page with any definition
    senses.sort(key=lambda sense: (
        0 if any(k.page.startswith("file:") or page_subject(k.page) == concept for g in sense for _, k in g) else 1,
        -len({k.page for g in sense for _, k in g}), -len(sense), min(g[0][0] for g in sense)))
    senses[0] = senses[0] + loose
    return senses


def answer_parts(known: list[Known], question: str, *, limit: int = 3) -> dict[str, object] | None:
    """What an answer about the concept a question names is made of (shared by answer_from and Lantern's own
    composer): the concept, the chosen relations with their pages, who read them, the sources, any
    disagreement and how much more is known."""
    text = " ".join(question.lower().replace("?", " ").split())
    names = list(dict.fromkeys(k.concept for k in known))
    named = [n for n in names if re.search(rf"(?<![a-z]){re.escape(n)}(?:s|es)?(?![a-z])", text)
             or (n.endswith("y") and re.search(rf"(?<![a-z]){re.escape(n[:-1])}ies(?![a-z])", text))]
    if not named:
        return None
    concept = max(named, key=lambda n: (len(n), -names.index(n)))
    groups: dict[tuple[str, str], list[tuple[int, Known]]] = {}
    for index, k in enumerate(known):
        if k.concept == concept:
            groups.setdefault((k.predicate, k.object.lower()), []).append((index, k))
    # Disputed relations are not stated as facts; the answer names the disagreement instead.
    disputed = sorted({items[0][1].object for items in groups.values() if items[0][1].predicate in ("is", "is_a")
                       and ("is_not", items[0][1].object.lower()) in groups})
    settled = [items for items in groups.values() if items[0][1].object not in disputed]
    amount = _ASKS_AMOUNT.search(text) is not None
    told = [items for items in settled if amount or not _FIGURE.match(items[0][1].object.lower())]
    senses = _senses(told, concept)
    others = list(dict.fromkeys(h for sense in senses[1:] for g in sense for _, k in g if (h := _source_name(k.page))))
    ranked = sorted(senses[0], key=lambda items: (
        -len({k.page for _, k in items}),
        # a page about the idea itself, or the owner's own file, before a page that mentions it in passing
        0 if any(k.page.startswith("file:") or page_subject(k.page) == concept for _, k in items) else 1,
        ORDER.index(items[0][1].predicate) if items[0][1].predicate in ORDER else len(ORDER),
        items[0][0],
    ))
    chosen = ranked[:limit]
    pages = list(dict.fromkeys(k.page for items in chosen for _, k in items if k.page))
    hosts = list(dict.fromkeys(h for h in (_source_name(p) for p in pages) if h))
    agreed = any(len({k.page for _, k in items}) > 1 for items in chosen)
    if not chosen:
        where = ""
    elif len(pages) == 1:
        where = f" (from {hosts[0] if hosts else 'my reading'})"
    elif agreed:
        where = f" ({len(pages)} pages agree, from {', '.join(hosts)})"
    else:
        where = f" (from {', '.join(hosts)})"
    more = len(settled) - len(chosen) - sum(len(sense) for sense in senses[1:])
    other = (f" I've also read about {concept} in another sense (from {', '.join(others[:2])})."
             if len(senses) > 1 else "")
    return {
        "concept": concept,
        "facts": [(items[0][1].predicate, items[0][1].object) for items in chosen],
        "lead": _lead([k for items in chosen for _, k in items]) if chosen else "From my reading",
        "where": where,
        "doubt": f" My sources disagree about whether it is {disputed[0]}." if disputed else "",
        "extra": (f" I know {more} more thing{'s' if more != 1 else ''} about it." if more > 0 else "") + other,
    }


def answer_from(known: list[Known], question: str, *, limit: int = 3) -> str | None:
    """Compose what is understood about the concept a question names. Shared with the phone (digest vectors).

    The same relation from several pages counts once; what most pages agree on
    comes first; when pages disagree, the answer says so instead of choosing.
    """
    parts = answer_parts(known, question, limit=limit)
    if parts is None:
        return None
    concept, facts = parts["concept"], parts["facts"]
    if not facts:
        return f"From my reading: I'm not sure about {concept}.{parts['doubt']}"
    said = "; ".join(f"{concept if i == 0 else 'it'} {_with_article(predicate, obj)}"
                     for i, (predicate, obj) in enumerate(facts))
    return f"{parts['lead']}: {said}{parts['where']}.{parts['doubt']}{parts['extra']}"


def tidy(memory: MemoryStore) -> int:
    """Forget learned relations that today's rules would not have kept (site furniture, cut-off phrases).

    Owner-chosen review ("Lantern decides"); audited forgetting, like !unlearn. Returns how many.
    """
    rows = memory._connection.execute(
        "SELECT c.id, e.canonical_name, c.object_text, s.reference FROM claims AS c JOIN entities AS e "
        "ON e.id = c.subject_entity_id LEFT JOIN sources AS s ON s.id = c.source_id "
        "WHERE e.kind = ? AND c.status = 'active' AND c.evidence_type = ?", (CONCEPT_KIND, EVIDENCE),
    ).fetchall()
    forgotten = 0
    for claim_id, subject, obj, reference in rows:
        if not keeps(str(subject), str(obj), _page_of(str(reference or ""))):
            memory.forget("claim", str(claim_id), requested_by="lantern-tidy", reason_code="correction")
            forgotten += 1
    return forgotten


def known_relations(memory: MemoryStore) -> list[Known]:
    """Every relation Prime learned from reading, oldest first."""
    rows = memory._connection.execute(
        "SELECT e.canonical_name, c.predicate, c.object_text, s.reference FROM claims AS c "
        "JOIN entities AS e ON e.id = c.subject_entity_id LEFT JOIN sources AS s ON s.id = c.source_id "
        "WHERE e.kind = ? AND c.status = 'active' AND c.evidence_type = ? ORDER BY c.created_at, c.rowid",
        (CONCEPT_KIND, EVIDENCE),
    )
    return [Known(str(r[0]), str(r[1]), str(r[2]), _page_of(str(r[3] or "")), _via(str(r[3] or ""))) for r in rows]


def _via(reference: str) -> str:
    """The node a hive batch came from: reading:hive-NODE-BATCH:PAGE -> NODE."""
    session = reference.split(":", 2)[1] if reference.count(":") >= 2 else ""
    return session[len("hive-"):-17] if session.startswith("hive-") and len(session) > len("hive-") + 17 else ""


def understanding(memory: MemoryStore, question: str, *, limit: int = 3) -> str | None:
    """What Prime understands about the concept a question names, from its reading, with sources; None if nothing."""
    return answer_from(known_relations(memory), question, limit=limit)


def remembered_reading(journals: Path) -> set[str]:
    """Every page address read before, so a new session never reads it again."""
    seen: set[str] = set()
    for path in journals.glob("*.json") if journals.is_dir() else []:
        try:
            seen.update(str(p["url"]) for p in json.loads(path.read_text(encoding="utf-8")).get("pages", []))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return seen


def iter_relations(memory: MemoryStore) -> Iterable[tuple[str, str, str]]:
    for row in memory._connection.execute(
        "SELECT e.canonical_name, c.predicate, c.object_text FROM claims AS c JOIN entities AS e "
        "ON e.id = c.subject_entity_id WHERE e.kind = ? AND c.status = 'active' ORDER BY c.created_at",
        (CONCEPT_KIND,),
    ):
        yield str(row[0]), str(row[1]), str(row[2])


def _different_things(concept: str, page_a: str, page_b: str) -> bool:
    """A figure held under a bare measure ("median age") from pages about different things (Burundi, Rwanda)
    is two things' figures, not a disagreement (Gate 6, owner's 1.1 item: "whose figure")."""
    return _MEASURE_BARE.match(concept) is not None and page_subject(page_a) != page_subject(page_b)


_TOPIC_MEASURE = re.compile(rf"^(?:has )?(?:a|an) {_MEASURES} of ")


def dispute_topic(dispute: dict[str, object]) -> str:
    """What to read to settle a disagreement, keeping whose figure it is: "median age of burundi"."""
    measured = _TOPIC_MEASURE.match(str(dispute.get("object", "")).lower()) if dispute.get("kind") == "numbers" else None
    return f"{measured['measure']} of {dispute['concept']}" if measured else str(dispute["concept"])


def disputes(memory: MemoryStore, limit: int = 20) -> list[dict[str, object]]:
    """Where Prime's sources disagree (Gate 6: conflicting sources stay visible, never resolved silently).

    Newest first, one entry per concept and statement:
    - kind "negation": one page says it, another denies it ("says" and "denies" name the sources);
    - kind "numbers": pages give different numbers ("versions": each value with its sources).
    "sides" lists each position with its claims, in a fixed order, for the owner's verdict.
    """
    rows = memory._connection.execute(
        "SELECT e.canonical_name, a.predicate, a.object_text, sa.reference, b.predicate, b.object_text, sb.reference, "
        "a.id, b.id "
        "FROM claim_links AS l JOIN claims AS a ON a.id = l.from_claim_id JOIN claims AS b ON b.id = l.to_claim_id "
        "JOIN entities AS e ON e.id = a.subject_entity_id LEFT JOIN sources AS sa ON sa.id = a.source_id "
        "LEFT JOIN sources AS sb ON sb.id = b.source_id "
        "WHERE l.relation = 'contradicts' AND a.status = 'active' AND b.status = 'active' AND e.kind = ? "
        "ORDER BY l.rowid DESC", (CONCEPT_KIND,),
    ).fetchall()
    found: dict[tuple[str, str, str], dict[str, object]] = {}
    for concept, predicate, obj, ref_a, other_predicate, other_obj, ref_b, id_a, id_b in rows:
        source_a, source_b = (_source_name(_page_of(str(r or ""))) for r in (ref_a, ref_b))
        if predicate == other_predicate and _different_things(str(concept), _page_of(str(ref_a or "")),
                                                              _page_of(str(ref_b or ""))):
            continue  # linked before figures kept whose they are: two things' figures, not a disagreement
        if predicate == other_predicate:  # the same statement with a different number
            measure = _number_key(str(obj)) or ("", "", "", [], "")
            key = (str(concept), "numbers", f"{predicate} {measure[0]} # {measure[1]}")
            if key not in found and len(found) >= limit:
                continue
            entry = found.setdefault(key, {"concept": str(concept), "kind": "numbers",
                                           "object": _with_article(str(predicate), str(obj)), "versions": {}})
            for value, source, claim in ((obj, source_a, id_a), (other_obj, source_b, id_b)):
                side = entry["versions"].setdefault(_with_article(str(predicate), str(value)), (set(), set()))
                side[0].add(source)
                side[1].add(str(claim))
            continue
        key = (str(concept), "negation", str(obj).casefold())
        if key not in found and len(found) >= limit:
            continue
        entry = found.setdefault(key, {"concept": str(concept), "kind": "negation", "object": str(obj),
                                       "says": set(), "denies": set(), "says_claims": set(), "denies_claims": set()})
        positive, negative = ((source_a, id_a), (source_b, id_b)) if predicate != "is_not" else ((source_b, id_b), (source_a, id_a))
        entry["says"].add(positive[0])
        entry["denies"].add(negative[0])
        entry["says_claims"].add(str(positive[1]))
        entry["denies_claims"].add(str(negative[1]))
    result = []
    for entry in found.values():
        if entry["kind"] == "numbers":
            versions = [{"value": v, "sources": sorted(s), "claims": sorted(c)} for v, (s, c) in sorted(entry["versions"].items())]
            result.append({**entry, "versions": versions,
                           "sides": [{"says": v["value"], "claims": v["claims"]} for v in versions]})
        else:
            result.append({"concept": entry["concept"], "kind": "negation", "object": entry["object"],
                           "says": sorted(entry["says"]), "denies": sorted(entry["denies"]),
                           "sides": [{"says": f"yes, {entry['concept']} is {entry['object']}", "claims": sorted(entry["says_claims"])},
                                     {"says": f"no, {entry['concept']} is not {entry['object']}", "claims": sorted(entry["denies_claims"])}]})
    for item in result:
        item["verdict"] = verdict_of(memory, item)
    return result


VERDICT_SOURCE = "owner-verdict"


def _verdicts_for(memory: MemoryStore, claims: list[str]) -> list[str]:
    """Owner verdict claims that back any of these claims."""
    if not claims:
        return []
    marks = ",".join("?" * len(claims))
    return [str(r[0]) for r in memory._connection.execute(
        f"SELECT v.id FROM claim_links AS l JOIN claims AS v ON v.id = l.from_claim_id JOIN sources AS s ON s.id = v.source_id "
        f"WHERE l.relation = 'supports' AND v.status = 'active' AND s.kind = ? AND l.to_claim_id IN ({marks})",
        (VERDICT_SOURCE, *claims))]


def verdict_of(memory: MemoryStore, dispute: dict[str, object]) -> int | None:
    """Which side the owner judged right (an index into "sides"), if any."""
    for index, side in enumerate(dispute["sides"]):
        if _verdicts_for(memory, list(side["claims"])):
            return index
    return None


def record_verdict(memory: MemoryStore, dispute: dict[str, object], choice: int | None) -> str | None:
    """The owner's verdict on a disagreement (owner, 2026-10-24).

    Both sides stay; the owner's judgment is kept as the owner's own statement backing one side.
    A new verdict replaces an earlier one, and None withdraws it. Returns the side chosen.
    """
    sides = list(dispute["sides"])
    for side in sides:
        for claim in _verdicts_for(memory, list(side["claims"])):
            memory.forget("claim", claim, requested_by="owner", reason_code="owner_request")
    if choice is None:
        return None
    if not 0 <= choice < len(sides):
        raise ValueError("no such side")
    chosen = sides[choice]
    row = memory._connection.execute(
        "SELECT subject_entity_id, predicate, object_text FROM claims WHERE id = ?", (chosen["claims"][0],)).fetchone()
    source = memory.add_source(VERDICT_SOURCE, f"verdict:{dispute['concept']}")
    claim = memory.add_claim(str(row[0]), str(row[1]), str(row[2]), evidence_type="reported_by_amelie", confidence=1.0,
                             source_id=source, consequential=False)
    for target in chosen["claims"]:
        memory.link_claims(claim, target, relation="supports", reason="The owner judged this side right (!verdict).")
    return str(chosen["says"])


def dispute_line(dispute: dict[str, object]) -> str:
    """One disagreement as the owner reads it."""
    if dispute["kind"] == "numbers":
        line = f"{dispute['concept']}: " + " / ".join(
            f"{v['value']} per {', '.join(v['sources'])}" for v in dispute["versions"])
    else:
        line = (f"{dispute['concept']}: is it {dispute['object']}? yes per {', '.join(dispute['says'])}; "
                f"no per {', '.join(dispute['denies'])}")
    if dispute.get("verdict") is not None:
        line += f" (you said: {dispute['sides'][dispute['verdict']]['says']})"
    return line
