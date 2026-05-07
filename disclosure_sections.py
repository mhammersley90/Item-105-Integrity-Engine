"""SEC filing section extractors for the Disclosure Integrity Engine.

Pulls Risk Factors, MD&A, and Financial Statements from a 10-K, 20-F,
or 10-Q HTML body by their canonical Item headers. The Skill running
in GC AI uses Risk Factors as its REQUIRED BASELINE for Item 105
adequacy testing; it uses MD&A and Financial Statements for materiality
calibration. Without these three documents the Skill is starting blind.

Strategy
--------
SEC HTML is heterogeneous (font tags, deeply nested tables, inline
XBRL, etc.) so we work on plain text extracted by BeautifulSoup rather
than trying to walk the DOM. For each target section:

  1. Collapse whitespace / non-breaking spaces into single spaces.
  2. Find every position where the section's start header matches
     (e.g. "Item 1A. Risk Factors" for a 10-K).
  3. The first match is almost always the table-of-contents entry; the
     last match is the actual section start. Take the last match.
  4. Find the next "Item N[A-Z]?." header after that position; that's
     the end boundary.
  5. Slice the text between, strip, and return.

This is good enough for the major issuers' filings on EDGAR. If a
filing uses a non-standard heading (typically very small issuers), the
extractor returns None for that section and the caller logs the miss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from bs4 import BeautifulSoup


# Section definitions. start_re is anchored on the canonical Item
# header. It runs against the cleaned plain text in lowercase, with
# all whitespace collapsed to single ASCII spaces, so the patterns
# don't need to be flexible about spacing.

@dataclass(frozen=True)
class SectionSpec:
    key: str        # short identifier used in filenames + manifest
    label: str      # human-readable name for the Skill / UI
    start_re: re.Pattern[str]


# 10-K / 10-Q (10-Q uses the same item structure for Risk Factors).
SECTIONS_10K: tuple[SectionSpec, ...] = (
    SectionSpec(
        "RiskFactors",
        "Risk Factors",
        re.compile(r"item\s*1a\.?\s*risk\s+factors"),
    ),
    SectionSpec(
        "MDA",
        "Management's Discussion and Analysis",
        # Two acceptable phrasings: full title or just "Management's
        # Discussion and Analysis" (some 10-Ks abbreviate).
        re.compile(
            r"item\s*7\.?\s*"
            r"(?:management['’]s\s+discussion|"
            r"management's\s+discussion)"
        ),
    ),
    SectionSpec(
        "FinancialStatements",
        "Financial Statements",
        re.compile(r"item\s*8\.?\s*financial\s+statements"),
    ),
)

# 20-F (foreign private issuers). Item 3.D for Risk Factors; Item 5
# for Operating and Financial Review; Item 18 for Financial Statements.
SECTIONS_20F: tuple[SectionSpec, ...] = (
    SectionSpec(
        "RiskFactors",
        "Risk Factors",
        re.compile(r"item\s*3\.?\s*d\.?\s*risk\s+factors"),
    ),
    SectionSpec(
        "MDA",
        "Operating and Financial Review and Prospects",
        re.compile(r"item\s*5\.?\s*operating\s+and\s+financial\s+review"),
    ),
    SectionSpec(
        "FinancialStatements",
        "Financial Statements",
        # 20-F filers use Item 18 if they reconcile to US GAAP, else
        # Item 17. Match either.
        re.compile(r"item\s*1[78]\.?\s*financial\s+statements"),
    ),
)

# Generic "next Item header" pattern used to find the end boundary.
# Matches "Item 1.", "Item 1A.", "Item 7A.", "Item 18." with a
# subsequent capitalized word so we don't false-positive on prose
# references like "Item 1.01".
_NEXT_ITEM_RE = re.compile(
    r"item\s*\d+[a-z]?\.?\s+[a-z]",
    re.IGNORECASE,
)


def _spec_for_form(form: str) -> tuple[SectionSpec, ...]:
    """Pick the section list for a form. Falls back to 10-K rules."""
    f = (form or "").upper().strip()
    if f.startswith("20-F"):
        return SECTIONS_20F
    return SECTIONS_10K


def _clean_text(html: str) -> str:
    """HTML -> single-spaced lowercase plain text for matching.

    BeautifulSoup with the default html.parser handles inline XBRL
    and the SEC's font-soup well enough. We don't try to preserve
    headings — headings are reconstructed from the Item pattern.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Drop <script>, <style>, and SEC's hidden inline-XBRL wrappers.
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    # Normalize whitespace and various non-breaking-space variants.
    text = text.replace(" ", " ").replace("​", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass(frozen=True)
class ExtractedSection:
    spec: SectionSpec
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


def extract_sections(
    html: str, form: str, *, sections: Iterable[str] | None = None
) -> dict[str, ExtractedSection]:
    """Extract one or more sections from a filing's primary HTML.

    Returns a dict keyed by SectionSpec.key. Sections that can't be
    located are omitted from the result (the caller decides whether
    that's a soft miss or a hard error).

    `sections`, if given, restricts extraction to those keys (e.g.
    ``["RiskFactors"]``); otherwise all three are attempted.
    """
    cleaned = _clean_text(html)
    cleaned_lc = cleaned.lower()
    specs = _spec_for_form(form)
    if sections is not None:
        wanted = set(sections)
        specs = tuple(s for s in specs if s.key in wanted)

    out: dict[str, ExtractedSection] = {}
    for spec in specs:
        matches = list(spec.start_re.finditer(cleaned_lc))
        if not matches:
            continue

        # Each match could be the TOC entry, the actual section body,
        # or a cross-reference ("see Item 1A. Risk Factors above"). For
        # each match, compute its slice length up to the next Item
        # header; the body is the match with the LARGEST slice. This
        # is more robust than "last match wins" against filings that
        # cross-reference the section after it ends.
        candidates: list[tuple[int, int, int]] = []  # (start, end, length)
        for m in matches:
            start = m.start()
            next_match = _NEXT_ITEM_RE.search(cleaned_lc, pos=start + 8)
            end = next_match.start() if next_match else len(cleaned)
            candidates.append((start, end, end - start))
        start, end, length = max(candidates, key=lambda t: t[2])
        body = cleaned[start:end].strip()

        # Drop clearly-degenerate slices (TOC entry only) but keep
        # legitimately-short sections — 10-Q risk-factor updates often
        # read "There have been no material changes from the risk
        # factors disclosed in our Annual Report" and can run as short
        # as ~120 chars. The 100-char floor lets those through while
        # still cutting TOC dotted-leader entries (typically <60 chars).
        if len(body) < 100:
            continue
        out[spec.key] = ExtractedSection(spec=spec, text=body)
    return out
