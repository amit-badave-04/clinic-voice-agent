"""Patient-name integrity gate.

Live calls showed the streaming ASR mangling Indian names badly enough that a
caller was nearly booked as "Three Watch Toshniwal", and an earlier booking
landed in the PMS in Devanagari. The prompt asks the LLM to transliterate and
read names back, but prompt adherence is probabilistic — this module makes the
guarantees deterministic at the write boundary:

  1. normalize_for_records(): any Devanagari in a name is transliterated to
     readable Latin before it can reach the DB or Cliniko.
  2. is_plausible(): obvious non-name strings (number words, app/object words,
     digits) are rejected so the agent re-asks instead of booking garbage.
  3. roster_suggestion(): a near-match against patients already on this phone
     number is surfaced for confirmation instead of silently creating a
     spelling-variant duplicate.

Romanization is best-effort readable ASCII (IAST + a readability fold), not
scholarly: the caller read-back remains the semantic check; this layer only
guarantees script and sanity.
"""
import re
import unicodedata

from indic_transliteration import sanscript
from rapidfuzz.distance import JaroWinkler

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# IAST output folded to plain readable ASCII (ś→sh before the generic
# diacritic strip so "श" doesn't collapse to bare "s").
_IAST_FOLD = [
    ("ś", "sh"), ("ṣ", "sh"), ("c", "ch"), ("ṭ", "t"), ("ḍ", "d"),
    ("ṇ", "n"), ("ñ", "n"), ("ṅ", "n"), ("ṃ", "m"), ("ḥ", "h"), ("ṛ", "ri"),
]

# Tokens that mark a "name" as a probable mis-hearing. Deliberately small and
# unambiguous — a false reject only costs one extra confirmation question.
_NON_NAME_TOKENS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "watch", "clock", "phone", "mobile", "whatsapp", "google", "facebook",
    "instagram", "youtube", "internet", "hello", "hi", "ok", "okay", "yes", "no",
    "haan", "nahi", "please", "thanks", "thank", "test", "testing", "appointment",
    "booking", "cancel", "doctor", "branch", "clinic", "number", "name",
}

_MATCH_THRESHOLD = 0.85


def contains_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def _romanize_word(word: str) -> str:
    latin = sanscript.transliterate(word, sanscript.DEVANAGARI, sanscript.IAST)
    for src, dst in _IAST_FOLD:
        latin = latin.replace(src, dst)
    # Strip any remaining diacritics (ā→a, ī→i, …) down to ASCII.
    latin = unicodedata.normalize("NFD", latin)
    latin = "".join(ch for ch in latin if not unicodedata.combining(ch))
    return latin.capitalize()


def normalize_for_records(name: str) -> str:
    """Collapse whitespace; transliterate only the Devanagari words, leaving
    Latin words exactly as the caller's confirmed spelling."""
    words = (name or "").split()
    return " ".join(_romanize_word(w) if contains_devanagari(w) else w for w in words)


def is_plausible(name: str) -> bool:
    tokens = re.split(r"[\s.-]+", (name or "").strip().lower())
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return False
    for token in tokens:
        if any(ch.isdigit() for ch in token):
            return False
        if token in _NON_NAME_TOKENS:
            return False
    return True


def roster_suggestion(name: str, roster: list[str]) -> str | None:
    """Closest existing patient name on this phone number, when it is close
    enough to be the same person misheard — but not an exact match."""
    target = (name or "").casefold()
    best_name, best_score = None, 0.0
    for candidate in roster:
        if candidate.casefold() == target:
            return None  # exact match: nothing to suggest
        score = JaroWinkler.normalized_similarity(target, candidate.casefold())
        if score > best_score:
            best_name, best_score = candidate, score
    return best_name if best_score >= _MATCH_THRESHOLD else None
