"""Load an instance's accepted personality into a session and voice replies.

The voice layer only adds framing around a reply chosen elsewhere: it never
removes, reorders, or changes the reply's content, and every applied rule is
attributed to the trait and level that triggered it. Evidence and uncertainty
replies receive no humor or playfulness. When the accepted personality is
missing, corrupt, disabled, or belongs to another instance, the session uses a
neutral voice and says why; it never invents a personality.
"""

from __future__ import annotations

from dataclasses import dataclass

from lantern.durable_identity_storage import DurablePersonalityStore
from lantern.instance_config import InstanceConfig
from lantern.personality_profile import PersonalityProfile

SERIOUS_ACTS = frozenset({
    "reviewed_evidence_context", "grounded_question", "milestone_recommendation",
})


@dataclass(frozen=True, slots=True)
class VoiceRule:
    rule_id: str
    trait: str
    minimum: int
    acts: frozenset[str]
    position: str  # "opener" or "closer"
    text: str


# Ordered; at most one opener and one closer apply to a reply.
VOICE_RULES = (
    VoiceRule(
        "playful-greeting", "playfulness", 4, frozenset({"greeting"}),
        "opener", "Okay, I'm here and ready to make something good.",
    ),
    VoiceRule(
        "warm-greeting", "warmth", 4, frozenset({"greeting"}),
        "opener", "It's really good to see you.",
    ),
    VoiceRule(
        "warm-planning", "warmth", 4, frozenset({"planning_intent"}),
        "opener", "I like where this is heading.",
    ),
    VoiceRule(
        "warm-acknowledgement", "warmth", 4, frozenset({"reflective_acknowledgement"}),
        "opener", "Thank you for sharing that with me.",
    ),
    VoiceRule(
        "curious-question", "curiosity", 4, frozenset({"grounded_question"}),
        "opener", "I want to understand this properly.",
    ),
    VoiceRule(
        "direct-correction-invite", "directness", 4, frozenset({"grounded_question"}),
        "closer", "If my reading of the record is wrong, say so plainly and I'll change it.",
    ),
    VoiceRule(
        "detailed-orientation", "explanation_detail", 4,
        frozenset({"project_orientation", "capability_orientation"}),
        "closer", "Use !help to see every command and what it changes.",
    ),
    VoiceRule(
        "social-continuation", "social_energy", 4,
        frozenset({"planning_intent", "reflective_acknowledgement", "project_orientation"}),
        "closer", "I'm glad to keep going with you whenever you're ready.",
    ),
)


@dataclass(frozen=True, slots=True)
class SessionPersonality:
    mode: str  # "accepted" or "neutral_fallback"
    instance_id: str | None
    profile: PersonalityProfile | None
    reason: str

    @property
    def profile_sha256(self) -> str | None:
        return None if self.profile is None else self.profile.profile_sha256

    def levels(self) -> dict[str, int]:
        if self.profile is None:
            return {}
        return {trait.name: trait.level for trait in self.profile.traits}

    def summary(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "mode": self.mode,
            "profile_sha256": self.profile_sha256,
            "profile_version": None if self.profile is None else self.profile.version,
            "reason": self.reason,
            "traits": self.levels(),
        }


def neutral(reason: str, instance_id: str | None = None) -> SessionPersonality:
    return SessionPersonality("neutral_fallback", instance_id, None, reason)


def load_session_personality(config: InstanceConfig | None) -> SessionPersonality:
    """Return the accepted profile, or a neutral fallback that names the reason."""
    if config is None:
        return neutral("no instance is configured")
    store = DurablePersonalityStore(
        config.personality_store,
        instance_id=config.instance_id,
        owner_id=config.owner_id,
    )
    try:
        registry = store.load()
    except FileNotFoundError:
        return neutral("no accepted personality has been stored", config.instance_id)
    except (OSError, ValueError) as error:
        return neutral(f"stored personality was refused: {error}", config.instance_id)
    active = registry.active
    if active is None:
        return neutral("no accepted personality is active", config.instance_id)
    if active.status != "accepted":
        return neutral(f"active personality is {active.status}", config.instance_id)
    return SessionPersonality(
        "accepted", config.instance_id, active,
        f"accepted profile version {active.version}",
    )


def apply_voice(
    text: str, dialogue_act: str, personality: SessionPersonality,
    *, adaptation: dict[str, dict[str, object]] | None = None,
) -> tuple[str, dict[str, object]]:
    """Frame a reply in the session's voice and explain exactly why.

    `adaptation` holds this person's bounded tone offsets; rules compare the
    stable level plus offset, and attribution shows both.
    """
    stable = personality.levels()
    offsets = {
        trait: float(value["offset"])
        for trait, value in (adaptation or {}).items() if trait in stable
    }
    levels = {trait: stable[trait] + offsets.get(trait, 0.0) for trait in stable}
    applied: list[dict[str, object]] = []
    opener = closer = None
    for rule in VOICE_RULES:
        if dialogue_act not in rule.acts or levels.get(rule.trait, -1) < rule.minimum:
            continue
        if dialogue_act in SERIOUS_ACTS and rule.trait in {"humor", "playfulness"}:
            continue
        if rule.position == "opener" and opener is None:
            opener = rule
        elif rule.position == "closer" and closer is None:
            closer = rule
        else:
            continue
        applied.append({
            "level": levels[rule.trait],
            "stable_level": stable[rule.trait],
            "tone_offset": offsets.get(rule.trait, 0.0),
            "minimum": rule.minimum,
            "position": rule.position,
            "rule_id": rule.rule_id,
            "trait": rule.trait,
        })
    voiced = " ".join(part for part in (
        None if opener is None else opener.text,
        text,
        None if closer is None else closer.text,
    ) if part)
    used = {item["trait"] for item in applied}
    return voiced, {
        **personality.summary(),
        "applied_rules": applied,
        "tone_adaptation": adaptation or {},
        "traits_without_effect": sorted(set(levels) - used),
        "content_unchanged": True,
        "dialogue_act": dialogue_act,
    }
