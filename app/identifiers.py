"""Cleaning and checking of identifiers: GSTIN, invoice numbers, PO numbers, company names.

Vendors write the same number in many ways ("INV/0457", "inv-457", "INV 457").
Every comparison in the pipeline goes through these functions, so two spellings
of one identifier are treated as the same thing.
"""

import re
from difflib import SequenceMatcher

GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

# Indian GST state codes used in this demo (subset).
STATE_NAMES = {
    "24": "Gujarat",
    "27": "Maharashtra",
    "29": "Karnataka",
    "33": "Tamil Nadu",
    "07": "Delhi",
}

LEGAL_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llp", "co", "company", "inc", "corp",
    "corporation", "and", "the", "industries", "services", "enterprises",
}


def gstin_check_char(first14: str) -> str:
    """GSTIN check character (mod-36 Luhn-style checksum over the first 14 characters)."""
    total = 0
    for index, char in enumerate(first14):
        product = GSTIN_CHARS.index(char) * (1 if index % 2 == 0 else 2)
        total += product // 36 + product % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def clean_gstin(value: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (value or "").upper())


def gstin_problem(value: str | None) -> str | None:
    """Return a human-readable reason the GSTIN is invalid, or None if it is valid."""
    gstin = clean_gstin(value)
    if not gstin:
        return "GSTIN is missing"
    if len(gstin) != 15:
        return f"GSTIN has {len(gstin)} characters, expected 15"
    if not GSTIN_PATTERN.match(gstin):
        return "GSTIN does not follow the state-code + PAN + entity + Z + check pattern"
    if gstin_check_char(gstin[:14]) != gstin[14]:
        return "GSTIN check digit is wrong (likely a typo or a made-up number)"
    return None


def invoice_number_key(value: str | None) -> str:
    """Canonical form of an invoice number: upper case, separators removed, leading zeros dropped per group.

    "INV/26-27/0457", "inv-26-27-457" and "INV 26 27 457" all become "INV|26|27|457".
    """
    groups = re.findall(r"[A-Z]+|\d+", (value or "").upper())
    return "|".join(group.lstrip("0") or "0" if group.isdigit() else group for group in groups)


def po_number_key(value: str | None) -> str | None:
    """Canonical PO number "PO-YYYY-NNNN" from loose spellings like "PO 2026/102" or "2026-0102"."""
    text = (value or "").upper()
    match = re.search(r"(20\d{2})\s*[-/ .]?\s*0*(\d{1,4})\b", text)
    if not match:
        return None
    return f"PO-{match.group(1)}-{int(match.group(2)):04d}"


def name_key(value: str | None) -> str:
    """Company name reduced to its distinctive words ("Sharma Office Supplies Pvt. Ltd." -> "sharma office supplies")."""
    words = re.findall(r"[a-z0-9]+", (value or "").lower())
    return " ".join(word for word in words if word not in LEGAL_SUFFIXES)


def similarity(a: str | None, b: str | None) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def text_similarity(a: str | None, b: str | None) -> float:
    """Similarity of two descriptions: the better of character overlap and word overlap."""
    words_a = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
    words_b = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    word_score = len(words_a & words_b) / max(1, min(len(words_a), len(words_b)))
    return max(similarity(a, b), word_score)
