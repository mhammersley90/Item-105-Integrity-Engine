"""Asserted-claim metadata extractor for the Disclosure Integrity Engine.

The GC AI Skill's "Asserted Claim Materiality Rule" activates when a
document contains:

  * a quantified claim amount,
  * a named or identifiable claimant,
  * a legal basis for liability, AND/OR
  * a structured assertion of liability (proof of debt, complaint,
    arbitration demand).

When such a document is present, the Skill MUST treat it as evidence
of potentially material risk and escalate omission as 🔴 Critical
under Item 105 — even if the claim is disputed or unresolved.

This module pulls those structured fields out of the text the
CourtListener API hands us (description / snippet / case name / docket
description) so the Skill consumes them as named manifest fields
rather than re-parsing raw text. We don't try to OCR the PDF itself
in v1; CourtListener's metadata fields carry enough signal for the
common shapes (Proof of Claim, Adversary Complaint, Demand for
Arbitration). PDF parsing can be a follow-up.

All numeric examples in this module are synthetic.

Outputs
-------
``extract_claim_metadata(*texts)`` returns a dict with these keys:

  * ``is_asserted_claim``   — bool; does this look like a structured
                              quantified assertion?
  * ``amount``              — int | None; numeric value normalized to
                              base units (e.g. 12_345)
  * ``amount_str``          — str | None; original substring matched
  * ``currency``            — str | None; "USD" / "GBP" / "EUR" / ...
  * ``claimant``            — str | None; best-effort named entity
  * ``legal_basis``         — str | None; cue phrase indicating the
                              legal theory (e.g. "proof of claim",
                              "adversary complaint", "indemnification")
  * ``document_type``       — str | None; document category cue
                              ("Proof of Claim", "Complaint", ...)
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------
# Currency symbols and ISO codes we recognise. The order matters for
# disambiguation: longer/more specific tokens first.
# --------------------------------------------------------------------------

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
    "C$": "CAD",
    "A$": "AUD",
}

_CURRENCY_CODES = {
    "USD", "GBP", "EUR", "JPY", "CHF", "CAD", "AUD", "NOK", "SEK",
    "DKK", "INR", "CNY", "HKD", "SGD", "BRL", "MXN", "ZAR", "KRW",
}

# Multipliers attached to amounts in prose form.
_MULTIPLIERS = {
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
    "k": 1_000,
    "m": 1_000_000,
    "bn": 1_000_000_000,
    "b": 1_000_000_000,
}

# --------------------------------------------------------------------------
# Regex catalog. Compiled once at import.
# --------------------------------------------------------------------------

# Amount with currency symbol prefix: "$12,345" or "$50K". The body
# matches a leading number with optional decimal and optional
# comma-thousands grouping, followed by an optional multiplier.
#
# Alternation order matters: regex matches alternatives left-to-right at
# each scan position, so longer prefixes (C$, A$) MUST come before the
# bare $ or "C$1M" would scan as "C" (no match), then "$1M" → USD with
# the C dropped.
_SYMBOL_AMOUNT_RE = re.compile(
    r"(?P<sym>C\$|A\$|\$|£|€|¥|₹)\s*"
    r"(?P<num>\d{1,3}(?:[,\.]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<mult>thousand|million|billion|trillion|bn|b|m|k)?\b",
    re.IGNORECASE,
)

# Amount with ISO code prefix or suffix: "USD 12,345" or
# "50 thousand GBP". Slightly looser than the symbol form; we require
# the code to appear within 4 chars of the number.
_CODE_AMOUNT_RE = re.compile(
    r"\b(?P<code>USD|GBP|EUR|JPY|CHF|CAD|AUD|NOK|SEK|DKK|INR|CNY|HKD|"
    r"SGD|BRL|MXN|ZAR|KRW)\s*"
    r"(?P<num>\d{1,3}(?:[,\.]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<mult>thousand|million|billion|trillion|bn|b|m|k)?\b",
    re.IGNORECASE,
)

# Document-type cues that signal a structured assertion of liability.
# The first match (longest first) wins.
_DOC_TYPE_CUES = (
    ("Proof of Claim", re.compile(r"\bproof\s+of\s+claim\b", re.IGNORECASE)),
    ("Proof of Debt", re.compile(r"\bproof\s+of\s+debt\b", re.IGNORECASE)),
    ("Adversary Complaint",
     re.compile(r"\badversary\s+(?:proceeding|complaint)\b", re.IGNORECASE)),
    ("Complaint", re.compile(r"\bcomplaint\b", re.IGNORECASE)),
    ("Arbitration Demand",
     re.compile(r"\b(?:demand\s+for\s+arbitration|arbitration\s+demand)\b",
                re.IGNORECASE)),
    ("Voluntary Petition",
     re.compile(r"\bvoluntary\s+petition\b", re.IGNORECASE)),
    ("Indemnification Demand",
     re.compile(r"\b(?:indemnification\s+(?:demand|claim)|claim\s+for\s+"
                r"indemnification)\b", re.IGNORECASE)),
    ("Notice of Claim",
     re.compile(r"\bnotice\s+of\s+claim\b", re.IGNORECASE)),
)

# Legal-basis cue phrases. Lighter-weight than document type — used
# even where there's no formal document name match.
_LEGAL_BASIS_CUES = (
    ("indemnification",
     re.compile(r"\bindemnif\w+\b", re.IGNORECASE)),
    ("breach of contract",
     re.compile(r"\bbreach\s+of\s+(?:contract|agreement)\b", re.IGNORECASE)),
    ("breach of warranty",
     re.compile(r"\bbreach\s+of\s+warrant\w+\b", re.IGNORECASE)),
    ("guaranty / guarantee",
     re.compile(r"\bguarant(?:y|ee|or)\w*\b", re.IGNORECASE)),
    ("tort claim",
     re.compile(r"\btort\s+claim\b", re.IGNORECASE)),
    ("fraud",
     re.compile(r"\bfraud\w*\b", re.IGNORECASE)),
    ("breach of fiduciary duty",
     re.compile(r"\bfiduciary\s+dut\w+\b", re.IGNORECASE)),
    ("securities fraud",
     re.compile(r"\bsecurities\s+(?:fraud|violation\w*)\b", re.IGNORECASE)),
)

# Claimant-detection: "filed by <Name>", "<Name> v.", "claim of <Name>".
# Best-effort — court filings have wildly varied phrasings.
#
# The "filed by" lookahead used to be `(?=\s+(...|\(|$))` but `$` inside
# an alternation that requires `\s+` first never matches (you can't
# have whitespace before end-of-string). Split into two alternatives
# so we anchor cleanly on either a delimiter word or the actual end.
_CLAIMANT_PATTERNS = (
    # Each list-word in the lookahead anchors on \b so we don't false-
    # positive on a name whose next word *starts with* one of the
    # tokens (e.g. "DEMO_ENTITY_001 INSTANCE" — without \b, " in" would
    # match the start of "INSTANCE" and clip the claimant to
    # "DEMO_ENTITY_001").
    re.compile(
        r"\bfiled\s+by\s+([A-Z][\w&,\.\- ]{2,60}?)"
        r"(?=\s+(?:against|in|on|for|alleging|under)\b"
        r"|\s*[\(\.,;]"
        r"|\s*$)",
        re.IGNORECASE,
    ),
    re.compile(r"\b([A-Z][\w&,\.\- ]{2,60}?)\s+v\.\s+([A-Z][\w&,\.\- ]{2,60})"),
    re.compile(
        r"\bclaim(?:ant)?\s+(?:of|by)\s+([A-Z][\w&,\.\- ]{2,60})",
        re.IGNORECASE,
    ),
)


def _normalize_amount(num_text: str, multiplier_text: str | None) -> int | None:
    """Convert "12,345" + None or "12" + "thousand" to an int."""
    if not num_text:
        return None
    cleaned = num_text.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if multiplier_text:
        mult = _MULTIPLIERS.get(multiplier_text.lower())
        if mult:
            value *= mult
    if value <= 0:
        return None
    return int(value)


def _find_first_amount(blob: str) -> dict[str, Any]:
    """Return the first credible currency+amount pair in the blob.

    Scans for symbol-prefixed amounts first (most reliable), then
    falls back to ISO code-prefixed amounts.
    """
    for m in _SYMBOL_AMOUNT_RE.finditer(blob):
        amount = _normalize_amount(m.group("num"), m.group("mult"))
        if amount is None:
            continue
        # Filter out tiny matches that are likely incidental fees
        # (e.g. "$5 fee", "$1.00").
        if amount < 10_000:
            continue
        sym = m.group("sym")
        return {
            "amount": amount,
            "amount_str": m.group(0).strip(),
            "currency": _CURRENCY_SYMBOLS.get(sym, sym),
        }
    for m in _CODE_AMOUNT_RE.finditer(blob):
        code = m.group("code").upper()
        if code not in _CURRENCY_CODES:
            continue
        amount = _normalize_amount(m.group("num"), m.group("mult"))
        if amount is None or amount < 10_000:
            continue
        return {
            "amount": amount,
            "amount_str": m.group(0).strip(),
            "currency": code,
        }
    return {}


def _find_document_type(blob: str) -> str | None:
    for label, pattern in _DOC_TYPE_CUES:
        if pattern.search(blob):
            return label
    return None


def _find_legal_basis(blob: str) -> str | None:
    for label, pattern in _LEGAL_BASIS_CUES:
        if pattern.search(blob):
            return label
    return None


def _find_claimant(blob: str) -> str | None:
    for pattern in _CLAIMANT_PATTERNS:
        m = pattern.search(blob)
        if m:
            # First capture group is the (likely) claimant.
            candidate = m.group(1).strip(" ,.")
            if 3 <= len(candidate) <= 80:
                return candidate
    return None


def extract_claim_metadata(*texts: str) -> dict[str, Any]:
    """Pull asserted-claim signals out of one or more text blobs.

    Pass any combination of CourtListener's ``description``,
    ``snippet``, ``caseName``, ``docket_description``, etc. The blobs
    are concatenated before extraction; longer / more specific blobs
    should come first as the first credible amount wins.

    A document is flagged as ``is_asserted_claim=True`` when at least
    two of {amount, document_type, legal_basis} are present. A bare
    "$5 million" mention without context, or a pure complaint-without-
    amount, doesn't trip the flag — both are routine in CL hits and
    would over-escalate the Skill.
    """
    blob = " ".join(t for t in texts if t)
    if not blob:
        return {"is_asserted_claim": False}

    amount_info = _find_first_amount(blob)
    document_type = _find_document_type(blob)
    legal_basis = _find_legal_basis(blob)
    claimant = _find_claimant(blob)

    signals = sum(
        1 for x in (amount_info.get("amount"), document_type, legal_basis)
        if x
    )
    is_asserted_claim = signals >= 2

    return {
        "is_asserted_claim": is_asserted_claim,
        "amount": amount_info.get("amount"),
        "amount_str": amount_info.get("amount_str"),
        "currency": amount_info.get("currency"),
        "claimant": claimant,
        "legal_basis": legal_basis,
        "document_type": document_type,
    }


def format_amount(amount: int | None, currency: str | None) -> str:
    """Render a numeric amount + currency in compact human-readable form.

    Used in table cells and filenames. Always strips leading/trailing
    whitespace so a missing currency doesn't leak a leading space into
    the output.

    Examples:
      (12_000,   "USD") -> "USD 12K"
      (50_000,   "USD") -> "USD 50K"
      (75_000,   "USD") -> "USD 75K"
      (50_000,   None)  -> "50K"
    """
    if amount is None:
        return ""
    cur = currency or ""
    if amount >= 1_000_000_000:
        body = f"{amount / 1_000_000_000:.1f}B"
    elif amount >= 1_000_000:
        body = f"{amount / 1_000_000:.0f}M"
    elif amount >= 1_000:
        body = f"{amount / 1_000:.0f}K"
    else:
        body = str(amount)
    return f"{cur} {body}".strip() if cur else body
