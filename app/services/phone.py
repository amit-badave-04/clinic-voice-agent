"""Phone normalization to E.164. Callers may speak digits ('98765 43210');
Retell sends '+14155551234'. Bare 10-digit numbers are assumed Indian (+91)."""
import re

_E164 = re.compile(r"\+\d{8,15}")


def normalize_phone(value: str | None) -> str:
    """Best-effort E.164 normalization; returns "" for anything that cannot be
    made into a valid-looking number (garbage in must not become identity)."""
    if not value:
        return ""
    digits = re.sub(r"[^\d+]", "", value.strip())
    if not digits:
        return ""
    if digits.startswith("+"):
        result = digits
    elif len(digits) == 10:
        result = f"+91{digits}"
    elif len(digits) == 11 and digits.startswith("0"):
        result = f"+91{digits[1:]}"
    elif len(digits) == 12 and digits.startswith("91"):
        result = f"+{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        result = f"+{digits}"
    else:
        result = f"+{digits}"
    # Final gate: a stray '+' mid-string or absurd lengths must yield "",
    # never a malformed identity like "+9198+76...".
    return result if _E164.fullmatch(result) else ""
