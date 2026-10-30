"""The quantity skill (owner-approved, 2026-10-29): amounts with units, worked out and explained.

The regression spec is `seed-materials/diagnostics/capacity-context.md`: asked "A bottle can hold 750 millilitres
and currently contains 250 millilitres. How much capacity remains?", the phone retrieved a definition, mixed in
mass-per-year material and never calculated. This skill answers such a question first, from the owner's own
numbers, and says how:

- **Quantities**: a number (digits, "1,000", "0.75", or a small number word) followed by a unit of volume, mass or
  duration. A unit followed by "per" or "/" is a rate, never an amount of room.
- **Roles**: the words before a quantity in its clause say whether it is the capacity ("can hold", "capacity",
  "fits", "up to") or the current amount ("contains", "currently", "already", "filled with", "has"); "currently
  holds" is the current amount.
- **Remaining capacity** (the seed's functional model `function:seed.remaining-capacity`): capacity minus current
  amount, after converting to one unit, in the unit asked for or else the capacity's unit. An overfilled
  container is flagged, never clamped to zero; different kinds (a mass and a volume), a rate, a missing value or
  two capacities are said plainly instead of guessed.
- **Conversion**: "how many millilitres are in 2 litres".

Exact decimal arithmetic (rounded half-up to three places only when a division does not end). Nothing here reads
or writes memory: the numbers are the owner's, and the reply says so. Pure and shared with the phone
(`PhoneQuantity`, `quantity-vectors-v1.json`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
import re

# unit word -> (kind, symbol, size in the kind's base unit: millilitre, gram, second)
UNITS: dict[str, tuple[str, str, str]] = {}
for _words, _kind, _symbol, _size in (
        (("ml", "millilitre", "millilitres", "milliliter", "milliliters"), "volume", "mL", "1"),
        (("cl", "centilitre", "centilitres", "centiliter", "centiliters"), "volume", "cL", "10"),
        (("dl", "decilitre", "decilitres", "deciliter", "deciliters"), "volume", "dL", "100"),
        (("l", "litre", "litres", "liter", "liters"), "volume", "L", "1000"),
        (("mg", "milligram", "milligrams"), "mass", "mg", "0.001"),
        (("g", "gram", "grams"), "mass", "g", "1"),
        (("kg", "kilogram", "kilograms", "kilo", "kilos"), "mass", "kg", "1000"),
        (("tonne", "tonnes"), "mass", "t", "1000000"),
        (("s", "sec", "secs", "second", "seconds"), "duration", "s", "1"),
        (("min", "mins", "minute", "minutes"), "duration", "min", "60"),
        (("h", "hr", "hrs", "hour", "hours"), "duration", "h", "3600")):
    for _word in _words:
        UNITS[_word] = (_kind, _symbol, _size)
NUMBER_WORDS = {"a": "1", "an": "1", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "half": "0.5"}
KIND_NAMES = {"volume": "a volume", "mass": "a mass", "duration": "a length of time"}
BOUNDARIES = frozenset({".", ";", "?", "!", ",", "and", "but", "while", "so"})
NOW_WORDS = frozenset({"currently", "already", "now", "filled", "contains", "contain", "containing", "has", "have",
                       "holding", "there", "inside"})
CAPACITY_WORDS = frozenset({"hold", "holds", "capacity", "fits", "fit", "up", "maximum", "max", "can", "could", "takes",
                            "size", "capable"})
STRONG_NOW = frozenset({"currently", "already", "now", "filled"})
REMAINING_WORDS = frozenset({"remain", "remains", "remaining", "left", "more", "room", "space", "spare", "free", "still"})
PERIODS = frozenset({"year", "day", "hour", "minute", "second", "week", "month"})
# ASCII digits and only the punctuation the skill reads, so Prime and the phone split text identically
_TOKEN = re.compile(r"[0-9]+(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?|[a-z]+|[.;?!,/]")
PLACES = Decimal("0.001")
MAX_DIGITS = 12


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    unit: str  # the unit word as written, lowercased
    index: int  # token position of the number
    role: str  # "capacity", "current" or ""
    per: str  # for a rate, what it is per ("per year"); "" for an amount

    @property
    def rate(self) -> bool:
        return bool(self.per)

    @property
    def kind(self) -> str:
        return UNITS[self.unit][0]

    @property
    def symbol(self) -> str:
        return UNITS[self.unit][1]

    def base(self) -> Decimal:
        return self.value * Decimal(UNITS[self.unit][2])

    def said(self) -> str:
        return f"{number(self.value)} {self.symbol}"


def number(value: Decimal) -> str:
    """A plain decimal: no exponent, no trailing zeros."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("’", "'"))


def _role(words: list[str], index: int) -> str:
    """Capacity or current amount, by the words before the number in its clause (at most eight)."""
    before: list[str] = []
    for word in reversed(words[max(0, index - 8):index]):
        if word in BOUNDARIES:
            break
        before.append(word)
    if any(w in STRONG_NOW for w in before):
        return "current"
    if any(w in CAPACITY_WORDS for w in before):
        return "capacity"
    if any(w in NOW_WORDS for w in before):
        return "current"
    return ""


def quantities(text: str) -> list[Quantity]:
    words = tokens(text)
    found = []
    for i, word in enumerate(words[:-1]):
        unit = words[i + 1]
        if unit not in UNITS:
            continue
        if word[0].isdigit():
            if sum(c.isdigit() for c in word) > MAX_DIGITS:
                continue  # beyond any container; and Prime and the phone multiply such numbers differently
            value = Decimal(word.replace(",", ""))
        elif word in NUMBER_WORDS and len(unit) > 2:  # "two litres", never "a l" or "a g"
            value = Decimal(NUMBER_WORDS[word])
            if word in ("a", "an") and i > 0 and words[i - 1] == "half":
                value = Decimal("0.5")  # "half a litre"
        else:
            continue
        per = ""
        if i + 3 < len(words) and words[i + 2] in ("per", "/", "a", "an", "each", "every") and words[i + 3][0].isalpha():
            if words[i + 2] in ("per", "/") or words[i + 3] in PERIODS:
                per = f"per {words[i + 3]}"
        found.append(Quantity(value, unit, i, _role(words, i), per))
    return found


def _asked_unit(words: list[str]) -> str:
    """The unit named right after "how much" / "how many", if any ("how many millilitres ...")."""
    for i in range(len(words) - 2):
        if words[i] == "how" and words[i + 1] in ("much", "many") and words[i + 2] in UNITS:
            return words[i + 2]
    return ""


def _in(base: Decimal, unit: str) -> Decimal:
    value = base / Decimal(UNITS[unit][2])
    return value if value == value.quantize(PLACES) else value.quantize(PLACES, rounding=ROUND_HALF_UP)


def answer(text: str) -> str | None:
    """A worked answer to a quantity question, or None when the text is not one (normal chat goes on)."""
    words = tokens(text)
    asking = any(words[i] == "how" and words[i + 1] in ("much", "many") for i in range(len(words) - 1)) or \
        ("?" in words and any(w in ("what", "whats") for w in words))
    found = quantities(text)
    if not asking or not found:
        return None
    asked = _asked_unit(words)
    if any(w in REMAINING_WORDS for w in words):
        return _remaining(found, asked)
    if asked and len(found) == 1:
        return _convert(found[0], asked)
    return None


def _remaining(found: list[Quantity], asked: str) -> str:
    rates = [q for q in found if q.rate]
    if rates:
        return (f"{rates[0].said()} {rates[0].per} is a rate, not an amount, so I can't take it from a capacity. "
                "Tell me how much it can hold and how much is in it now.")
    unassigned = [q for q in found if not q.role]
    if len(found) == 2 and len(unassigned) == 1:
        other = "current" if found[0 if found[0] is not unassigned[0] else 1].role == "capacity" else "capacity"
        found = [q if q.role else Quantity(q.value, q.unit, q.index, other, q.per) for q in found]
    capacity = [q for q in found if q.role == "capacity"]
    current = [q for q in found if q.role == "current"]
    if len(capacity) > 1 or len(current) > 1 or len(found) > 2 or (len(found) == 2 and not (capacity and current)):
        return ("I found " + ", ".join(q.said() for q in found) + ", and I can't tell which is how much it can hold "
                "and which is how much is in it now. Could you say it as: it can hold … and it has … in it?")
    if not capacity or not current:
        missing = "how much it can hold" if not capacity else "how much is in it now"
        return f"I have {found[0].said()}, but to work out what remains I also need {missing}."
    cap, cur = capacity[0], current[0]
    if cap.kind != cur.kind:
        return (f"{cap.said()} is {KIND_NAMES[cap.kind]} and {cur.said()} is {KIND_NAMES[cur.kind]}, so I can't take "
                "one from the other without knowing how they relate (a density, for example).")
    unit = asked if asked and UNITS[asked][0] == cap.kind else cap.unit
    symbol = UNITS[unit][1]
    steps = []
    for q in (cap, cur):
        if q.symbol != symbol:
            steps.append(f"{q.said()} is {number(_in(q.base(), unit))} {symbol}")
    left = cap.base() - cur.base()
    said_cap, said_cur = f"{number(_in(cap.base(), unit))} {symbol}", f"{number(_in(cur.base(), unit))} {symbol}"
    working = ("; ".join(steps) + "; " if steps else "") + f"{said_cap} − {said_cur} = {number(_in(left, unit))} {symbol}"
    if left < 0:
        return (f"It doesn't add up: {cur.said()} is in it but it can only hold {cap.said()}, which is "
                f"{number(_in(-left, unit))} {symbol} more than fits ({working}). Either it's overfilled or the two "
                "numbers are from different moments; I won't call that zero.")
    if left == 0:
        return f"None: it's full. ({working}.)"
    return (f"{number(_in(left, unit))} {symbol} remains. It can hold {cap.said()} and already has {cur.said()} in it: "
            f"{working}. That assumes its usable capacity stays the same and nothing is added or leaks out. "
            "(Worked out from the numbers you gave me.)")


def _convert(quantity: Quantity, asked: str) -> str:
    if quantity.rate:
        return f"{quantity.said()} {quantity.per} is a rate, so I'd need a time span to turn it into an amount."
    if UNITS[asked][0] != quantity.kind:
        return (f"{quantity.said()} is {KIND_NAMES[quantity.kind]} and {UNITS[asked][1]} measures "
                f"{KIND_NAMES[UNITS[asked][0]]}, so one can't be turned into the other without more to go on.")
    source, target = Decimal(UNITS[quantity.unit][2]), Decimal(UNITS[asked][2])
    ratio = (f"1 {quantity.symbol} = {number(_in(source, asked))} {UNITS[asked][1]}" if source >= target
             else f"{number(_in(target, quantity.unit))} {quantity.symbol} = 1 {UNITS[asked][1]}")
    return f"{quantity.said()} is {number(_in(quantity.base(), asked))} {UNITS[asked][1]} ({ratio})."
