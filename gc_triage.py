"""GC-triage classifier shared by both front-ends.

Used by app.py and nicegui_app.py to label CourtListener / RECAP hits
as 'contract', 'lawsuit', or '' (other / unclear) so a General Counsel
can quickly filter the haystack down to filings that are likely
contracts to feed into GC AI for risk-disclosure mapping.

The keyword lists are tuned for bankruptcy + commercial litigation
matter docket vocabulary. Tweak as you encounter real-world false
positives or negatives — keep them in this single module so both GUIs
stay in sync.
"""

from __future__ import annotations

# Filings whose description / type / snippet contains one of these
# keywords are classified as 'contract'. Bankruptcy court filings
# expose contracts via Schedules of Assets and Liabilities (Schedule G
# lists executory contracts), motions to assume / reject executory
# contracts, asset-purchase agreements, plan support agreements,
# indenture amendments, and so on.
_GC_CONTRACT_KEYWORDS = (
    "agreement", "executory contract", "executory", "schedule g",
    "schedule g/h", "lease", "asset purchase", "indenture",
    "promissory note", "license agreement", "guaranty", "guarantee",
    "supply agreement", "credit agreement", "loan agreement",
    "intercreditor", "subordination", "assignment of contract",
    "cure amount", "cure notice", "assume", "assumption",
    "reject", "rejection", "amendment to",
)

# Filings classified as 'lawsuit'. Adversary proceedings, complaints,
# motions to dismiss, summary judgment motions, and discovery disputes
# all live here.
_GC_LAWSUIT_KEYWORDS = (
    "complaint", "adversary proceeding", "summary judgment",
    "motion to dismiss", "jury demand", "answer to complaint",
    "counterclaim", "subpoena", "preliminary injunction",
    "temporary restraining order", "class action",
    "tort claim", "discovery dispute", "deposition notice",
)


def classify_filing(*texts: str) -> str:
    """Return 'contract', 'lawsuit', or '' based on keyword signals.

    The GC's primary interest is contract identification (a complaint
    that mentions a contract is most useful as a contract surfacing
    tool, not a lawsuit one), so when both signals fire we default to
    'contract'.
    """
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return ""
    if any(k in blob for k in _GC_CONTRACT_KEYWORDS):
        return "contract"
    if any(k in blob for k in _GC_LAWSUIT_KEYWORDS):
        return "lawsuit"
    return ""
