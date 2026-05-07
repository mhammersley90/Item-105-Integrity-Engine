"""Headless usage example for backend integrators.

Demonstrates how to drive the three search clients and the staging /
commit pipeline programmatically — no Streamlit, no GUI. Useful for
anyone wiring this into a server-side workflow (cron job, queue
worker, CI pipeline, internal tooling).

Run any of the snippets below as a script, or copy the relevant calls
into your own service.

All names, dates, case criteria, and identifiers in these examples are
synthetic. Replace them with appropriate search inputs for your use case.

Required environment variables (only the ones for the sources you use):
    SEC_USER_AGENT          e.g. "DEMO_OPERATOR contact@example.invalid" (replace before use)
    GC_AI_API_KEY           u:gcai_... (only if uploading to GC AI)
    GC_AI_FOLDER_ID         existing GC AI folder UUID (or use create_folder)
    PACER_USERNAME          (only for PACER PCL search)
    PACER_PASSWORD
    PACER_OTP_CODE          (if MFA is enabled on the PACER account)
    COURTLISTENER_TOKEN     40-char token from courtlistener.com/profile/api-token/
"""

from __future__ import annotations

import os
from pathlib import Path

from sec_contract_scanner import (
    EdgarClient,
    GCAIClient,
    commit_selection,
    scan_and_download,
    STAGING_DIRNAME,
)
from pacer_client import PACERClient, PCLClient
from courtlistener_client import CourtListenerClient


# --------------------------------------------------------------------------
# Example 1: SEC scan, save matches locally, no GC AI upload
# --------------------------------------------------------------------------

def example_sec_scan_local_only():
    """Search EDGAR for contracts mentioning a phrase; save matches under
    ./contracts/ and write a manifest.jsonl. No GC AI upload."""
    edgar = EdgarClient(user_agent=os.environ["SEC_USER_AGENT"])
    scan_and_download(
        client=edgar,
        phrase="DEMO_ENTITY_001",
        output_dir=Path("./contracts"),
        forms=["10-K", "10-Q", "8-K", "S-1", "S-4", "20-F"],
        start="2098-01-01",
        end="2099-12-31",
        max_filings=None,
        include_exhibits=True,
        # No gc_ai client passed -> local save only
    )


# --------------------------------------------------------------------------
# Example 2: SEC scan with auto-upload into a GC AI folder
# --------------------------------------------------------------------------

def example_sec_scan_with_gc_ai_upload():
    """Same as above but each saved contract is also uploaded to a GC AI
    folder. The folder must already exist; pass its UUID."""
    edgar = EdgarClient(user_agent=os.environ["SEC_USER_AGENT"])
    gc_ai = GCAIClient(api_key=os.environ["GC_AI_API_KEY"])
    folder_id = os.environ["GC_AI_FOLDER_ID"]

    scan_and_download(
        client=edgar,
        phrase="DEMO_ENTITY_001",
        output_dir=Path("./contracts"),
        forms=["10-K", "10-Q", "8-K"],
        start=None,
        end=None,
        max_filings=50,
        gc_ai=gc_ai,
        gc_ai_folder_id=folder_id,
        include_exhibits=True,
    )


# --------------------------------------------------------------------------
# Example 3: Two-phase staging — preview first, then commit selected rows
# --------------------------------------------------------------------------

def example_sec_preview_then_commit():
    """Run a preview-mode scan that stages all matches into _staging/,
    then programmatically commit a subset (filtered by your own logic)
    to the main folder + GC AI."""
    out_dir = Path("./contracts")
    edgar = EdgarClient(user_agent=os.environ["SEC_USER_AGENT"])
    gc_ai = GCAIClient(api_key=os.environ["GC_AI_API_KEY"])
    folder_id = os.environ["GC_AI_FOLDER_ID"]

    # Phase 1: stage everything that matches into _staging/
    scan_and_download(
        client=edgar,
        phrase="DEMO_ENTITY_001",
        output_dir=out_dir,
        forms=["10-K", "10-Q", "8-K"],
        start=None,
        end=None,
        max_filings=None,
        include_exhibits=True,
        staging_only=True,
    )

    # Phase 2: read the staging manifest, filter, commit selected rows
    import json
    staging_dir = out_dir / STAGING_DIRNAME
    staged_records = []
    with (staging_dir / "manifest.jsonl").open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            staged_records.append(json.loads(line))

    # Apply your own selection logic — e.g. keep only Ex 10 contracts
    selected = [r for r in staged_records if r.get("exhibit") == "10"]

    result = commit_selection(
        staging_dir=staging_dir,
        output_dir=out_dir,
        selected_records=selected,
        gc_ai=gc_ai,
        gc_ai_folder_id=folder_id,
    )
    print(
        f"Committed: moved={result['moved']}, "
        f"uploaded={result['uploaded']}, "
        f"failures={len(result['failed'])}"
    )


# --------------------------------------------------------------------------
# Example 4: PACER PCL party search (federal court case index)
# --------------------------------------------------------------------------

def example_pacer_party_search():
    """Authenticate to PACER and search the federal court case index for
    a corporate entity. QA environment is free; production bills $0.10
    per search page."""
    auth = PACERClient(environment="qa")  # change to "production" when live
    creds = auth.authenticate(
        username=os.environ["PACER_USERNAME"],
        password=os.environ["PACER_PASSWORD"],
        otp_code=os.environ.get("PACER_OTP_CODE"),
    )

    pcl = PCLClient(token=creds.token, environment="qa")
    result = pcl.search_parties({
        "lastName": "DEMO_ENTITY_001",
        "courtCase": {
            "jurisdictionType": "cv",            # civil cases
            "courtId": ["nysdc"],                # example district court
            "dateFiledFrom": "2098-01-01",
        },
    })
    for hit in result.content:
        cc = hit.get("courtCase") or {}
        print(
            f"{cc.get('dateFiled')}  {cc.get('caseNumberFull')}  "
            f"{cc.get('caseTitle')}  -> {cc.get('caseLink')}"
        )

    auth.logout(creds.token)


# --------------------------------------------------------------------------
# Example 5: CourtListener RECAP search (free archived PACER documents)
# --------------------------------------------------------------------------

def example_courtlistener_recap_search():
    """Search the RECAP archive — federal court documents previously
    fetched from PACER, mirrored for free. Returns hits with PDF URLs
    you can download via cl.download_pdf()."""
    cl = CourtListenerClient(api_token=os.environ["COURTLISTENER_TOKEN"])
    page = cl.search_recap(
        "DEMO_ENTITY_001",
        court_ids=["nysd", "cacd", "ilnd"],
        filed_after="2098-01-01",
        order_by="dateFiled desc",
    )
    for hit in page.results:
        case = hit.get("caseName") or hit.get("case_name")
        court = hit.get("court_id") or hit.get("court")
        filepath = hit.get("filepath_local")
        print(f"{court}  {case}  PDF: {filepath or '(not in RECAP)'}")
    # Paginate via cl.next_page(page) — returns an empty result on terminal page.


# --------------------------------------------------------------------------
# Example 6: Pray-and-Pay for a document not yet in RECAP
# --------------------------------------------------------------------------

def example_pray_for_document(recap_document_id: int):
    """Create a free Pray-and-Pay request for a document that hasn't been
    fetched into RECAP yet. CourtListener emails you when a document
    becomes available."""
    cl = CourtListenerClient(api_token=os.environ["COURTLISTENER_TOKEN"])
    prayer = cl.create_prayer(recap_document_id)
    print(f"Prayer created: id={prayer['id']}, status={prayer['status']}")


if __name__ == "__main__":
    # Pick the example you want to run, or call them from your own service.
    # The default below is the safest free path.
    example_sec_scan_local_only()
