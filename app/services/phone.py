"""Phone normalization to E.164. Callers may speak digits ('98765 43210');
Retell sends '+14155551234'. Bare 10-digit numbers are assumed Indian (+91)."""
import re


def normalize_phone(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"[^\d+]", "", value.strip())
    if not digits:
        return ""
    if digits.startswith("+"):
        return digits
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"
