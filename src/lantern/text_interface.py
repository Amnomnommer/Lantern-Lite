"""Tablet-local, honest text interface for Lantern's Gate 2 memory."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence, TextIO

from lantern.companion import EMPTY_LIBRARY, WORKSTATION, Library, Place, respond
from lantern.learning import companion_learning as learning
from lantern.learning.concepts import OwnerTurn
from lantern.learning.phone_ledger import view as learning_view
from lantern.phone_experience import turn_digest, validate_learning
from lantern import association_mind, belief_mind, idea_space, predictive_core, connotation_mind, console_mind, expectation_mind, language_mind, self_mind, instance_personality, intents, quantity_skill, reading_digest, sleep_mind
from lantern.vault_guard import require_vault_open
from lantern.memory import (
    Claim,
    EpisodeConsolidator,
    MemoryStore,
    StagedObservation,
    StagingStore,
)


DEFAULT_DATABASE = Path("/srv/lantern/memory/lantern.db")
DEFAULT_STAGING = Path("/srv/lantern/staging/owner-interface.sqlite3")
LEARNING_LABEL = "Prime on the Workstation; every change is yours to adopt or revert"

_WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_QUESTION_WORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "can",
        "could",
        "do",
        "does",
        "for",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "of",
        "on",
        "please",
        "tell",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "would",
        "you",
    }
)
_ASK_RESULT_LIMIT = 5
_ENTITY_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class InterfaceReply:
    text: str
    exit_requested: bool = False


class LocalTextInterface:
    """Deterministic interaction surface with review-before-commit semantics."""

    def __init__(
        self,
        memory: MemoryStore,
        staging: StagingStore,
        *,
        companion: bool = False,
        owner_name: str = "Amelie",
        place: Place = WORKSTATION,
        library: Library = EMPTY_LIBRARY,
        ledger_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        journals: Path | None = None,
        conversations: Path | None = None,
        personality_path: Path | None = None,
        talk_folder: Path | None = None,
        sleep_path: Path | None = None,
        associations: "association_mind.AssociationStore | None" = None,
        feelings: "connotation_mind.ConnotationStore | None" = None,
        expectations: "expectation_mind.ExpectationStore | None" = None,
        console_path: Path | None = None,
        ideas: "idea_space.IdeaSpace | None" = None,
        core: "predictive_core.PredictiveCore | None" = None,
        language: "language_mind.LanguageStore | None" = None,
        self_path: Path | None = None,
    ) -> None:
        self.memory = memory
        # The sleep mind (owner amendment, 2026-10-24): consolidates once a day, at the first opening.
        self.sleep = None if sleep_path is None else sleep_mind.SleepStore(sleep_path)
        self.associations = associations  # the association mind: what goes with what
        self.ideas = ideas  # the shared idea space: one small vector per idea (what it's like, what follows it)
        self.core = core  # the predictive core: a small network that learns by guessing what comes next
        self.feelings = feelings  # the connotation mind: how ideas feel, and to whom
        self.expectations = expectations  # the expectation mind: prediction, surprise, curiosity
        self.language = language  # Lantern's own language: learned sentence shapes, filled with sourced facts
        # Lantern's self: its life story, its own inner goals and a quiet inner life (owner amendment, 2026-10-24).
        self.self_mind = None if self_path is None else self_mind.SelfMind(self_path)
        self._topics: list[str] = []
        self._last_message: datetime | None = None
        self.staging = staging
        # Conversation first (owner, 2026-10-22): plain text gets the phone's
        # companion reply instead of becoming a pending note.
        self.companion = companion
        self.owner_name = owner_name
        self.place = place
        self.library = library
        self._turns: list[OwnerTurn] = []
        self._exchanges: list[learning.Exchange] = []
        self._last_reply = ""
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._thread = ""
        self.journals = journals  # Prime's reading journals, digested into memory
        # Each day's conversation is kept in the vault so a restart does not forget today.
        self.conversations = conversations
        if companion:
            self._open_day(learning.CONVERSATION_PREFIX + self._now()[:10])
        # This Lantern's own personality (instance state in the vault, never shared through the hive).
        self.personality_path = personality_path
        self.talk_folder = talk_folder  # kept Lantern conversations (Phone Link), for the shared words
        # Kept as Personality Core profiles beside the old levels file: every change a new version.
        self.personality = (None if personality_path is None
                            else instance_personality.PersonalityStore(personality_path.parent))
        self.levels: dict[str, int] | None = (
            None if self.personality is None else self.personality.levels(at=self._now()))
        # Gate 7 in conversation: the phone's ledger format, kept in the vault.
        self.ledger_path = ledger_path
        self.ledger: learning.Ledger = []
        if ledger_path is not None and ledger_path.exists():
            self.ledger = validate_learning(json.loads(ledger_path.read_text(encoding="utf-8")))
        # The console (owner amendment, 2026-10-24): this Lantern's own small neural network, from its temperament.
        self.console = (None if console_path is None else
                        console_mind.Console(console_path, seed=self.place.name if hasattr(self.place, "name") else "Prime",
                                             levels=self.levels))
        self._console_pending: console_mind.Decision | None = None
        self._owner_surprise = 0.0

    def welcome(self) -> str:
        report = self.memory.check_integrity()
        if self.companion:
            health = "my memory is healthy" if report.healthy else "my memory is NOT healthy (try !status)"
            return (
                f"Hi, {self.owner_name}! I'm here on the Workstation and {health}. "
                "How are you doing today? (!help lists what else I can do.)"
            )
        return (
            "Hi, Amelie. I'm online, local, and memory is "
            f"{'healthy' if report.healthy else 'not healthy'}. "
            "My language layer is still tiny, so I'll show exactly what I "
            "understand instead of bluffing. Type !help for the current moves."
        )

    def handle(self, line: str) -> InterfaceReply:
        text = line.strip()
        if not text:
            return InterfaceReply("I received an empty message, so I changed nothing.")
        if text.startswith("!"):
            text = f"/{text[1:]}"
        elif not text.startswith("/") and self.companion and _TERMINAL_COMMAND.match(text):
            # Typed into the chat by mistake: say where it goes instead of chatting about it.
            return InterfaceReply(
                "That looks like a terminal command, not something to say to me. Say !quit first, then run it in "
                "the terminal. I changed nothing."
            )
        elif not text.startswith("/") and self.companion and quantity_skill.answer(text) is not None:
            # A quantity question is answered first (quantity skill), and kept in today's conversation.
            return InterfaceReply(self._converse(text))
        elif not text.startswith("/") and self.companion and (route := intents.route(text, last_reply=self._last_reply)):
            # No commands needed: the phone's phrasings ("yes", "forget that", "good answer", ...).
            return InterfaceReply(self._follow(route))
        elif not text.startswith("/") and self.companion:
            return InterfaceReply(self._converse(text))
        elif not text.startswith("/"):
            session_id = self.staging.stage_session(
                text,
                [
                    StagedObservation(
                        key="owner_message",
                        category="owner_intent",
                        content=text,
                        evidence_type="reported_by_amelie",
                    )
                ],
            )
            return InterfaceReply(
                "I preserved your exact words as a pending note, but I won't "
                "pretend I understand more than that yet. "
                f"Draft {session_id}. Use !review {session_id}, then !commit "
                "or !discard."
            )

        command, _, argument = text.partition(" ")
        command = command.lower()
        argument = argument.strip()
        if command == "/help":
            return InterfaceReply(self._help())
        if command in {"/quit", "/exit"}:
            return InterfaceReply("Session closed. My durable memory remains here.", True)
        if command == "/status":
            return InterfaceReply(self._status())
        if command == "/hive":
            return InterfaceReply(self._hive())
        if command == "/claims":
            return InterfaceReply(self._claims())
        if command == "/entities":
            return InterfaceReply(self._entities())
        if command == "/recall":
            return InterfaceReply(self._recall(argument))
        if command in {"/ask", "/consider"}:
            return InterfaceReply(self._ask(argument))
        if command == "/explain":
            return InterfaceReply(self._explain(argument))
        if command == "/schemas":
            return InterfaceReply(self._schemas())
        if command == "/pending":
            return InterfaceReply(self._pending())
        if command == "/review":
            return InterfaceReply(self._review(argument))
        if command == "/discard":
            self._require_argument(command, argument)
            self.staging.discard_pending(argument)
            return InterfaceReply(f"Discarded pending draft {argument}; it was not learned.")
        if self.companion and command == "/disputes":
            found = reading_digest.disputes(self.memory)
            if not found:
                return InterfaceReply("None of my sources disagree with each other right now.")
            lines = ["Where my sources disagree (I keep both sides and don't choose alone):"]
            for number, dispute in enumerate(found, 1):
                lines.append(f"{number}. {reading_digest.dispute_line(dispute)}")
                lines += [f"   ({chr(97 + i)}) {side['says']}" for i, side in enumerate(dispute["sides"])]
            lines.append("Tell me which you believe with !verdict NUMBER LETTER (or !verdict NUMBER unsure).")
            return InterfaceReply("\n".join(lines))
        if self.companion and command == "/verdict":
            return InterfaceReply(self._verdict(argument))
        if self.companion and self.console is not None and command == "/console":
            return InterfaceReply(self._console_command(argument))
        if self.companion and self.expectations is not None and command in ("/growth", "/surprises", "/expect"):
            if command == "/growth":
                return InterfaceReply(expectation_mind.explain_growth(self.expectations))
            if command == "/surprises":
                return InterfaceReply(expectation_mind.explain_surprises(self.expectations))
            if not (argument or "").strip():
                return InterfaceReply("Use !expect IDEA.")
            return InterfaceReply(expectation_mind.explain_expect(self.expectations, argument or ""))
        if self.companion and self.feelings is not None and command == "/feel":
            if not (argument or "").strip():
                return InterfaceReply("Use !feel IDEA.")
            return InterfaceReply(connotation_mind.explain(self.feelings, argument or ""))
        if self.companion and self.core is not None and command == "/predict":
            return InterfaceReply(predictive_core.explain_guess(self.core, argument or ""))
        if self.companion and self.core is not None and command == "/core":
            return InterfaceReply(self._core_command(argument))
        if self.companion and self.journals is not None and command == "/sites":
            from lantern import reading_priority
            return InterfaceReply(reading_priority.explain(self.journals.parent))
        if self.companion and self.journals is not None and command == "/research":
            from lantern import research_mind
            return InterfaceReply(research_mind.explain(self.journals.parent))
        if self.companion and self.ideas is not None and command == "/similar":
            if not (argument or "").strip():
                return InterfaceReply("Use !similar IDEA.")
            return InterfaceReply(idea_space.explain(self.ideas, argument or ""))
        if self.companion and self.associations is not None and command == "/associations":
            if not (argument or "").strip():
                return InterfaceReply("Use !associations IDEA.")
            return InterfaceReply(association_mind.explain(self.associations, argument or ""))
        if self.companion and self.self_mind is not None and command in (
                "/story", "/yesterday", "/goals", "/drop", "/reflect", "/think"):
            return InterfaceReply(self._self_command(command, argument))
        if self.companion and self.language is not None and command == "/language":
            return InterfaceReply(self.language.explain())
        if self.companion and self.sleep is not None and command == "/belief":
            if not (argument or "").strip():
                return InterfaceReply("Use !belief IDEA.")
            return InterfaceReply(belief_mind.explain(self.sleep.connection, argument or ""))
        if self.companion and self.sleep is not None and command in ("/patterns", "/accept", "/reject"):
            return InterfaceReply(self._review_patterns(command, argument))
        if self.companion and self.sleep is not None and command in ("/sleep", "/volume", "/cold", "/restore"):
            return InterfaceReply(self._sleep_command(command, argument))
        if self.companion and command == "/readings":
            return InterfaceReply(self._readings())
        if self.companion and command == "/unlearn":
            self._require_argument(command, argument)
            return InterfaceReply(self._unlearn(argument))
        if self.companion and self.talk_folder is not None and command in ("/words", "/retire"):
            return InterfaceReply(self._words(command, argument))
        if self.companion and self.levels is not None and command in ("/personality", "/tone", "/voice"):
            return InterfaceReply(self._personality(command, argument))
        if self.companion and command in _LEARNING_COMMANDS:
            return InterfaceReply(self._learn(command, argument))
        if command == "/remember":
            if self.companion and "|" not in argument:
                return InterfaceReply(self._remember_words(argument))
            return InterfaceReply(self._stage_claim(argument))
        if command == "/infer":
            return InterfaceReply(self._stage_inference(argument))
        if command == "/entity":
            return InterfaceReply(self._stage_entity(argument))
        if command == "/correct":
            return InterfaceReply(self._stage_correction(argument))
        if command == "/commit":
            return InterfaceReply(self._commit(argument))
        return InterfaceReply(
            f"I don't know the command {command}. Type !help; I changed nothing."
        )

    def _now(self) -> str:
        return self._clock().replace(microsecond=0).isoformat()

    def _owner_notes(self) -> list[tuple[str, str]]:
        """(episode id, words) of everything the owner asked Prime to remember, oldest first."""
        return [(str(row["id"]), str(row["content"])) for row in self.memory._connection.execute(
            "SELECT e.id, f.content FROM episode_facts AS f JOIN episodes AS e ON e.id = f.episode_id "
            "WHERE f.observation_key = 'owner_message' ORDER BY e.created_at, e.id, f.sequence")]

    def _forget_note(self, episode_id: str, words: str) -> str:
        self.memory.forget("episode", episode_id, requested_by="owner", reason_code="owner_request")
        return f'I have let go of: "{words}"'

    def _follow(self, route: intents.Route) -> str:
        notes = self._owner_notes()
        if route.action == "remember":
            return self._remember_words("")
        if route.action == "remember_words":
            return self._remember_words(route.text)
        if route.action == "forget_newest":
            return self._forget_note(*notes[-1]) if notes else "I'm not remembering anything about you yet, so there's nothing to forget."
        if route.action == "forget_about":
            found = [note for note in notes if route.text in note[1].lower()]
            return self._forget_note(*found[-1]) if found else f'I don\'t remember anything about "{route.text}".'
        if route.action == "correct":
            if not notes:
                return "I'm not remembering anything yet to correct."
            old = notes[-1][1]
            self.memory.forget("episode", notes[-1][0], requested_by="owner", reason_code="correction")
            self._remember_words(route.text)
            return f'Got it, I\'ve corrected that. I now remember: "{route.text}" (instead of "{old}").'
        if route.action in ("good", "bad"):
            return self._learn(f"/{route.action}", "")
        if route.action in ("keep", "stop"):
            return self._keep_or_stop(route.action)
        if route.action == "name":
            self.owner_name = route.text
            return f"I will call you {route.text}."
        if route.action == "memories":
            if not notes:
                return 'I\'m not remembering anything about you yet. Tell me something and say "remember that".'
            listed = "\n".join(f"{i}. {words}" for i, (_, words) in enumerate(notes, 1))
            return f"What you told me (I remember it in your words):\n{listed}\nSay \"forget that\", \"forget about …\" or \"no, I meant …\"."
        if route.action == "library":
            return self._readings()
        if route.action == "learning":
            return self._learn("/learning", "")
        if route.action == "help":
            return self._help()
        return route.text

    def _keep_or_stop(self, action: str) -> str:
        last = self._exchanges[-1] if self._exchanges else None
        if last is None:
            return "There's nothing to keep yet. Talk with me a little first." if action == "keep" else "I haven't said anything yet to stop."
        view = learning_view(self.ledger)
        strategy = last.strategy
        if strategy == "baseline":
            return self._learn("/good" if action == "keep" else "/bad", "")
        if action == "keep":
            if strategy in view.adopted:
                return "I already reply that way; I'll keep it."
            evaluation = view.evaluations.get(strategy)
            retained = evaluation is not None and evaluation["data"]["decision"] == "retain_candidate"
            return self._learn("/adopt" if retained else "/good", strategy if retained else "")
        if strategy in view.adopted:
            return self._learn("/revert", strategy)
        if strategy in view.trials:
            return self._learn("/stop", strategy)
        return self._learn("/bad", "")

    def _converse(self, text: str) -> str:
        at = self._now()
        thread = learning.CONVERSATION_PREFIX + at[:10]
        if thread != self._thread:  # a new day starts a new conversation
            self._open_day(thread)
        turn = OwnerTurn(turn_digest(thread, len(self._turns), "note", text, at), text, at)
        # Learning on the backend: a reaction rates the last reply, trials start once
        # reading gives material, and running trials are judged as ratings arrive.
        ledger = self.ledger
        previous = self._exchanges[-1] if self._exchanges else None
        rated = previous is None or any(
            r.turn == previous.turn.digest and r.about == "reply" for r in learning_view(ledger).ratings
        )
        cue = learning.reaction(text)
        if cue is not None and not rated:
            ledger, _ = learning.rate(ledger, previous, thread, cue[0], f"implicit: {cue[1]}", at)
        if self.console is not None:
            # What happened next teaches the console: a reaction to the last reply, and any idea a voice
            # brought in (heard or not) coming back in the owner's own words.
            if cue is not None and self._console_pending is not None:
                self.console.reward(self._console_pending, cue[0])
            self.console.notice(text, "you")
        ledger = learning.auto_trials(ledger, self.library, at)
        ledger, notice = learning.auto_evaluate(ledger, at)
        self._turns.append(turn)
        self.observe(text, "you")  # the owner's own words teach the minds too
        worked = quantity_skill.answer(text)
        if worked is not None:  # worked out from the owner's numbers, answered before anything social
            self._save_ledger(ledger)
            self._exchanges.append(learning.Exchange(turn, "quantity", worked, (), "baseline"))
            self._last_reply = worked
            self._save_day()
            return worked
        returning = None
        if self.self_mind is not None:
            now = datetime.now(timezone.utc)
            idle = self._last_message is None or now - self._last_message >= timedelta(minutes=self_mind.IDLE_MINUTES)
            self._last_message = now
            named = [] if self.sleep is None else sleep_mind.named(self.memory, text)
            for topic in sorted(named, key=len, reverse=True)[:3]:  # ideas only, never the owner's words
                self.self_mind.record("topic", topic)
                self._topics.append(topic)
            if idle:  # back after a while: reflect, and share a thought once
                self.reflect()
                thought = self.self_mind.unshared_thought()
                returning = None if thought is None else f"While you were away I was thinking: {thought}"
        reply = respond(
            text,
            turn_index=len(self._turns) - 1,
            owner_name=self.owner_name,
            facts=self._remembered(),
            library=self.library,
            previous_reply=self._last_reply,
            place=self.place,
            understanding=self._understand,
        )
        shaped, strategy = learning.shape(reply, thread, self._turns, turn, learning_view(ledger), self.library)
        rules: tuple[str, ...] = ()
        if self.levels is not None and self.personality.enabled():  # the owner can turn the voice off
            shaped, rules = instance_personality.voice(shaped, reply.act, self.levels)
        if notice is not None and reply.act != "care":
            shaped = f"{shaped} ({notice})"
        self._save_ledger(ledger)
        self._exchanges.append(learning.Exchange(turn, reply.act, shaped, rules, strategy))
        self._last_reply = shaped
        self._save_day()
        return shaped if returning is None else f"{returning} {shaped}"

    def _day_path(self, thread: str) -> Path | None:
        return None if self.conversations is None else self.conversations / f"{thread[len(learning.CONVERSATION_PREFIX):]}.json"

    def _open_day(self, thread: str) -> None:
        """Start (or resume) the conversation thread for one day."""
        self._thread, self._turns, self._exchanges, self._last_reply = thread, [], [], ""
        path = self._day_path(thread)
        if path is None or not path.exists():
            return
        try:
            day = json.loads(path.read_text(encoding="utf-8"))
            turns = {t["digest"]: OwnerTurn(t["digest"], t["text"], t["at"]) for t in day["turns"]}
            self._turns = list(turns.values())
            self._exchanges = [learning.Exchange(turns[e["turn"]], e["act"], e["reply"], (), e["strategy"])
                               for e in day["exchanges"]]
        except (OSError, ValueError, KeyError, TypeError):
            # A damaged day file must not stop Prime talking; start the day fresh and keep the file.
            self._turns, self._exchanges = [], []
            return
        self._last_reply = self._exchanges[-1].reply if self._exchanges else ""

    def _save_day(self) -> None:
        path = self._day_path(self._thread)
        if path is None:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        day = {
            "format": "lantern_prime_conversation_day_v1", "thread": self._thread,
            "turns": [{"at": t.at, "digest": t.digest, "text": t.text} for t in self._turns],
            "exchanges": [{"act": e.act, "reply": e.reply, "strategy": e.strategy, "turn": e.turn.digest}
                          for e in self._exchanges],
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(day, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def observe(self, text: str, community: str = "reading") -> None:
        """Feed every mind that learns from text (association, connotation)."""
        if self.associations is not None:
            self.associations.observe(text, community)
        if self.ideas is not None:
            self.ideas.observe(text, community)
        if self.core is not None:
            self.core.observe(text, community)
        if self.feelings is not None:
            self.feelings.observe(text, community)
        if self.language is not None:
            self.language.observe(text, community)
        if self.expectations is not None:
            cost = self.expectations.observe(text, community)
            if community == "you":  # how surprising the owner's words were (bits per idea), for the console
                count = sum(len(s) for s in association_mind.sentence_ideas(text))
                self._owner_surprise = cost / count if count else 0.0

    def wake(self) -> str:
        """The first opening of the day: one night's sleep, and what it did (empty if it already slept)."""
        if self.sleep is None:
            return ""
        keep = frozenset() if self.expectations is None else frozenset(self.expectations.salient())
        report = sleep_mind.sleep(self.memory, self.sleep, keep=keep)
        morning = sleep_mind.morning(report)
        if report is not None and self.console is not None:
            self.console.replay()  # the console goes over the day's lessons once more
        if report is not None and self.self_mind is not None:
            self.self_mind.record("slept", morning)
            self.reflect()
        if report is not None and self.expectations is not None and self.journals is not None:
            # Curiosity from learning progress: where it learns fastest, or is still surprised, it reads next.
            wondered = self.expectations.wonder(self.journals.parent / "wonders.json")
            self.expectations.prune()
            if self.associations is not None:
                self.associations.prune()  # bounded: coincidences seen once go first
            if self.ideas is not None:
                self.ideas.tidy()  # the idea space stays a fixed size
            if wondered:
                morning += f" I woke up curious about {', '.join(wondered)}."
        return morning

    def reflect(self) -> dict[str, object]:
        """One quiet reflection (inner life): goals, connections, surprises. No network, no actions."""
        if self.self_mind is None:
            return {"reflected": False}
        return self_mind.reflect(self.self_mind, memory=self.memory, associations=self.associations,
                                 expectations=self.expectations, topics=self._topics[-10:],
                                 wonders=None if self.journals is None else self.journals.parent / "wonders.json")

    def note_reading(self, pages: int, learned: int, ideas: list[str] | None = None) -> None:
        """A reading session joins the life story, and what it learned teaches the console (ideas that came back)."""
        if self.console is not None and learned:
            fresh = {k.concept for k in reading_digest.known_relations(self.memory)[-learned:]}
            self.console.notice(" ; ".join(sorted(fresh)), "reading")
        if self.self_mind is not None and (pages or learned):
            about = f", mostly about {', '.join(ideas[:3])}" if ideas else ""
            what = f"{pages} page(s)" if pages else "what I had been given"
            self.self_mind.record("read", f"{what}{about}, and understood {learned} new thing(s)")

    def _self_command(self, command: str, argument: str | None) -> str:
        mind = self.self_mind
        word = (argument or "").strip().lower()
        if command == "/story":
            return mind.tell(word or None)
        if command == "/yesterday":
            return mind.tell("yesterday")
        if command == "/goals":
            return mind.explain_goals()
        if command == "/drop":
            goals = mind.goals()
            if not word.isdigit() or not 1 <= int(word) <= len(goals):
                return "Use !drop NUMBER, with a number from !goals."
            goal = goals[int(word) - 1]
            mind.finish(int(goal["id"]), "the owner let it go", status="dropped")
            return f"Okay, I've let go of: {self_mind.goal_words(str(goal['kind']), str(goal['about']))}."
        if command == "/reflect":
            if word in ("on", "off"):
                mind.set_reflecting(word == "on")
                return ("I'll reflect quietly while I'm open again." if word == "on"
                        else "I'll stop reflecting on my own until you say !reflect on.")
            return f"I'm {'reflecting quietly while open' if mind.reflecting() else 'not reflecting on my own'}. !reflect on|off"
        result = self.reflect()  # /think: reflect now, aloud
        if not result.get("reflected"):
            return "My reflection is paused (!reflect on)."
        said = []
        if result["goals_done"]:
            said.append(f"I reached a goal about {', '.join(result['goals_done'])}.")
        if result["goals_new"]:
            said.append(f"I set myself new goals about {', '.join(result['goals_new'])}.")
        thought = mind.unshared_thought()
        if thought:
            said.append(thought)
        return " ".join(said) or "I thought it over; nothing new stood out."

    def _understand(self, question: str) -> str | None:
        """What reading taught about an idea, plus one thing sleep worked out; asking counts as using it."""
        answer = reading_digest.understanding(self.memory, question)
        if self.sleep is None:
            return answer
        names = sleep_mind.named(self.memory, question)
        self.sleep.touch(names)
        if not names:
            words = [w for w in re.findall(r"[a-z][a-z' -]+", question.lower())]
            cold = next((c for w in words for c in [w.strip()] if self.sleep.cold_matches(c)), None)
            if answer is None and cold:
                return f"I set aside what I knew about {cold} a while ago. !restore {cold} brings it back."
            return answer
        top = max(names, key=len)
        approved = sleep_mind.generalizations(self.memory, top)
        if answer is not None and approved:  # the owner approved it: always said
            predicate, obj = approved[0]
            answer += f" Most {top}s I know {reading_digest._with_article(predicate, obj)} (a pattern you approved)."
        if answer is None:
            return answer
        sure = belief_mind.sureness(self.sleep.connection, top)  # evidence, never ratings
        if self.language is not None and self.language.ready():
            # Lantern's own words: its learned sentence shapes filled with its sourced facts (grounded).
            parts = reading_digest.answer_parts(reading_digest.known_relations(self.memory), question)
            composed = None if parts is None else self.language.compose(str(parts["concept"]), list(parts["facts"]))
            if composed:
                lead = str(parts["lead"]) + (f", {sure}" if sure and parts["lead"] == "From my reading" else "")
                answer = f"{lead}: {composed}{parts['where']}.{parts['doubt']}{parts['extra']}"
                sure = None
        if sure and answer.startswith("From my reading:"):
            answer = f"From my reading, {sure}:" + answer[len("From my reading:"):]
        # The minds at the console each propose something; the console chooses whom to listen to.
        offered: dict[str, dict[str, object]] = {}
        worked = self.sleep.worked_out(top, limit=1)
        if worked:
            predicate, obj, because = worked[0]
            offered["sleep"] = {"text": f" I also worked out that {top} {reading_digest._with_article(predicate, obj)} "
                                        f"(because {because}).", "ideas": obj.split()[-1:], "strength": 0.6}
        near: list[dict[str, object]] = []
        if self.ideas is not None:  # the idea space: ideas that keep the same company
            near = [{"idea": n["idea"], "strength": float(n["similarity"]) * 4}
                    for n in self.ideas.similar(top.split()[-1], limit=5) if n["idea"] not in top.split()][:2]
        if not near and self.associations is not None:
            near = [n for n in self.associations.neighbours(top.split()[-1], limit=5)
                    if n["strength"] >= 1.0 and n["idea"] not in top.split()][:2]
        if near:
            offered["associations"] = {"text": f" It reminds me of {' and '.join(str(n['idea']) for n in near)}.",
                                       "ideas": [str(n["idea"]) for n in near],
                                       "strength": min(1.0, float(near[0]["strength"]) / 4)}
        if self.feelings is not None:
            mood = connotation_mind.mood_line(self.feelings, top.split()[-1])
            if mood:
                overall = self.feelings.overall(top.split()[-1]) or {"valence": 0.5}
                offered["feeling"] = {"text": f" {mood}", "ideas": [], "strength": min(1.0, abs(float(overall["valence"])))}
        said = set(re.findall(r"[a-z][a-z'-]+", answer.lower()))  # never "expect" what was just said
        guessed = [] if self.core is None else [
            g for g in self.core.guess(list(self._topics)[-2:] + top.split()) if g["idea"] not in said]
        if guessed:  # the predictive core: what this conversation makes it expect next
            offered["expectation"] = {"text": f" Thinking about {top}, I'd expect {guessed[0]['idea']} to come up. Does it for you?",
                                      "ideas": [str(guessed[0]["idea"])], "strength": min(1.0, float(guessed[0]["score"]) * 2)}
        elif self.expectations is not None:
            follows = [(n, c) for n, c in self.expectations.expect(top.split()[-1]) if n not in top.split() and n not in said]
            if follows:
                nxt, count = follows[0]
                total = sum(c for _, c in follows) or 1
                offered["expectation"] = {"text": f" In what I've read, {nxt} often comes up with {top}. Does it for you?",
                                          "ideas": [nxt], "strength": count / total}
        if not offered:
            return answer
        if self.console is None:
            chosen = list(offered)  # no console: every mind speaks
        else:
            decision = self.console.choose(self._console_context(question), offered)
            self._console_pending = decision
            chosen = decision.chosen
        return answer + "".join(str(offered[v]["text"]) for v in console_mind.VOICES if v in chosen)

    def _core_command(self, argument: str | None) -> str:
        words = (argument or "").split()
        if not words:
            return self.core.explain()
        if words[0] in ("on", "off"):
            self.core.set_enabled(words[0] == "on")
            return ("My predictive core is on: I learn by guessing what comes next." if words[0] == "on"
                    else "My predictive core is off: it neither learns nor guesses until you turn it on again.")
        if words[0] == "restore" and len(words) > 1 and words[1].isdigit():
            try:
                return f"My predictive core is back to how it was on {self.core.restore(int(words[1]))}."
            except ValueError:
                return "No such version; !core lists them."
        return "Use !core, !core on|off, or !core restore N."

    def _console_context(self, text: str) -> list[float]:
        """What the console sees about this moment (identical layout to console_mind.FEATURES)."""
        lowered = text.lower().strip()
        question = 1.0 if "?" in text or lowered.split(" ", 1)[0] in ("what", "who", "how", "why", "when", "where") else 0.0
        warmth = max(-1.0, min(1.0, float(connotation_mind.tone(text)[0])))
        hour = self._clock().hour
        return [1.0, question, min(len(text.split()), 40) / 40, warmth, min(self._owner_surprise / 16, 1.0), 1.0,
                1.0 if hour >= 18 or hour < 6 else 0.0, 0.5]

    def _console_command(self, argument: str | None) -> str:
        words = (argument or "").split()
        if not words:
            return self.console.explain()
        if words[0] in ("on", "off"):
            self.console.set_enabled(words[0] == "on")
            return ("My console is on: I choose whom to listen to, and I learn from how it goes." if words[0] == "on"
                    else "My console is off: every mind takes its fixed turn until you turn it on again.")
        if words[0] == "restore" and len(words) == 2 and words[1].isdigit():
            try:
                return f"My console is back to how it was on {self.console.restore(int(words[1]))}."
            except ValueError:
                return "No such version; !console lists them."
        return "Use !console, !console on|off, or !console restore N."

    def _review_patterns(self, command: str, argument: str | None) -> str:
        """The owner's review of what sleep noticed: accept a pattern as a generalization, or reject it."""
        pending = sleep_mind.pending_patterns(self.sleep)
        if command == "/patterns":
            if not pending:
                return "No patterns are waiting for your review."
            lines = ["Patterns I noticed while sleeping (not facts until you accept them):"]
            lines += [f"{i}. most {c}s I know ({round(sh * 100)}% of {m}) {reading_digest._with_article(p, o)}"
                      for i, (c, p, o, m, sh) in enumerate(pending, 1)]
            lines.append("!accept NUMBER keeps one as a generalization I worked out; !reject NUMBER and I won't suggest it again.")
            return "\n".join(lines)
        number = (argument or "").strip()
        if not number.isdigit() or not 1 <= int(number) <= len(pending):
            return "Use !accept NUMBER or !reject NUMBER, with a number from !patterns."
        pattern = pending[int(number) - 1]
        verdict = "accepted" if command == "/accept" else "rejected"
        sleep_mind.review_pattern(self.memory, self.sleep, pattern, verdict)
        said = f"most {pattern[0]}s {reading_digest._with_article(pattern[1], pattern[2])}"
        return (f"Kept as something I worked out and you approved: {said}." if verdict == "accepted"
                else f"Okay, I won't suggest \"{said}\" again.")

    def _sleep_command(self, command: str, argument: str | None) -> str:
        store = self.sleep
        if command == "/sleep" and (argument or "").strip() == "undo":
            done = sleep_mind.undo(self.memory, store)
            return (f"Undid last night's sleep: {done['rewordings_removed']} rewording(s) removed and "
                    f"{done['restored']} fact(s) brought back as they were.")
        if command == "/sleep":
            last = store.last_night()
            if last is None:
                return "I haven't slept yet; I sleep at the first opening of each day."
            lines = [f"Last sleep ({last['night']}{', undone' if last['undone'] else ''}): " + sleep_mind.morning(last)]
            patterns = store.connection.execute(
                "SELECT category, predicate, object, members, share FROM patterns ORDER BY members DESC LIMIT 5").fetchall()
            if patterns:
                lines.append("Patterns I noticed (for your review, not facts):")
                lines += [f"• most {c}s I know ({round(sh * 100)}% of {m}) {reading_digest._with_article(p, o)}"
                          for c, p, o, m, sh in patterns]
            return "\n".join(lines)
        idea = " ".join((argument or "").lower().split())
        if not idea:
            return f"Use {command.replace('/', '!')} IDEA."
        if command == "/volume":
            volume = store.volume(idea)
            if volume is None:
                return f"I haven't written a volume about {idea} yet (I write one once I know {sleep_mind.VOLUME_MIN_FACTS}+ things)."
            lines = [f"What I know about {idea}:"] + [f"• {f['says']} ({', '.join(reading_digest._source_name(p) for p in f['pages'])})"
                                                      for f in volume["facts"]]
            lines += [f"• I worked out: {w}" for w in volume["worked_out"]]
            if volume.get("disagreement"):
                lines.append(f"• My sources disagree: {volume['disagreement']}")
            if volume["more"]:
                lines.append(f"…and {volume['more']} more.")
            return "\n".join(lines)
        if command == "/cold":
            found = store.cold_matches(idea)
            if not found:
                return f"Nothing about {idea} is in cold storage."
            return "\n".join([f"Set aside about {idea} (!restore {idea} brings it back):"] + [
                f"• {idea} {reading_digest._with_article(f['predicate'], f['object'])} — {f['why']}" for f in found[:10]])
        restored = sleep_mind.restore(self.memory, store, idea)
        return f"Brought back {restored} fact(s) about {idea}." if restored else f"Nothing about {idea} is in cold storage."

    def _verdict(self, argument: str | None) -> str:
        """The owner's verdict on a disagreement: kept as the owner's own statement; both sides stay."""
        parts = (argument or "").split()
        found = reading_digest.disputes(self.memory)
        if len(parts) != 2 or not parts[0].isdigit() or not 1 <= int(parts[0]) <= len(found):
            return "Use !verdict NUMBER LETTER, with the numbers and letters from !disputes."
        dispute = found[int(parts[0]) - 1]
        choice = parts[1].lower()
        if choice == "unsure":
            reading_digest.record_verdict(self.memory, dispute, None)
            return f"Okay, I won't count your view on {dispute['concept']}. Both sides stay as my sources say."
        index = ord(choice) - 97 if len(choice) == 1 and choice.isalpha() else -1
        if not 0 <= index < len(dispute["sides"]):
            return f"Choose a letter from (a) to ({chr(96 + len(dispute['sides']))}), or unsure."
        said = reading_digest.record_verdict(self.memory, dispute, index)
        return (f"Noted: you believe \"{said}\". I keep both sides and their sources; "
                "your view is kept as yours, and !verdict again changes it.")

    def _readings(self) -> str:
        found = reading_digest.sessions(self.journals) if self.journals is not None else []
        if not found:
            return "I have not read anything yet. Give me internet time with: lantern-prime.sh read MINUTES"
        lines = ["My reading sessions (newest first); !unlearn SESSION removes everything one session taught me:"]
        lines += [f"• {s['session']}: {s['pages']} page(s), {s['relations_added'] or 0} thing(s) learned" for s in found[:20]]
        concepts = sum(1 for _ in self.memory._connection.execute(
            "SELECT 1 FROM entities WHERE kind = ?", (reading_digest.CONCEPT_KIND,)))
        lines.append(f"I know about {concepts} concept(s) from reading. Ask me \"what is ...?\"")
        return "\n".join(lines)

    def _unlearn(self, session: str) -> str:
        if self.journals is None or not (self.journals / f"{session}.json").exists():
            raise ValueError(f"no reading session {session}; see !readings")
        forgotten = reading_digest.unlearn_session(self.memory, session)
        self.library = Library(self.library.knowledge, self.library.phrases, reading_digest.known_relations(self.memory))
        return f"I have let go of {forgotten} thing(s) I learned in reading session {session}."

    def _save_ledger(self, ledger: learning.Ledger) -> None:
        if ledger == self.ledger:
            return
        self.ledger = ledger
        if self.ledger_path is None:
            return
        self.ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.ledger_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.ledger_path)

    def _learn(self, command: str, argument: str) -> str:
        at = self._now()
        ledger = self.ledger
        if command in ("/good", "/bad"):
            previous = self._exchanges[-1] if self._exchanges else None
            ledger, message = learning.rate(ledger, previous, self._thread or None, command == "/good", argument, at)
            if self.console is not None:  # a rating teaches the console too, more strongly than a reaction
                self.console.reward(self._console_pending, command == "/good", weight=1.5)
            ledger, notice = learning.auto_evaluate(ledger, at)
            if notice is not None:
                message = f"{message} {notice}"
        elif command == "/learning":
            text = learning.summary(ledger, label=LEARNING_LABEL)
            if self.levels is not None:
                found = instance_personality.proposals(ledger, self.levels)
                text += "\n" + ("\n".join(
                    f"• Proposal: {p['trait']} {p['before']} → {p['after']} from {len(p['evidence'])} ratings. "
                    f"!adopt personality {p['trait']}" for p in found) if found else
                    "• No personality proposals yet (I need 3 voice ratings: !good voice or !bad voice).")
            return text
        elif command in ("/adopt", "/revert") and argument.startswith("personality") and self.levels is not None:
            trait = argument[len("personality"):].strip()
            if trait not in instance_personality.TRAITS:
                return f"Use {command.replace('/', '!')} personality trait, with one of: {', '.join(instance_personality.TRAITS)}."
            operation = instance_personality.adopt if command == "/adopt" else instance_personality.revert
            before = dict(self.levels)
            ledger, self.levels, message = operation(ledger, self.levels, trait, at)
            if self.levels != before:  # an owner adoption or revert: the next personality version
                self.personality.change(self.levels, f"learning:{command[1:]}:{trait}", at)
        else:
            self._require_argument(command, argument)
            if command == "/adopt":
                ledger, message = learning.adopt_strategy(ledger, argument, at, where="on the Workstation")
            else:
                operation = {"/trial": learning.trial, "/stop": learning.stop,
                             "/evaluate": learning.evaluate, "/revert": learning.revert_strategy}[command]
                ledger, message = operation(ledger, argument, at)
        self._save_ledger(ledger)
        return message

    def _words(self, command: str, argument: str) -> str:
        """The words Lantern coined with another Lantern: always readable, and the owner can retire any."""
        from lantern.node_conversation import TalkHost

        host = TalkHost(self.talk_folder, mind=None)
        words = host.memory("", name="Prime").words
        if command == "/retire":
            word = argument.strip().lower()
            return (f"I'll never use \"{word}\" again." if host.retire(word)
                    else f"\"{word}\" isn't one of our words. !words lists them.")
        if not words:
            return "My phone and I haven't coined any words yet."
        lines = ["Words my phone and I made (from our conversations):"]
        lines += [f"• {word} = {meaning}" for word, meaning in words]
        retired = host.retired()
        if retired:
            lines.append("Retired: " + ", ".join(retired))
        lines.append("!retire WORD stops us using one.")
        return "\n".join(lines)

    def _personality(self, command: str, argument: str) -> str:
        """Owner-only: the local chat is the owner's; commands arriving from the phone are refused upstream."""
        at = self._now()
        words = argument.lower().split()
        if command == "/voice":
            if words not in (["on"], ["off"]):
                return "Use !voice on or !voice off."
            return self.personality.set_enabled(words == ["on"], at)
        if command == "/personality" and words[:1] == ["history"]:
            return self.personality.describe_history()
        if command == "/personality" and words[:1] == ["restore"]:
            if len(words) != 2 or not words[1].lstrip("v").isdigit():
                return "Use !personality restore N (see !personality history)."
            self.levels, message = self.personality.restore(int(words[1].lstrip("v")), at)
            return message
        if command == "/personality":
            text = instance_personality.describe(self.levels, self.library.known, label="on the Workstation")
            return text + ("" if self.personality.enabled() else "\n(My voice is off right now: !voice on.)")
        before = dict(self.levels)
        self.levels, message = instance_personality.tone(self.levels, argument.lower(), where="on the Workstation")
        if self.levels != before:
            self.personality.change(self.levels, f"owner:tone:{argument.split()[0].lower()}", at)
        return message

    def _remembered(self) -> list[str]:
        """What the owner asked Lantern to keep, oldest first: their own words and committed claims."""
        notes = [
            str(row["content"])
            for row in self.memory._connection.execute(
                "SELECT f.content FROM episode_facts AS f JOIN episodes AS e ON e.id = f.episode_id "
                "WHERE f.observation_key = 'owner_message' ORDER BY e.created_at, e.id, f.sequence"
            )
        ]
        # Only what the owner said; what Lantern read is knowledge, not something "you told me".
        return notes + [c.object_text for c in self.memory.current_claims() if c.evidence_type == "reported_by_amelie"]

    def _remember_words(self, argument: str) -> str:
        """`!remember` keeps the owner's last message (or the given words) verbatim, as on the phone."""
        words = argument or (self._turns[-1].text if self._turns else "")
        if not words:
            raise ValueError("tell me something first, then say !remember")
        session_id = self.staging.stage_session(
            words,
            [StagedObservation("owner_message", "owner_intent", words, "reported_by_amelie")],
        )
        result = EpisodeConsolidator(self.memory, self.staging).consolidate(session_id)
        return (
            f'I\'ll remember that: "{words}" (episode {result.episode_id}). '
            "It is in my memory exactly as you said it; lantern-memory can forget it."
        )

    @staticmethod
    def _require_argument(command: str, argument: str) -> None:
        if not argument:
            display_command = f"!{command.lstrip('!/')}"
            raise ValueError(f"{display_command} requires an identifier")

    @staticmethod
    def _parts(argument: str, count: int, usage: str) -> list[str]:
        parts = [part.strip() for part in argument.split("|")]
        if len(parts) != count or any(not part for part in parts):
            display_usage = f"!{usage[1:]}" if usage.startswith("/") else usage
            raise ValueError(f"usage: {display_usage}")
        return parts

    def _help(self) -> str:
        talk = (
            "Just talk to me: I reply like the phone does.",
            "!remember — keep your last message in my memory, exactly as you said it",
            "!remember WORDS — keep those words",
            "!good / !bad [REASON] — rate my last reply; I also notice \"haha\" or \"huh?\"",
            "!learning — what I am learning; !trial, !stop, !evaluate, !adopt, !revert STRATEGY",
            "!readings — what I learned from each reading session; !unlearn SESSION undoes one",
            "!disputes — where my sources disagree (I keep both sides)",
            "!verdict NUMBER LETTER — which side of a disagreement you believe (unsure withdraws it)",
            "!sleep — what last night's sleep did (!sleep undo reverses it); !volume IDEA — what I know about an idea",
            "!cold IDEA — what I set aside about an idea; !restore IDEA brings it back",
            "!patterns — patterns I noticed while sleeping; !accept N / !reject N reviews them",
            "!belief IDEA — how sure I am about an idea, and the evidence (never ratings)",
            "!language — the sentence shapes I've learned from my reading and use in my own words",
            "!story [yesterday|DATE] — my life story; !goals — my own goals (!drop N); !think — reflect now",
            "!reflect on|off — whether I think quietly on my own while I'm open",
            "!associations IDEA — what an idea goes with in my reading and our conversations",
            "!similar IDEA — what an idea is like in my idea space, and what usually follows it",
            "!research — what I'm reading about again and why, and what each kind of research has brought me",
            "!sites — the sites I've found that let me read (I go back to them), and what I'll search first",
            "!predict IDEA … — what my predictive core expects next after some ideas",
            "!core — how well my predictive core guesses what comes next (!core off|on, !core restore N)",
            "!feel IDEA — how an idea feels, and to whom (from the words around it)",
            "!growth — how predictable my world is becoming, and where I'm curious; !surprises — what surprised me",
            "!expect IDEA — what I expect to come after an idea",
            "!console — which of my minds I listen to, and how I'm learning that (!console off|on, !console restore N)",
            "!personality — my voice and interests; !personality history; !personality restore N; !tone TRAIT up|down",
            "!voice off / !voice on — switch my personality off and back on",
            "!words — words my phone and I made; !retire WORD stops us using one",
        ) if self.companion else ()
        return "\n".join(
            (
                *talk,
                "Current Gate 2 commands:",
                "!status — inspect memory health and counts",
                "!hive — inspect Prime's local node and causal event journal",
                "!entities — list known entities",
                "!claims — list active claims",
                "!recall TEXT — find active claims or durable episodes by explicit text",
                "!ask QUESTION — inspect relevant evidence and contradictions",
                "!explain CLAIM_ID — show how a claim is known",
                "!schemas — list cognitive schemas",
                "!pending — list uncommitted drafts",
                "!review ID — inspect exactly what a draft means",
                "!entity KIND | NAME — draft a new entity",
                "!remember ENTITY | PREDICATE | STATEMENT | CONFIDENCE",
                "!infer ENTITY | PREDICATE | STATEMENT | CONFIDENCE | BASIS_CLAIM_IDS",
                "!correct CLAIM_ID | STATEMENT | REASON | CONFIDENCE",
                "!commit ID — make a reviewed draft durable",
                "!discard ID — permanently reject an uncommitted draft",
                "!quit — end this local session",
                "Legacy / commands remain accepted for compatibility.",
                (
                    "I never turn what you say into facts on my own; only !remember and reviewed drafts reach memory."
                    if self.companion else
                    "Plain text becomes a pending verbatim note, not an inferred fact."
                ),
            )
        )

    def _status(self) -> str:
        report = self.memory.check_integrity()
        hive_events = self.memory.hive_events()
        payload = {
            "schema_version": self.memory.schema_version,
            "healthy": report.healthy,
            "writable": report.writable,
            "counts": self.memory.record_counts(),
            "pending_drafts": len(self.staging.pending_sessions()),
            "hive": {
                "distributed_authority": False,
                "node_count": len(self.memory.hive_nodes()),
                "event_count": len(hive_events),
                "head_event_id": hive_events[-1].event_id,
            },
        }
        return json.dumps(payload, sort_keys=True, indent=2)

    def _hive(self) -> str:
        before = self.memory.state_digest()
        checkpoint = self.memory.latest_hive_checkpoint()
        current_content_digest = self.memory.content_digest()
        payload = {
            "distributed_authority": False,
            "authority_mode": "owner_controlled_single_host",
            "nodes": [asdict(node) for node in self.memory.hive_nodes()],
            "events": [
                {
                    "event_id": event.event_id,
                    "origin_sequence": event.origin_sequence,
                    "event_type": event.event_type,
                    "lifecycle_state": event.lifecycle_state,
                    "parent_ids": list(event.parent_ids),
                    "created_at": event.created_at,
                    "digest": event.digest,
                }
                for event in self.memory.hive_events()
            ],
            "current_content_digest": current_content_digest,
            "latest_checkpoint_matches": (
                None
                if checkpoint is None
                else checkpoint.payload.get("content_digest")
                == current_content_digest
            ),
            "read_only": True,
        }
        if self.memory.state_digest() != before:
            raise RuntimeError("hive inspection changed authoritative memory")
        return json.dumps(payload, sort_keys=True, indent=2)

    def _claims(self) -> str:
        claims = [asdict(claim) for claim in self.memory.current_claims()]
        return json.dumps(claims, sort_keys=True, indent=2)

    def _entities(self) -> str:
        rows = self.memory._connection.execute(
            "SELECT id, kind, canonical_name, created_at "
            "FROM entities ORDER BY canonical_name, kind, id"
        )
        return json.dumps([dict(row) for row in rows], sort_keys=True, indent=2)

    def _recall(self, query: str) -> str:
        self._require_argument("/recall", query)
        terms = tuple(part.casefold() for part in query.split() if part)
        before = self.memory.state_digest()
        matches: list[dict[str, object]] = []
        represented_episode_ids: set[str] = set()
        for claim in self.memory.current_claims():
            subject = self._entity_name(claim.subject_entity_id)
            searchable = " ".join(
                (subject, claim.predicate, claim.object_text)
            ).casefold()
            if all(term in searchable for term in terms):
                matches.append(self._claim_explanation(claim.id, compact=True))
                if claim.episode_id is not None:
                    represented_episode_ids.add(claim.episode_id)
        matches.extend(
            self._episode_recall_matches(terms, represented_episode_ids)
        )
        payload = {
            "query": query,
            "match_count": len(matches),
            "matches": matches,
            "clarification_needed": not matches,
            "read_only": True,
        }
        if not matches:
            payload["message"] = (
                "I found no active claim or durable episode matching those "
                "explicit terms. Please use different terms or tell me what "
                "subject or fact you mean; I changed nothing."
            )
        if self.memory.state_digest() != before:
            raise RuntimeError("read-only recall changed authoritative memory")
        return json.dumps(payload, sort_keys=True, indent=2)

    def _episode_recall_matches(
        self,
        terms: tuple[str, ...],
        excluded_episode_ids: set[str],
    ) -> list[dict[str, object]]:
        rows = self.memory._connection.execute(
            """
            SELECT e.id AS episode_id, e.summary, e.retention_class,
                   e.created_at, f.observation_key, f.category, f.content,
                   f.evidence_type, f.required, f.sequence
            FROM episodes AS e
            LEFT JOIN episode_facts AS f ON f.episode_id = e.id
            ORDER BY e.created_at, e.id, f.sequence, f.id
            """
        ).fetchall()
        episodes: dict[str, dict[str, object]] = {}
        for row in rows:
            episode_id = str(row["episode_id"])
            episode = episodes.setdefault(
                episode_id,
                {
                    "record_type": "episode",
                    "episode_id": episode_id,
                    "summary": str(row["summary"]),
                    "retention_class": str(row["retention_class"]),
                    "created_at": str(row["created_at"]),
                    "facts": [],
                },
            )
            if row["observation_key"] is not None:
                facts = episode["facts"]
                assert isinstance(facts, list)
                facts.append(
                    {
                        "observation_key": str(row["observation_key"]),
                        "category": str(row["category"]),
                        "content": str(row["content"]),
                        "evidence_type": str(row["evidence_type"]),
                        "required": bool(row["required"]),
                        "sequence": int(row["sequence"]),
                    }
                )

        matches: list[dict[str, object]] = []
        for episode in episodes.values():
            facts = episode.pop("facts")
            if episode["episode_id"] in excluded_episode_ids:
                continue
            assert isinstance(facts, list)
            searchable = " ".join(
                [
                    str(episode["summary"]),
                    *[str(fact["content"]) for fact in facts],
                ]
            ).casefold()
            if not all(term in searchable for term in terms):
                continue
            matched_facts = [
                fact for fact in facts
                if any(term in str(fact["content"]).casefold() for term in terms)
            ]
            episode["matched_facts"] = matched_facts
            matches.append(episode)
        return matches

    def _explain(self, claim_id: str) -> str:
        self._require_argument("/explain", claim_id)
        return json.dumps(
            self._claim_explanation(claim_id, compact=False),
            sort_keys=True,
            indent=2,
        )

    def _ask(self, question: str) -> str:
        """Ground a question in active claims without inferring or writing state."""
        self._require_argument("/ask", question)
        terms = self._meaningful_terms(question)
        before = self.memory.state_digest()
        ranked: list[tuple[int, float, str, object, tuple[str, ...]]] = []
        for claim in self.memory.current_claims():
            subject = self._entity_name(claim.subject_entity_id)
            claim_terms = self._meaningful_terms(
                " ".join((subject, claim.predicate.replace("_", " "), claim.object_text))
            )
            matched = tuple(sorted(set(terms).intersection(claim_terms)))
            if matched:
                ranked.append(
                    (-len(matched), -claim.confidence, claim.id, claim, matched)
                )
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        selected = ranked[:_ASK_RESULT_LIMIT]
        evidence = []
        for negative_count, _, _, claim, matched in selected:
            explanation = self._claim_explanation(claim.id, compact=True)
            explanation["matched_terms"] = list(matched)
            explanation["match_count"] = -negative_count
            evidence.append(explanation)

        conflicts = self._relevant_conflicts([item[3] for item in selected])
        if not terms:
            message = (
                "I could not identify a concrete subject or concept in that question. "
                "Please name what you want to know; I changed nothing."
            )
        elif not evidence:
            message = (
                "I found no active memory relevant to the explicit terms in that "
                "question. Please add context or teach me through a reviewed draft; "
                "I changed nothing."
            )
        elif conflicts:
            message = (
                "I found relevant memory, but it contains conflicting active claims. "
                "I will not choose between them without clarification; I changed nothing."
            )
        else:
            message = (
                "I found grounded memory relevant to the question. These are evidence "
                "records, not a newly inferred answer; I changed nothing."
            )
        payload = {
            "question": question,
            "interpreted_terms": list(terms),
            "evidence": evidence,
            "conflicts": conflicts,
            "clarification_needed": not evidence or bool(conflicts) or not terms,
            "message": message,
            "read_only": True,
        }
        if self.memory.state_digest() != before:
            raise RuntimeError("read-only question handling changed authoritative memory")
        return json.dumps(payload, sort_keys=True, indent=2)

    @staticmethod
    def _meaningful_terms(text: str) -> tuple[str, ...]:
        words = (match.casefold() for match in _WORD_PATTERN.findall(text))
        return tuple(sorted({word for word in words if word not in _QUESTION_WORDS}))

    def _relevant_conflicts(self, claims: list[Claim]) -> list[dict[str, object]]:
        relevant = {claim.id: claim for claim in claims}
        conflicts: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for claim in claims:
            for link in self.memory.links_for_claim(claim.id):
                if link["relation"] != "contradicts":
                    continue
                other_id = (
                    link["to_claim_id"]
                    if link["from_claim_id"] == claim.id
                    else link["from_claim_id"]
                )
                if other_id not in relevant:
                    continue
                pair = tuple(sorted((claim.id, other_id)))
                if pair in seen:
                    continue
                seen.add(pair)
                conflicts.append(
                    {
                        "claim_ids": list(pair),
                        "relation": "contradicts",
                        "reason": link["reason"],
                    }
                )
        conflicts.sort(key=lambda item: tuple(item["claim_ids"]))
        return conflicts

    def _claim_explanation(
        self,
        claim_id: str,
        *,
        compact: bool,
    ) -> dict[str, object]:
        claim = self.memory.get_claim(claim_id)
        payload: dict[str, object] = {
            "claim": asdict(claim),
            "subject": self._entity_name(claim.subject_entity_id),
            "how_known": {
                "evidence_type": claim.evidence_type,
                "confidence": claim.confidence,
                "source": self._source_summary(claim.source_id),
            },
        }
        if not compact:
            payload["episode"] = (
                None
                if claim.episode_id is None
                else self.memory.get_episode(claim.episode_id)
            )
            payload["links"] = self.memory.links_for_claim(claim.id)
            payload["status_history"] = self.memory.status_history(claim.id)
        return payload

    def _entity_name(self, entity_id: str) -> str:
        row = self.memory._connection.execute(
            "SELECT canonical_name FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            raise KeyError(entity_id)
        return str(row["canonical_name"])

    def _source_summary(self, source_id: str | None) -> dict[str, str] | None:
        if source_id is None:
            return None
        row = self.memory._connection.execute(
            "SELECT kind, reference FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return {"kind": str(row["kind"]), "reference": str(row["reference"])}

    def _schemas(self) -> str:
        schemas = [asdict(item) for item in self.memory.list_cognitive_schemas()]
        return json.dumps(schemas, sort_keys=True, indent=2)

    def _pending(self) -> str:
        pending = [
            {
                "id": item.id,
                "created_at": item.created_at,
                "raw_bytes": len((item.raw_text or "").encode("utf-8")),
            }
            for item in self.staging.pending_sessions()
        ]
        return json.dumps(pending, sort_keys=True, indent=2)

    def _review(self, session_id: str) -> str:
        self._require_argument("/review", session_id)
        session = self.staging.get_session(session_id)
        payload = {
            "id": session.id,
            "status": session.status,
            "raw_text": session.raw_text,
            "interpretation": [
                asdict(item) for item in self.staging.observations(session_id)
            ],
        }
        return json.dumps(payload, sort_keys=True, indent=2)

    def _stage_claim(self, argument: str) -> str:
        entity_name, predicate, object_text, confidence_text = self._parts(
            argument,
            4,
            "/remember ENTITY | PREDICATE | STATEMENT | CONFIDENCE",
        )
        confidence = self._confidence(confidence_text)
        self._entity_id(entity_name)
        observations = (
            StagedObservation("mode", "owner_intent", "claim", "reported_by_amelie"),
            StagedObservation("subject", "participant", entity_name, "reported_by_amelie"),
            StagedObservation("predicate", "owner_intent", predicate, "reported_by_amelie"),
            StagedObservation("object", "owner_intent", object_text, "reported_by_amelie"),
            StagedObservation("confidence", "uncertainty", str(confidence), "reported_by_amelie"),
        )
        session_id = self.staging.stage_session(argument, observations)
        return (
            f"Drafted claim {session_id}. I have not learned it yet. "
            f"Use !review {session_id}, then !commit {session_id} or !discard {session_id}."
        )

    def _stage_inference(self, argument: str) -> str:
        entity_name, predicate, object_text, confidence_text, basis_text = self._parts(
            argument,
            5,
            "/infer ENTITY | PREDICATE | STATEMENT | CONFIDENCE | BASIS_CLAIM_IDS",
        )
        confidence = self._confidence(confidence_text)
        self._entity_id(entity_name)
        basis_claim_ids = tuple(
            claim_id.strip() for claim_id in basis_text.split(",") if claim_id.strip()
        )
        if not basis_claim_ids:
            raise ValueError("an inference requires at least one basis claim")
        if len(set(basis_claim_ids)) != len(basis_claim_ids):
            raise ValueError("an inference cannot repeat a basis claim")
        for claim_id in basis_claim_ids:
            self.memory.get_claim(claim_id)
        observations = (
            StagedObservation("mode", "lantern_action", "inference", "inferred"),
            StagedObservation("subject", "participant", entity_name, "remembered"),
            StagedObservation("predicate", "lantern_action", predicate, "inferred"),
            StagedObservation("object", "lesson", object_text, "inferred"),
            StagedObservation(
                "confidence",
                "uncertainty",
                str(confidence),
                "inferred",
            ),
            StagedObservation(
                "basis_claim_ids",
                "prior_belief",
                json.dumps(basis_claim_ids),
                "remembered",
            ),
        )
        session_id = self.staging.stage_session(argument, observations)
        return (
            f"Drafted inference {session_id}. It is explicitly inferred, cites "
            "reviewable basis claims, and is not learned yet. "
            f"Use !review {session_id}, then !commit {session_id} or !discard {session_id}."
        )

    def _stage_entity(self, argument: str) -> str:
        kind, canonical_name = self._parts(
            argument,
            2,
            "/entity KIND | NAME",
        )
        kind = kind.casefold()
        if _ENTITY_KIND_PATTERN.fullmatch(kind) is None:
            raise ValueError(
                "entity kind must start with a letter and contain only "
                "lowercase letters, numbers, underscores, or hyphens"
            )
        if len(canonical_name) > 200 or any(ord(char) < 32 for char in canonical_name):
            raise ValueError("entity name must be printable and at most 200 characters")
        if self._find_entity(kind, canonical_name) is not None:
            raise ValueError(f"entity already exists: {kind} | {canonical_name}")
        observations = (
            StagedObservation("mode", "owner_intent", "entity", "reported_by_amelie"),
            StagedObservation("kind", "owner_intent", kind, "reported_by_amelie"),
            StagedObservation(
                "canonical_name",
                "participant",
                canonical_name,
                "reported_by_amelie",
            ),
        )
        session_id = self.staging.stage_session(argument, observations)
        return (
            f"Drafted entity {session_id}. I have not created it yet. "
            f"Use !review {session_id}, then !commit {session_id} or !discard {session_id}."
        )

    def _stage_correction(self, argument: str) -> str:
        claim_id, object_text, reason, confidence_text = self._parts(
            argument,
            4,
            "/correct CLAIM_ID | STATEMENT | REASON | CONFIDENCE",
        )
        self.memory.get_claim(claim_id)
        confidence = self._confidence(confidence_text)
        observations = (
            StagedObservation("mode", "correction", "correction", "reported_by_amelie"),
            StagedObservation("target", "prior_belief", claim_id, "remembered"),
            StagedObservation("object", "correction", object_text, "reported_by_amelie"),
            StagedObservation("reason", "correction", reason, "reported_by_amelie"),
            StagedObservation("confidence", "uncertainty", str(confidence), "reported_by_amelie"),
        )
        session_id = self.staging.stage_session(argument, observations)
        return (
            f"Drafted correction {session_id}; it is not learned and the original "
            "claim is unchanged. "
            f"Use !review {session_id}, then !commit {session_id} or !discard {session_id}."
        )

    @staticmethod
    def _confidence(value: str) -> float:
        confidence = float(value)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        return confidence

    def _entity_id(self, canonical_name: str) -> str:
        rows = list(
            self.memory._connection.execute(
                "SELECT id FROM entities WHERE canonical_name = ?",
                (canonical_name,),
            )
        )
        if len(rows) != 1:
            raise ValueError(f"expected exactly one known entity named {canonical_name!r}")
        return str(rows[0]["id"])

    def _find_entity(self, kind: str, canonical_name: str) -> str | None:
        row = self.memory._connection.execute(
            "SELECT id FROM entities WHERE kind = ? AND canonical_name = ?",
            (kind, canonical_name),
        ).fetchone()
        return None if row is None else str(row["id"])

    def _commit(self, session_id: str) -> str:
        self._require_argument("/commit", session_id)
        session = self.staging.get_session(session_id)
        if session.status != "pending":
            return f"Draft {session_id} was already committed as episode {session.episode_id}."
        observations = {item.key: item.content for item in self.staging.observations(session_id)}
        mode = observations.get("mode", "note")
        if mode == "entity":
            return self._commit_entity(session_id, observations)
        if mode == "claim":
            return self._commit_claim(session_id, observations)
        if mode == "inference":
            return self._commit_inference(session_id, observations)
        if mode == "correction":
            return self._commit_correction(session_id, observations)
        result = EpisodeConsolidator(self.memory, self.staging).consolidate(session_id)
        return (
            f"Committed note as episode {result.episode_id}. Raw text expired after "
            "fidelity-preserving consolidation."
        )

    def _commit_entity(self, session_id: str, values: dict[str, str]) -> str:
        existing = self._find_entity(values["kind"], values["canonical_name"])
        prior_record = self.memory.consolidation_record(session_id)
        if existing is not None and prior_record is None:
            raise ValueError(
                f"entity already exists: {values['kind']} | {values['canonical_name']}"
            )
        episode_id = self._prepare_episode(session_id)
        entity_id = existing
        if entity_id is None:
            entity_id = self.memory.add_entity(
                values["kind"],
                values["canonical_name"],
            )
        self.staging.mark_consolidated(session_id, episode_id)
        return (
            f"Committed entity {entity_id} as {values['kind']} "
            f"{values['canonical_name']!r} with reviewed episode {episode_id}."
        )

    def _commit_claim(self, session_id: str, values: dict[str, str]) -> str:
        entity_id = self._entity_id(values["subject"])
        prior = self.memory.current_claims(
            subject_entity_id=entity_id,
            predicate=values["predicate"],
        )
        episode_id = self._prepare_episode(session_id)
        existing = self._existing_interface_claim(session_id)
        if existing is not None:
            self.staging.mark_consolidated(session_id, episode_id)
            return f"Committed claim {existing} with provenance."
        source_id = self.memory.add_source("owner_interface", f"interface:{session_id}")
        claim_id = self.memory.add_claim(
            entity_id,
            values["predicate"],
            values["object"],
            evidence_type="reported_by_amelie",
            confidence=float(values["confidence"]),
            source_id=source_id,
            episode_id=episode_id,
        )
        contradictions = [item for item in prior if item.object_text != values["object"]]
        for old in contradictions:
            self.memory.link_claims(
                claim_id,
                old.id,
                relation="contradicts",
                reason="Same subject and predicate have different reported objects.",
            )
        self.staging.mark_consolidated(session_id, episode_id)
        suffix = (
            f" I found {len(contradictions)} conflicting active claim(s) and linked them; "
            "I did not silently choose a winner."
            if contradictions else ""
        )
        return f"Committed claim {claim_id} with provenance.{suffix}"

    def _commit_inference(self, session_id: str, values: dict[str, str]) -> str:
        entity_id = self._entity_id(values["subject"])
        basis_claim_ids = tuple(json.loads(values["basis_claim_ids"]))
        if not basis_claim_ids:
            raise ValueError("an inference requires at least one basis claim")
        for basis_claim_id in basis_claim_ids:
            self.memory.get_claim(basis_claim_id)
        prior = self.memory.current_claims(
            subject_entity_id=entity_id,
            predicate=values["predicate"],
        )
        episode_id = self._prepare_episode(session_id)
        existing = self._existing_interface_claim(session_id)
        if existing is not None:
            self.staging.mark_consolidated(session_id, episode_id)
            return f"Committed inferred claim {existing} with explicit derivation."
        source_id = self.memory.add_source(
            "lantern_inference",
            f"interface:{session_id}",
        )
        claim_id = self.memory.add_claim(
            entity_id,
            values["predicate"],
            values["object"],
            evidence_type="inferred",
            confidence=float(values["confidence"]),
            source_id=source_id,
            episode_id=episode_id,
        )
        for basis_claim_id in basis_claim_ids:
            self.memory.link_claims(
                claim_id,
                basis_claim_id,
                relation="derived_from",
                reason="Owner-reviewed inference explicitly cites this basis claim.",
            )
        contradictions = [item for item in prior if item.object_text != values["object"]]
        for old in contradictions:
            self.memory.link_claims(
                claim_id,
                old.id,
                relation="contradicts",
                reason="Same subject and predicate have different inferred objects.",
            )
        self.staging.mark_consolidated(session_id, episode_id)
        suffix = (
            f" I found {len(contradictions)} conflicting active claim(s) and linked "
            "them; I did not silently choose a winner."
            if contradictions else ""
        )
        return (
            f"Committed inferred claim {claim_id} with explicit derivation from "
            f"{len(basis_claim_ids)} basis claim(s).{suffix}"
        )

    def _commit_correction(self, session_id: str, values: dict[str, str]) -> str:
        old = self.memory.get_claim(values["target"])
        episode_id = self._prepare_episode(session_id)
        existing = self._existing_interface_claim(session_id)
        if existing is not None:
            self.staging.mark_consolidated(session_id, episode_id)
            return (
                f"Committed correction as claim {existing}. Original claim {old.id} "
                "remains in history as superseded."
            )
        source_id = self.memory.add_source("owner_correction", f"interface:{session_id}")
        claim_id = self.memory.correct_claim(
            old.id,
            object_text=values["object"],
            reason=values["reason"],
            actor="Amelie",
            evidence_type="reported_by_amelie",
            confidence=float(values["confidence"]),
            source_id=source_id,
            episode_id=episode_id,
        )
        self.staging.mark_consolidated(session_id, episode_id)
        return (
            f"Committed correction as claim {claim_id}. Original claim {old.id} "
            "remains in history as superseded."
        )

    def _prepare_episode(self, session_id: str) -> str:
        session = self.staging.get_session(session_id)
        observations = self.staging.observations(session_id)
        summary = EpisodeConsolidator.render_summary(observations)
        facts = [
            {
                "observation_key": item.key,
                "category": item.category,
                "content": item.content,
                "evidence_type": item.evidence_type,
                "required": item.required,
            }
            for item in observations
        ]
        episode_id, _ = self.memory.add_consolidated_episode(
            session_id,
            session.source_digest,
            summary,
            facts,
        )
        return episode_id

    def _existing_interface_claim(self, session_id: str) -> str | None:
        row = self.memory._connection.execute(
            """
            SELECT c.id
            FROM claims AS c
            JOIN sources AS s ON s.id = c.source_id
            WHERE s.reference = ?
            ORDER BY c.created_at, c.id
            LIMIT 1
            """,
            (f"interface:{session_id}",),
        ).fetchone()
        return None if row is None else str(row["id"])


def minds_observer(*minds) -> Callable[[str, str], None]:
    """One observer for reading that feeds every mind given (None entries are skipped)."""
    present = [m for m in minds if m is not None]

    def observe(text: str, community: str = "reading") -> None:
        for mind in present:
            mind.observe(text, community)
    return observe


_TERMINAL_COMMAND = re.compile(r"^(?:sudo |bash |sh |cd |git |ls |\./|/opt/|lantern-[a-z]+ |python3? |[A-Z]=\S+$|\$)")
_LEARNING_COMMANDS = frozenset({"/good", "/bad", "/learning", "/trial", "/stop", "/evaluate", "/adopt", "/revert"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lantern's tablet-local text interface")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--staging", default=str(DEFAULT_STAGING))
    parser.add_argument(
        "--ledger",
        help="Prime's conversation-learning ledger (phone format); default: beside the memory database",
    )
    parser.add_argument(
        "--reading",
        help="reading journals (lantern-explore workspace journals/) to build the library from",
    )
    parser.add_argument(
        "--conversations",
        help="where each day's conversation is kept (default: beside the memory database)",
    )
    parser.add_argument(
        "--personality",
        help="this Lantern's own personality levels (instance state; default: none, plain voice)",
    )
    parser.add_argument(
        "--digest-only",
        action="store_true",
        help="interpret new reading journals into memory, delete their raw text, print a summary and exit",
    )
    parser.add_argument(
        "--notes-only",
        action="store_true",
        help="Gate 2 behaviour: plain text becomes a pending note instead of a conversation reply",
    )
    return parser


def run_interface(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> int:
    args = _parser().parse_args(argv)
    with MemoryStore(args.database) as memory, StagingStore(args.staging) as staging:
        ledger = Path(args.ledger) if args.ledger else Path(args.database).parent / "companion-ledger.json"
        require_vault_open(ledger)
        library = EMPTY_LIBRARY
        journals = Path(args.reading) if args.reading else None
        learned = 0
        associations = feelings = expectations = language = None
        if journals is not None:
            require_vault_open(journals)
            # Read -> interpret -> schema -> delete: new journals become concept claims, raw text goes.
            associations = (None if args.notes_only else
                            association_mind.AssociationStore(Path(args.database).parent / "associations.sqlite3"))
            feelings = (None if args.notes_only else connotation_mind.ConnotationStore(
                Path(args.database).parent / "connotation.sqlite3", words=Path(args.database).parent / "connotation-words.json"))
            expectations = (None if args.notes_only else
                            expectation_mind.ExpectationStore(Path(args.database).parent / "expectations.sqlite3"))
            language = (None if args.notes_only else
                        language_mind.LanguageStore(Path(args.database).parent / "language.sqlite3"))
            results = reading_digest.digest_all(memory, journals, observe=None if associations is None else
                                                minds_observer(associations, feelings, expectations, language))
            learned = sum(int(r.get("relations_added", 0)) for r in results)
            tidied = reading_digest.tidy(memory)
            if args.digest_only:
                print(json.dumps({"sessions": results, "relations_added": learned, "tidied": tidied},
                                 indent=2, sort_keys=True), file=stdout)
                return 0
            library = Library((), reading_digest.load_phrasebook(journals.parent / reading_digest.PHRASEBOOK),
                              reading_digest.known_relations(memory))
        conversations = Path(args.conversations) if args.conversations else Path(args.database).parent / "conversations"
        require_vault_open(conversations)
        interface = LocalTextInterface(
            memory, staging, companion=not args.notes_only, ledger_path=None if args.notes_only else ledger,
            library=library, journals=journals, conversations=None if args.notes_only else conversations,
            personality_path=None if args.notes_only or not args.personality else Path(args.personality),
            sleep_path=None if args.notes_only or journals is None else Path(args.database).parent / "sleep.sqlite3",
            associations=associations, feelings=feelings, expectations=expectations, language=language,
            console_path=None if args.notes_only or not args.personality else Path(args.personality).parent / "console.json",
            self_path=None if args.notes_only else Path(args.database).parent / "self.sqlite3",
        )
        if learned:
            interface.note_reading(0, learned)
        welcome = interface.welcome()
        morning = interface.wake()
        if morning:
            welcome += f" {morning}"
        if learned:
            welcome += f" (I just took in {learned} new thing(s) from my reading.)"
        if journals is not None and tidied:
            welcome += f" (I tidied away {tidied} thing(s) that came from page menus or were cut off.)"
        print(f"Lantern> {welcome}", file=stdout)
        while True:
            print("Amelie> ", end="", file=stdout, flush=True)
            line = stdin.readline()
            if line == "":
                print("\nLantern> Input closed; ending local session.", file=stdout)
                return 0
            try:
                reply = interface.handle(line)
            except (KeyError, ValueError, PermissionError) as error:
                print(f"Lantern> I changed nothing: {error}", file=stdout)
                continue
            print(f"Lantern> {reply.text}", file=stdout)
            if reply.exit_requested:
                return 0


def main() -> None:
    raise SystemExit(run_interface())


if __name__ == "__main__":
    main()
