"""ContractReview · GC AI — NiceGUI front-end.

Run with:
    pip install -e ".[nicegui]"
    python nicegui_app.py

Then open http://localhost:8080.

Parallel implementation of app.py (Streamlit) for materially better
visual fidelity to gc.ai. Both front-ends share the same backend
modules and manifest schema, so you can switch between them.

Sections (feature-parity with the Streamlit app):
  * SEC scan with live log streaming + concurrent-scan lock
  * Staging picker with row selection (commit / commit-and-upload)
  * Saved-contracts table with re-upload to GC AI
  * PACER Case Locator search (party / case modes)
  * CourtListener / RECAP search with GC-triage classifier,
    Stage selected PDFs, and Pray-and-Pay
  * Citation Lookup
  * Save profile / launchd plist generator
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from nicegui import run, ui

from sec_contract_scanner import (
    GC_AI_DEFAULT_BASE_URL,
    ISSUER_DEFAULT_FORMS,
    PROFILE_DEFAULT_FORMS,
    STAGING_DIRNAME,
    EdgarClient,
    GCAIClient,
    _append_manifest_line,
    commit_selection,
    fetch_and_save_disclosure_sections,
    fetch_issuer_material_exhibits,
    safe_filename,
    scan_and_download,
)
from pacer_client import PACERAuthError, PACERClient, PCLClient, PCLError
from courtlistener_client import CourtListenerClient, CourtListenerError
from gc_triage import classify_filing as _classify_filing
from claim_extractor import extract_claim_metadata, format_amount


DEFAULT_FORMS = list(PROFILE_DEFAULT_FORMS)
ALL_FORMS = DEFAULT_FORMS + [
    "10-K/A", "10-Q/A", "8-K/A", "S-1/A", "S-3", "S-3/A", "S-4/A",
    "20-F/A", "6-K", "DEF 14A", "DEFM14A", "F-1", "F-3", "F-4",
    "NT 10-K", "NT 10-Q", "POS AM", "424B3", "424B5",
]

# Process-wide lock so two browser tabs can't run scans concurrently.
# Same protection as the Streamlit version (redirect_stdout is
# process-global, not thread-local).
_active_scan_lock = threading.Lock()


def _cl_normalize(result: Any, token: str) -> dict:
    """Flatten a CourtListenerSearchResult into table-ready rows + metadata.

    The raw result is kept for `next_page()`; rows include the GC-triage
    classification, sanitized snippet, and resolved PDF link / recap doc id.
    """
    rows: list[dict] = []
    for idx, hit in enumerate(result.results):
        abs_url = hit.get("absolute_url")
        cl_link = (
            f"https://www.courtlistener.com{abs_url}"
            if abs_url and abs_url.startswith("/")
            else abs_url
        )
        filepath = hit.get("filepath_local")
        if hit.get("docket_id"):
            nested = hit.get("recap_documents") or []
            nested = nested if isinstance(nested, list) else []
            if not filepath and nested:
                filepath = (
                    nested[0].get("filepath_local")
                    or nested[0].get("local_path")
                )
            recap_doc_id = next(
                (
                    d.get("id") for d in nested
                    if d.get("id") and not d.get("filepath_local")
                ),
                nested[0].get("id") if nested else None,
            )
        else:
            if not filepath:
                nested = hit.get("opinions") or []
                if nested and isinstance(nested, list):
                    filepath = (
                        nested[0].get("filepath_local")
                        or nested[0].get("local_path")
                    )
            recap_doc_id = hit.get("id")
        pdf_link = (
            f"https://www.courtlistener.com{filepath}"
            if filepath and filepath.startswith("/")
            else filepath
        )
        snippet = (
            hit.get("snippet")
            or hit.get("short_description")
            or ""
        )
        snippet = re.sub(r"</?mark>", "", snippet)
        description = (
            hit.get("description")
            or hit.get("short_description")
            or hit.get("docket_description")
            or ""
        )
        doc_type = (
            hit.get("document_type")
            or hit.get("type_of_document")
            or ""
        )
        court_value = (
            hit.get("court_id")
            or hit.get("court_citation_string")
            or hit.get("court")
            or hit.get("docket_court_id")
        )
        case_value = (
            hit.get("caseName")
            or hit.get("case_name")
            or hit.get("docket_case_name")
        )
        docket_value = (
            hit.get("docketNumber")
            or hit.get("docket_number")
            or hit.get("docket_number_core")
        )
        kind = _classify_filing(description, doc_type, snippet, case_value or "")
        # Claim-metadata extraction. The Skill's Asserted Claim
        # Materiality Rule activates when the document is a structured,
        # quantified assertion of liability. Surface those as a third
        # `kind` value so the GC can filter on them directly, and pass
        # through the structured fields the Skill expects (amount,
        # claimant, legal_basis, document_type) into the manifest.
        claim_blob_parts = [
            description, snippet, case_value, doc_type,
            hit.get("docket_description") or "",
        ]
        claim = extract_claim_metadata(*[p for p in claim_blob_parts if p])
        if claim.get("is_asserted_claim"):
            kind = "asserted_claim"
        rows.append({
            "row_id": f"r{idx}",
            "kind": kind,
            "claim_amount": format_amount(
                claim.get("amount"), claim.get("currency"),
            ) or None,
            "filed": (
                hit.get("entry_date_filed")
                or hit.get("dateFiled")
                or hit.get("date_filed")
            ),
            "court": court_value,
            "case": case_value,
            "docket": docket_value,
            "description": (description[:160] if description else None),
            "doc_type": doc_type or None,
            "snippet": snippet[:400] if snippet else None,
            "courtlistener": cl_link,
            "pdf": pdf_link,
            "recap_doc_id": recap_doc_id,
            # Structured claim fields preserved separately so the staging
            # writer can persist them to the manifest as named fields.
            "_claim_amount_raw": claim.get("amount"),
            "_claim_currency": claim.get("currency"),
            "_claim_claimant": claim.get("claimant"),
            "_claim_legal_basis": claim.get("legal_basis"),
            "_claim_document_type": claim.get("document_type"),
            "_claim_is_asserted": bool(claim.get("is_asserted_claim")),
        })
    return {
        "rows": rows,
        "token": token,
        "raw": result,
        "has_next": bool(getattr(result, "next_url", None)),
    }


def _load_manifest_records(directory: Path) -> list[dict]:
    manifest = directory / "manifest.jsonl"
    if not manifest.exists():
        return []
    rows: list[dict] = []
    with manifest.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


_GC_CSS = """
<style>
  body {
    background-color: #FAF5EE;
    color: #0E1A2D;
    font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
  }
  .gc-card {
    background-color: #FFFFFF;
    border: 1px solid #E8E1D2;
    border-radius: 14px;
    padding: 1.25rem 1.5rem;
  }
  .gc-pill {
    background-color: #F2EBDC;
    border: 1px solid #E8E1D2;
    border-radius: 9999px;
    padding: 0.5rem 1.25rem;
    color: #0E1A2D;
    font-size: 0.85rem;
    text-align: center;
  }
  .gc-rail {
    background-color: #0E1A2D;
    color: #FAF5EE;
    min-width: 64px;
    padding: 1rem 0;
  }
  .gc-sidebar {
    background-color: #F5EDDF;
    min-width: 320px;
    max-width: 360px;
    padding: 1.5rem 1.25rem;
    overflow-y: auto;
    height: 100vh;
  }
  .gc-main {
    flex-grow: 1;
    padding: 2rem;
    overflow-y: auto;
    height: 100vh;
  }
  .gc-section-title {
    font-size: 1.5rem;
    font-weight: 700;
    color: #0E1A2D;
    letter-spacing: -0.01em;
    margin: 0;
  }
  .gc-section-caption {
    color: #5A6273;
    font-size: 0.9rem;
  }
</style>
"""


# --------------------------------------------------------------------------
# Page — entire layout lives inside index() so handlers can close over
# widget refs. Theme calls must also live inside the page (NiceGUI 2/3
# rejects ui.* at module scope when @ui.page is used).
# --------------------------------------------------------------------------


@ui.page("/")
def index() -> None:
    ui.colors(
        primary="#0E1A2D",
        secondary="#5A6273",
        accent="#F2EBDC",
        positive="#0E1A2D",
        info="#0E1A2D",
        negative="#B23A48",
        warning="#C77B30",
    )
    ui.add_head_html(_GC_CSS)

    class LogPusher(io.TextIOBase):
        """File-like that pushes complete lines onto a NiceGUI ui.log."""

        def __init__(self, log_widget: Any) -> None:
            super().__init__()
            self.log = log_widget
            self._buf = ""

        def write(self, s: str) -> int:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self.log.push(line)
            return len(s)

        def flush(self) -> None:
            if self._buf:
                self.log.push(self._buf)
                self._buf = ""

    # Per-page mutable state. Captured by closures, persists across UI
    # events for the lifetime of this browser tab.
    state: dict[str, Any] = {
        "pacer": None,           # {"token", "env", "username", "client_code", "warning", "cost"}
        "cl_last_result": None,  # most recent CourtListenerSearchResult
    }

    with ui.row().classes("w-full gap-0 flex-nowrap").style(
        "min-height: 100vh; align-items: stretch;"
    ):

        # ---- Dark navy rail (jump-to-section nav) -----------------------
        # NiceGUI auto-assigns each element a DOM id of "c<n>"; we capture
        # the four target sections below as they're built and scroll to
        # them by that auto-id.
        nav_targets: dict[str, Any] = {}

        def _scroll_to(key: str) -> None:
            target = nav_targets.get(key)
            if target is None:
                return
            ui.run_javascript(
                f"document.getElementById('c{target.id}')"
                ".scrollIntoView({behavior: 'smooth', block: 'start'})"
            )

        with ui.column().classes("gc-rail items-center gap-4"):
            ui.icon("hub", size="md").classes(
                "text-white cursor-pointer"
            ).on("click", lambda: _scroll_to("workspace")).tooltip(
                "Build workspace"
            )
            ui.icon("search", size="md").classes(
                "text-white opacity-60 cursor-pointer"
            ).on("click", lambda: _scroll_to("sec")).tooltip("SEC search")
            ui.icon("balance", size="md").classes(
                "text-white opacity-60 cursor-pointer"
            ).on("click", lambda: _scroll_to("baseline")).tooltip(
                "Disclosure baseline"
            )
            ui.icon("folder_open", size="md").classes(
                "text-white opacity-60 cursor-pointer"
            ).on("click", lambda: _scroll_to("saved")).tooltip(
                "Saved contracts"
            )
            ui.icon("description", size="md").classes(
                "text-white opacity-60 cursor-pointer"
            ).on("click", lambda: _scroll_to("citations")).tooltip(
                "Citation lookup"
            )
            ui.icon("settings", size="md").classes(
                "text-white opacity-60 cursor-pointer"
            ).on("click", lambda: _scroll_to("schedule")).tooltip(
                "Save profile / Schedule"
            )

        # ---- Cream sidebar (credentials) ---------------------------------
        with ui.column().classes("gc-sidebar gap-3"):
            ui.label("Credentials").classes("text-lg font-bold")

            with ui.expansion("SEC", value=True).classes("w-full"):
                sec_user_agent = ui.input(
                    label="User-Agent",
                    placeholder="DEMO_OPERATOR contact@example.invalid",
                    value=os.environ.get("SEC_USER_AGENT") or "",
                ).props("outlined dense rounded").classes("w-full")
                ui.label("SEC requires name + contact email.").classes(
                    "text-xs text-secondary"
                )

            with ui.expansion("GC AI", value=True).classes("w-full"):
                gc_ai_key = ui.input(
                    label="API key (user-scoped)",
                    value=os.environ.get("GC_AI_API_KEY") or "",
                    password=True,
                    password_toggle_button=True,
                    placeholder="u:gcai_xxxxxxxxxxxxxxxx",
                ).props("outlined dense rounded").classes("w-full")
                gc_ai_base_url = ui.input(
                    label="API base URL",
                    value=GC_AI_DEFAULT_BASE_URL,
                ).props("outlined dense rounded").classes("w-full")

            with ui.expansion("PACER", value=False).classes("w-full"):
                pacer_env = ui.toggle(
                    {"qa": "QA (free)", "production": "Production (billed)"},
                    value="qa",
                ).props("inline dense")
                ui.label(
                    "QA is the free PACER test environment. Production charges "
                    "$0.10 per search page (~54 hits)."
                ).classes("text-xs text-secondary")
                pacer_username = ui.input(
                    label="Username",
                ).props("outlined dense rounded").classes("w-full")
                pacer_password = ui.input(
                    label="Password", password=True, password_toggle_button=True,
                ).props("outlined dense rounded").classes("w-full")
                pacer_client_code = ui.input(
                    label="Client code (optional)",
                ).props("outlined dense rounded").classes("w-full")
                pacer_otp = ui.input(
                    label="MFA one-time passcode",
                ).props("outlined dense rounded").classes("w-full")
                pacer_redact = ui.checkbox(
                    "I am a filer and confirm redaction compliance",
                    value=False,
                )
                pacer_status_label = ui.label("").classes(
                    "text-xs text-secondary"
                )
                with ui.row().classes("w-full gap-2"):
                    pacer_login_btn = ui.button("Login").props(
                        "rounded color=primary unelevated dense"
                    ).classes("flex-grow")
                    pacer_logout_btn = ui.button("Logout").props(
                        "rounded outline color=primary dense"
                    ).classes("flex-grow")

                def _render_pacer_status() -> None:
                    sess = state["pacer"]
                    if sess:
                        cost = sess.get("cost", 0.0)
                        pacer_status_label.text = (
                            f"Logged in as {sess['username']} "
                            f"({sess['env']}). Session cost: ${cost:.2f}"
                            + (" — QA is free." if sess["env"] == "qa" else "")
                        )
                    else:
                        pacer_status_label.text = "Not logged in."

                async def do_pacer_login() -> None:
                    if not pacer_username.value or not pacer_password.value:
                        ui.notify("Username and password required.", type="negative")
                        return
                    env = pacer_env.value
                    username = (pacer_username.value or "").strip()
                    password = pacer_password.value or ""
                    client_code = (pacer_client_code.value or "").strip() or None
                    otp = (pacer_otp.value or "").strip() or None
                    redact = bool(pacer_redact.value)

                    def do_login() -> Any:
                        client = PACERClient(environment=env)
                        return client.authenticate(
                            username=username,
                            password=password,
                            client_code=client_code,
                            otp_code=otp,
                            redact_flag=redact,
                        )

                    pacer_login_btn.props("loading")
                    try:
                        result = await run.io_bound(do_login)
                    except PACERAuthError as exc:
                        ui.notify(
                            f"PACER login failed (loginResult={exc.login_result}): {exc}",
                            type="negative", multi_line=True,
                        )
                        return
                    except Exception as exc:
                        ui.notify(f"PACER login error: {exc}", type="negative")
                        return
                    finally:
                        pacer_login_btn.props(remove="loading")

                    state["pacer"] = {
                        "token": result.token,
                        "env": env,
                        "username": username,
                        "client_code": result.client_code,
                        "warning": result.warning,
                        "cost": 0.0,
                    }
                    _render_pacer_status()
                    if result.warning:
                        ui.notify(result.warning, type="warning", multi_line=True)
                    ui.notify(f"Logged in to PACER ({env}).", type="positive")

                async def do_pacer_logout() -> None:
                    sess = state["pacer"]
                    if not sess:
                        ui.notify("Not logged in.", type="info")
                        return
                    token = sess["token"]
                    env = sess["env"]

                    def do_logout() -> None:
                        try:
                            PACERClient(environment=env).logout(token)
                        except Exception:
                            pass

                    await run.io_bound(do_logout)
                    state["pacer"] = None
                    _render_pacer_status()
                    ui.notify("Logged out of PACER.", type="positive")

                pacer_login_btn.on_click(do_pacer_login)
                pacer_logout_btn.on_click(do_pacer_logout)
                _render_pacer_status()

            with ui.expansion("CourtListener", value=False).classes("w-full"):
                cl_token = ui.input(
                    label="API token (free)",
                    value=os.environ.get("COURTLISTENER_TOKEN") or "",
                    password=True,
                    password_toggle_button=True,
                    placeholder="40-char token",
                ).props("outlined dense rounded").classes("w-full")
                ui.label(
                    "Get one free at courtlistener.com/profile/api-token/. "
                    "Free tier: 5 req/min, 50/hr, 125/day."
                ).classes("text-xs text-secondary")

        # ---- Main content area --------------------------------------------
        with ui.column().classes("gc-main gap-6"):

            ui.label("ContractReview · GC AI").classes(
                "text-3xl font-bold tracking-tight"
            )
            ui.label(
                "Find SEC filings containing a target phrase, then stage "
                "matches into a GC AI folder for risk-disclosure mapping."
            ).classes("gc-section-caption")
            with ui.element("div").classes("gc-pill").style("max-width: 720px;"):
                ui.label(
                    "Credentials stay on your machine. "
                    "Uploads to GC AI are encrypted in transit."
                )

            # ===================================================================
            # Workspace builder
            # ===================================================================
            # Headline action: given a CIK + company name, run all three
            # data-collection steps the GC AI Skill needs (disclosure
            # baseline + material contracts + RECAP asserted claims) and
            # drop everything into a single named GC AI folder.
            # Individual sections (SEC search, Disclosure baseline,
            # CourtListener) remain available below for ad-hoc use.
            workspace_card = ui.element("div").classes("gc-card w-full")
            nav_targets["workspace"] = workspace_card
            with workspace_card:
                ui.label("Workspace builder").classes("gc-section-title")
                ui.label(
                    "Build a complete document set for one company in one "
                    "click: pulls disclosure baseline (Risk Factors, MD&A, "
                    "Financial Statements), scans recent material contracts, "
                    "and stages RECAP asserted claims naming the issuer. "
                    "Saves to your local output folder; if a GC AI key is "
                    "set in the sidebar, also creates a named folder and "
                    "uploads everything ready for the Skill to consume."
                ).classes("gc-section-caption")

                with ui.row().classes("w-full gap-4 items-start"):
                    with ui.column().classes("flex-grow gap-3").style(
                        "min-width: 320px;"
                    ):
                        ws_company = ui.input(
                            label="Company name",
                            placeholder="e.g. DEMO_ISSUER_001 (synthetic token)",
                        ).props("outlined dense rounded").classes("w-full")
                        ws_cik = ui.input(
                            label="Issuer CIK",
                            placeholder="e.g. 0000000000 (synthetic placeholder)",
                        ).props("outlined dense rounded").classes("w-full")
                        ws_folder_name = ui.input(
                            label="GC AI folder name (defaults to company)",
                        ).props("outlined dense rounded").classes("w-full")
                    with ui.column().classes("flex-grow gap-3").style(
                        "min-width: 320px;"
                    ):
                        ws_steps = ui.select(
                            options={
                                "baseline": "Disclosure baseline (10-K / 20-F)",
                                "contracts": "Material exhibits (Ex 2 / 4 / 9 / 10)",
                                "recap": "RECAP asserted claims",
                            },
                            value=["baseline", "contracts", "recap"],
                            multiple=True,
                            label="Steps to run",
                        ).props(
                            "outlined dense rounded use-chips"
                        ).classes("w-full")
                        ws_max_filings = ui.number(
                            label="Max recent filings to scan (per issuer)",
                            value=30, min=1, step=10, format="%d",
                        ).props("outlined dense rounded").classes("w-full")

                ws_status = ui.label("").classes("gc-section-caption")
                ws_log = ui.log(max_lines=400).classes("w-full").style(
                    "background-color: #FFFFFF; border: 1px solid #E8E1D2; "
                    "border-radius: 10px; padding: 0.75rem; "
                    "font-family: ui-monospace, monospace; font-size: 0.78rem; "
                    "max-height: 300px;"
                )

                async def build_workspace() -> None:
                    company = (ws_company.value or "").strip()
                    cik = (ws_cik.value or "").strip()
                    if not company:
                        ui.notify("Company name is required.", type="warning")
                        return
                    if not cik:
                        ui.notify("Issuer CIK is required.", type="warning")
                        return
                    if not sec_user_agent.value or "@" not in sec_user_agent.value:
                        ui.notify(
                            "SEC User-Agent must include a contact email.",
                            type="negative",
                        )
                        return
                    # GC AI key is OPTIONAL. With a key, the workspace
                    # creates a named folder and uploads everything; without
                    # one, all artifacts are saved locally to out_dir and
                    # the user can drag them into GC AI later.

                    steps = set(ws_steps.value or [])
                    if not steps:
                        ui.notify("Pick at least one step.", type="warning")
                        return
                    if "recap" in steps and not (cl_token.value or "").strip():
                        ui.notify(
                            "RECAP step needs a CourtListener token in the "
                            "sidebar. Add one or untick RECAP.",
                            type="negative", multi_line=True,
                        )
                        return

                    # Capture all input values BEFORE acquiring the lock
                    # so a NiceGUI value-of-None doesn't leak the lock if
                    # any .strip() crashes. Use defensive `(x or "")` on
                    # every input to make None values safe.
                    out_dir = Path(
                        (sec_output_dir.value or "").strip() or "./contracts"
                    )
                    user_agent = (sec_user_agent.value or "").strip()
                    gc_key = (gc_ai_key.value or "").strip()
                    gc_base = (gc_ai_base_url.value or "").strip()
                    folder_name = (
                        (ws_folder_name.value or "").strip() or company
                    )
                    folder_description = (
                        f"GC AI Disclosure Integrity workspace for {company} "
                        f"(CIK {cik}). Auto-built by ContractReview."
                    )
                    cl_tok = (cl_token.value or "").strip()
                    max_filings_v = (
                        int(ws_max_filings.value) if ws_max_filings.value else None
                    )

                    if not _active_scan_lock.acquire(blocking=False):
                        ui.notify(
                            "Another scan is already running. Wait for it.",
                            type="warning",
                        )
                        return

                    ws_run_btn.props("loading")
                    ws_status.text = "Building workspace…"
                    ws_log.clear()

                    # `summary` is shared between do_build and the outer
                    # exception handler so we can surface the folder_id
                    # (if create_folder succeeded) even when a later step
                    # raises. Otherwise the user has no way to find the
                    # orphan folder in GC AI to clean it up.
                    summary: dict = {
                        "baseline": 0, "contracts_ran": False, "recap": 0,
                    }

                    def do_build() -> dict:
                        writer = LogPusher(ws_log)
                        try:
                            with contextlib.redirect_stdout(writer), \
                                 contextlib.redirect_stderr(writer):
                                edgar = EdgarClient(user_agent=user_agent)
                                gc_ai_client: GCAIClient | None = None
                                folder_id: str | None = None
                                if gc_key:
                                    gc_ai_client = GCAIClient(
                                        gc_key, base_url=gc_base or None,
                                    )
                                    folder = gc_ai_client.create_folder(
                                        name=folder_name,
                                        description=folder_description,
                                    )
                                    folder_id = folder["id"]
                                    # Record folder identity in summary
                                    # immediately so a downstream step's
                                    # exception still surfaces it for the
                                    # user (otherwise the folder lingers
                                    # in GC AI as an empty orphan with no
                                    # way for the user to find its UUID).
                                    summary["folder_id"] = folder_id
                                    summary["folder_path"] = folder.get(
                                        "path", folder_name
                                    )
                                    print(
                                        f"[1/4] Created GC AI folder "
                                        f"{folder.get('path', folder_name)} "
                                        f"(id={folder_id})"
                                    )
                                else:
                                    print(
                                        f"[1/4] No GC AI key — saving "
                                        f"locally to {out_dir} only. "
                                        f"Drag this folder into GC AI later "
                                        f"or re-run with a key to upload."
                                    )

                                if "baseline" in steps:
                                    print(f"\n[2/4] Pulling disclosure baseline for CIK {cik}…")
                                    baseline_records = (
                                        fetch_and_save_disclosure_sections(
                                            client=edgar,
                                            cik=cik,
                                            output_dir=out_dir,
                                            forms=["10-K", "20-F"],
                                            section_keys=[
                                                "RiskFactors", "MDA",
                                                "FinancialStatements",
                                            ],
                                            gc_ai=gc_ai_client,
                                            gc_ai_folder_id=folder_id,
                                        )
                                    )
                                    summary["baseline"] = len(baseline_records)
                                else:
                                    print("[2/4] Skipping disclosure baseline.")

                                if "contracts" in steps:
                                    print(
                                        f"\n[3/4] Pulling material exhibits "
                                        f"(Ex 2/4/9/10) from CIK {cik}'s "
                                        f"recent filings…"
                                    )
                                    # Skip 6-K (FPI interim reports —
                                    # press-release bodies + EX-99.1, no
                                    # material contracts) and 8-K (US
                                    # current reports — same reason). The
                                    # budget reaches contract-bearing
                                    # filings (annual + registration
                                    # statements + amendments) sooner.
                                    contract_bearing_forms = [
                                        f for f in ISSUER_DEFAULT_FORMS
                                        if f not in ("6-K", "8-K", "8-K/A")
                                    ]
                                    contracts_pulled = (
                                        fetch_issuer_material_exhibits(
                                            client=edgar,
                                            cik=cik,
                                            output_dir=out_dir,
                                            forms=contract_bearing_forms,
                                            max_filings=max_filings_v or 30,
                                            gc_ai=gc_ai_client,
                                            gc_ai_folder_id=folder_id,
                                        )
                                    )
                                    summary["contracts"] = contracts_pulled
                                else:
                                    print("[3/4] Skipping material-contracts pull.")

                                if "recap" in steps:
                                    print(
                                        f"\n[4/4] Searching RECAP for asserted "
                                        f"claims naming '{company}'…"
                                    )
                                    cl = CourtListenerClient(cl_tok)
                                    page = cl.search_recap(
                                        company,
                                        order_by="dateFiled desc",
                                        search_type="r",
                                    )
                                    normalized = _cl_normalize(page, cl_tok)
                                    asserted = [
                                        r for r in normalized["rows"]
                                        if r.get("_claim_is_asserted")
                                        and r.get("pdf")
                                    ]
                                    print(
                                        f"  RECAP returned {len(normalized['rows'])} hit(s); "
                                        f"{len(asserted)} qualify as asserted claims with PDFs."
                                    )
                                    staged = 0
                                    for row in asserted:
                                        try:
                                            pdf_url = str(row["pdf"]).strip()
                                            basename = pdf_url.rsplit("/", 1)[-1]
                                            basename = basename.split("?", 1)[0].split("#", 1)[0] or "recap.pdf"
                                            pdf_bytes = cl.download_pdf(pdf_url)
                                            amt = format_amount(
                                                row.get("_claim_amount_raw"),
                                                row.get("_claim_currency"),
                                            ).replace(" ", "") or "Unspec"
                                            target_name = safe_filename(
                                                str(row.get("filed") or "undated"),
                                                str(row.get("court") or "RECAP").upper(),
                                                str(row.get("docket") or "no-docket")
                                                .replace(":", "_").replace("/", "_"),
                                                f"AssertedClaim_{amt}",
                                                basename,
                                            )
                                            if not target_name.lower().endswith(".pdf"):
                                                target_name += ".pdf"
                                            path = out_dir / target_name
                                            path.parent.mkdir(parents=True, exist_ok=True)
                                            path.write_bytes(pdf_bytes)
                                            if gc_ai_client is not None and folder_id:
                                                try:
                                                    gc_ai_client.upload_file(
                                                        path, folder_id=folder_id,
                                                    )
                                                except Exception as exc:
                                                    print(
                                                        f"  ! upload failed for {target_name}: {exc}"
                                                    )
                                                    continue
                                                print(
                                                    f"  -> staged + uploaded {target_name}"
                                                )
                                            else:
                                                print(
                                                    f"  -> staged locally {target_name}"
                                                )
                                            staged += 1
                                        except Exception as exc:
                                            print(
                                                f"  ! {row.get('docket', '?')}: {exc}"
                                            )
                                    summary["recap"] = staged
                                else:
                                    print("[4/4] Skipping RECAP search.")
                                # folder_id / folder_path were captured
                                # right after create_folder so partial
                                # failures still report them.
                        finally:
                            writer.flush()
                        return summary

                    try:
                        summary = await run.io_bound(do_build)
                    except Exception as exc:
                        ws_status.text = ""
                        # Surface the folder_id if create_folder succeeded
                        # so the user can clean up the orphan in GC AI.
                        orphan_msg = (
                            f" (orphan GC AI folder: {summary.get('folder_path')}, "
                            f"id={summary.get('folder_id')})"
                            if summary.get("folder_id")
                            else ""
                        )
                        ui.notify(
                            f"Workspace build failed: {exc}{orphan_msg}",
                            type="negative", multi_line=True,
                        )
                        return
                    finally:
                        if _active_scan_lock.locked():
                            try:
                                _active_scan_lock.release()
                            except RuntimeError:
                                pass
                        ws_run_btn.props(remove="loading")

                    parts = []
                    if "baseline" in steps:
                        parts.append(
                            f"{summary.get('baseline', 0)} baseline section(s)"
                        )
                    if "contracts" in steps:
                        parts.append(
                            f"{summary.get('contracts', 0)} material exhibit(s)"
                        )
                    if "recap" in steps:
                        parts.append(
                            f"{summary.get('recap', 0)} asserted claim(s)"
                        )
                    if summary.get("folder_id"):
                        ws_status.text = (
                            f"Workspace ready in GC AI folder "
                            f"{summary.get('folder_path', folder_name)}: "
                            + ", ".join(parts) + "."
                        )
                    else:
                        ws_status.text = (
                            f"Workspace built locally in {out_dir}: "
                            + ", ".join(parts)
                            + ". (No GC AI upload — add an API key and "
                            "use 'Upload selected to GC AI' on the Saved "
                            "contracts table to push the files over.)"
                        )
                    refresh_saved()

                ws_run_btn = ui.button(
                    "Build workspace", on_click=build_workspace,
                ).props("rounded color=primary unelevated").classes(
                    "w-full mt-2"
                ).style("padding: 0.75rem; font-size: 1rem;")

            # ===================================================================
            # SEC search section
            # ===================================================================
            sec_card = ui.element("div").classes("gc-card w-full")
            nav_targets["sec"] = sec_card
            with sec_card:
                ui.label("SEC search").classes("gc-section-title")

                with ui.row().classes("w-full gap-4 items-start"):
                    with ui.column().classes("flex-grow gap-3").style("min-width: 320px;"):
                        sec_phrase = ui.input(
                            label="Phrase to find", value="DEMO_ENTITY_001",
                        ).props("outlined dense rounded").classes("w-full")
                        sec_forms = ui.select(
                            options=ALL_FORMS,
                            value=list(DEFAULT_FORMS),
                            multiple=True,
                            label="Form types",
                        ).props("outlined dense rounded use-chips").classes("w-full")
                        sec_include_exhibits = ui.checkbox(
                            "Also scan exhibits attached to each filing",
                            value=True,
                        )
                    with ui.column().classes("flex-grow gap-3").style("min-width: 320px;"):
                        with ui.row().classes("gap-3 w-full"):
                            sec_start = ui.input(
                                label="Start date", placeholder="YYYY-MM-DD",
                            ).props("outlined dense rounded").classes("flex-grow")
                            sec_end = ui.input(
                                label="End date", placeholder="YYYY-MM-DD",
                            ).props("outlined dense rounded").classes("flex-grow")
                        sec_max_filings = ui.number(
                            label="Max filings (0 = no limit)",
                            value=0, min=0, step=10, format="%d",
                        ).props("outlined dense rounded").classes("w-full")
                        sec_output_dir = ui.input(
                            label="Local output folder", value="./contracts",
                        ).props("outlined dense rounded").classes("w-full")

                with ui.row().classes("gap-4 w-full items-center mt-2"):
                    sec_gc_folder_mode = ui.toggle(
                        {
                            "create": "Create new folder",
                            "existing": "Use existing folder ID",
                            "none": "Don't upload",
                        },
                        value="create",
                    ).props("inline")
                    sec_preview_mode = ui.toggle(
                        {
                            "preview": "Preview — let me pick",
                            "auto": "Save & upload everything",
                        },
                        value="preview",
                    ).props("inline")
                with ui.row().classes("gap-3 w-full"):
                    sec_gc_folder_name = ui.input(
                        label="New folder name",
                        value="DEMO_ENTITY_001 — SYNTHETIC",
                    ).props("outlined dense rounded").classes("flex-grow")
                    sec_gc_folder_id = ui.input(
                        label="Existing folder UUID",
                    ).props("outlined dense rounded").classes("flex-grow")
                sec_gc_folder_description = ui.input(
                    label="Folder description (optional, used when creating)",
                    value="Auto-uploaded by SEC contract scanner.",
                ).props("outlined dense rounded").classes("w-full")

                sec_status = ui.label("").classes("gc-section-caption mt-2")
                sec_log = ui.log(max_lines=400).classes("w-full").style(
                    "background-color: #FFFFFF; border: 1px solid #E8E1D2; "
                    "border-radius: 10px; padding: 0.75rem; "
                    "font-family: ui-monospace, monospace; font-size: 0.78rem; "
                    "max-height: 360px;"
                )

                async def run_sec_scan() -> None:
                    if not sec_user_agent.value or "@" not in sec_user_agent.value:
                        ui.notify(
                            "SEC User-Agent must include a contact email.",
                            type="negative",
                        )
                        return
                    if not sec_phrase.value.strip():
                        ui.notify("Search phrase is required.", type="negative")
                        return

                    folder_mode = sec_gc_folder_mode.value
                    preview = sec_preview_mode.value == "preview"
                    if folder_mode != "none" and not preview:
                        if not gc_ai_key.value.strip():
                            ui.notify(
                                "GC AI API key required to upload.",
                                type="negative",
                            )
                            return

                    if not _active_scan_lock.acquire(blocking=False):
                        ui.notify(
                            "Another scan is already running. Wait for it to finish.",
                            type="warning",
                        )
                        return

                    sec_run_btn.props("loading")
                    sec_status.text = "Scan running…"
                    sec_log.clear()

                    out_dir = Path((sec_output_dir.value or "").strip() or "./contracts")
                    target_dir = (
                        out_dir / STAGING_DIRNAME if preview else out_dir
                    )
                    rows_before = len(_load_manifest_records(target_dir))

                    user_agent = sec_user_agent.value.strip()
                    phrase = sec_phrase.value.strip()
                    forms_list = sec_forms.value or DEFAULT_FORMS
                    include_exh = sec_include_exhibits.value
                    start_v = sec_start.value.strip() or None
                    end_v = sec_end.value.strip() or None
                    max_filings = int(sec_max_filings.value) if sec_max_filings.value else None
                    gc_key = gc_ai_key.value.strip()
                    gc_base = gc_ai_base_url.value.strip()
                    folder_name = sec_gc_folder_name.value.strip()
                    folder_description = (sec_gc_folder_description.value or "").strip() or None
                    folder_id = sec_gc_folder_id.value.strip()

                    def do_scan() -> None:
                        writer = LogPusher(sec_log)
                        try:
                            with contextlib.redirect_stdout(writer), \
                                 contextlib.redirect_stderr(writer):
                                edgar = EdgarClient(user_agent=user_agent)
                                gc_ai_client: GCAIClient | None = None
                                effective_folder_id: str | None = None
                                if folder_mode != "none" and not preview and gc_key:
                                    gc_ai_client = GCAIClient(
                                        gc_key, base_url=gc_base or None,
                                    )
                                    if folder_mode == "existing":
                                        effective_folder_id = folder_id or None
                                    else:
                                        folder = gc_ai_client.create_folder(
                                            name=folder_name or "Contracts",
                                            description=folder_description,
                                        )
                                        effective_folder_id = folder["id"]
                                        print(
                                            f"Created GC AI folder "
                                            f"{folder.get('path', '?')} "
                                            f"(id={effective_folder_id})"
                                        )
                                scan_and_download(
                                    client=edgar,
                                    phrase=phrase,
                                    output_dir=out_dir,
                                    forms=forms_list,
                                    start=start_v,
                                    end=end_v,
                                    max_filings=max_filings,
                                    gc_ai=gc_ai_client,
                                    gc_ai_folder_id=effective_folder_id,
                                    include_exhibits=include_exh,
                                    staging_only=preview,
                                )
                        except Exception as exc:
                            print(f"\nERROR: {type(exc).__name__}: {exc}")
                        finally:
                            writer.flush()

                    try:
                        await run.io_bound(do_scan)
                    finally:
                        if _active_scan_lock.locked():
                            try:
                                _active_scan_lock.release()
                            except RuntimeError:
                                pass
                        sec_run_btn.props(remove="loading")

                    rows_after = len(_load_manifest_records(target_dir))
                    new_rows = max(0, rows_after - rows_before)
                    if new_rows > 0:
                        if preview:
                            sec_status.text = (
                                f"Scan complete. {new_rows} new candidate(s) "
                                "staged — pick which to commit below."
                            )
                        else:
                            sec_status.text = (
                                f"Scan complete. {new_rows} new contract(s) saved."
                            )
                    else:
                        sec_status.text = (
                            "Scan complete — 0 new contracts. Every match "
                            "was either already saved (Saved contracts "
                            "below) or filtered out as boilerplate / "
                            "metadata-only."
                        )
                    refresh_staging()
                    refresh_saved()

                sec_run_btn = ui.button(
                    "Run scan", on_click=run_sec_scan,
                ).props("rounded color=primary unelevated").classes(
                    "w-full mt-2"
                ).style("padding: 0.7rem; font-size: 1rem;")

            # ===================================================================
            # Disclosure baseline (Risk Factors / MD&A / Financial Statements)
            # ===================================================================
            # The Skill running in GC AI requires Risk Factors as its
            # baseline for Item 105 testing. This card pulls the canonical
            # sections from a company's latest 10-K / 20-F so they sit in
            # the GC AI folder alongside the contracts the Skill maps them
            # against.
            baseline_card = ui.element("div").classes("gc-card w-full")
            nav_targets["baseline"] = baseline_card
            with baseline_card:
                ui.label("Disclosure baseline").classes("gc-section-title")
                ui.label(
                    "Pull Risk Factors, MD&A, and Financial Statements from "
                    "the issuer's latest annual report. The GC AI Skill uses "
                    "these as the REQUIRED BASELINE for Item 105 disclosure-"
                    "adequacy testing — without them, the Skill is starting "
                    "blind."
                ).classes("gc-section-caption")

                with ui.row().classes("w-full gap-4 items-start"):
                    with ui.column().classes("flex-grow gap-3").style(
                        "min-width: 320px;"
                    ):
                        baseline_cik = ui.input(
                            label="Issuer CIK",
                            placeholder="e.g. 0000000000 (synthetic placeholder)",
                        ).props("outlined dense rounded").classes("w-full")
                        baseline_forms = ui.select(
                            options=["10-K", "20-F", "10-Q", "10-K/A", "20-F/A"],
                            value=["10-K", "20-F"],
                            multiple=True,
                            label="Form types (newest match wins)",
                        ).props(
                            "outlined dense rounded use-chips"
                        ).classes("w-full")
                    with ui.column().classes("flex-grow gap-3").style(
                        "min-width: 320px;"
                    ):
                        baseline_sections = ui.select(
                            options={
                                "RiskFactors": "Risk Factors (REQUIRED)",
                                "MDA": "Management's Discussion & Analysis",
                                "FinancialStatements": "Financial Statements",
                            },
                            value=["RiskFactors", "MDA", "FinancialStatements"],
                            multiple=True,
                            label="Sections to extract",
                        ).props(
                            "outlined dense rounded use-chips"
                        ).classes("w-full")
                        baseline_upload_to_gc = ui.toggle(
                            {
                                "create": "Create new GC AI folder",
                                "existing": "Use existing folder ID",
                                "none": "Local save only",
                            },
                            value="none",
                        ).props("inline")

                baseline_status = ui.label("").classes("gc-section-caption")
                baseline_log = ui.log(max_lines=200).classes("w-full").style(
                    "background-color: #FFFFFF; border: 1px solid #E8E1D2; "
                    "border-radius: 10px; padding: 0.75rem; "
                    "font-family: ui-monospace, monospace; font-size: 0.78rem; "
                    "max-height: 240px;"
                )

                async def run_baseline_pull() -> None:
                    cik = (baseline_cik.value or "").strip()
                    if not cik:
                        ui.notify("Issuer CIK is required.", type="warning")
                        return
                    if not sec_user_agent.value or "@" not in sec_user_agent.value:
                        ui.notify(
                            "SEC User-Agent must include a contact email.",
                            type="negative",
                        )
                        return

                    upload_mode = baseline_upload_to_gc.value
                    out_dir = Path(
                        (sec_output_dir.value or "").strip() or "./contracts"
                    )
                    user_agent = sec_user_agent.value.strip()
                    forms_list = list(baseline_forms.value or ["10-K", "20-F"])
                    section_keys = list(
                        baseline_sections.value
                        or ["RiskFactors", "MDA", "FinancialStatements"]
                    )
                    gc_key = (gc_ai_key.value or "").strip()
                    gc_base = (gc_ai_base_url.value or "").strip()
                    folder_name = (sec_gc_folder_name.value or "").strip()
                    folder_id = (sec_gc_folder_id.value or "").strip()
                    folder_description = (
                        sec_gc_folder_description.value or ""
                    ).strip() or None

                    if upload_mode != "none":
                        if not gc_key:
                            ui.notify(
                                "GC AI key required to upload.", type="negative"
                            )
                            return
                        if upload_mode == "existing" and not folder_id:
                            ui.notify(
                                "Folder UUID required when using existing folder.",
                                type="negative",
                            )
                            return

                    baseline_run_btn.props("loading")
                    baseline_status.text = "Fetching from EDGAR…"
                    baseline_log.clear()

                    def do_pull() -> list[dict]:
                        writer = LogPusher(baseline_log)
                        try:
                            with contextlib.redirect_stdout(writer), \
                                 contextlib.redirect_stderr(writer):
                                edgar = EdgarClient(user_agent=user_agent)
                                gc_ai_client: GCAIClient | None = None
                                effective_folder_id: str | None = None
                                if upload_mode != "none" and gc_key:
                                    gc_ai_client = GCAIClient(
                                        gc_key, base_url=gc_base or None,
                                    )
                                    if upload_mode == "existing":
                                        effective_folder_id = folder_id
                                    else:
                                        folder = gc_ai_client.create_folder(
                                            name=(
                                                folder_name
                                                or f"Disclosure Baseline {cik}"
                                            ),
                                            description=folder_description,
                                        )
                                        effective_folder_id = folder["id"]
                                        print(
                                            f"Created GC AI folder "
                                            f"{folder.get('path', '?')} "
                                            f"(id={effective_folder_id})"
                                        )
                                return fetch_and_save_disclosure_sections(
                                    client=edgar,
                                    cik=cik,
                                    output_dir=out_dir,
                                    forms=forms_list,
                                    section_keys=section_keys,
                                    gc_ai=gc_ai_client,
                                    gc_ai_folder_id=effective_folder_id,
                                )
                        finally:
                            writer.flush()

                    try:
                        records = await run.io_bound(do_pull)
                    except Exception as exc:
                        baseline_status.text = ""
                        ui.notify(
                            f"Baseline pull failed: {exc}",
                            type="negative", multi_line=True,
                        )
                        return
                    finally:
                        baseline_run_btn.props(remove="loading")

                    if records:
                        names = ", ".join(r["section"] for r in records)
                        baseline_status.text = (
                            f"Saved {len(records)} section(s): {names}. "
                            "These now sit in your output folder (and GC AI "
                            "folder, if uploading) ready for the Skill."
                        )
                        refresh_saved()
                    else:
                        baseline_status.text = (
                            "No sections extracted. Verify the CIK and form "
                            "types, or check the log above."
                        )

                baseline_run_btn = ui.button(
                    "Pull baseline from EDGAR", on_click=run_baseline_pull,
                ).props("rounded color=primary unelevated").classes(
                    "w-full mt-2"
                ).style("padding: 0.7rem; font-size: 1rem;")

            # ===================================================================
            # Staging picker
            # ===================================================================
            with ui.element("div").classes("gc-card w-full"):
                with ui.row().classes("w-full justify-between items-center"):
                    ui.label("Staged candidates").classes("gc-section-title")
                    staging_count = ui.label("0 candidate(s) staged").classes(
                        "gc-section-caption"
                    )
                ui.label(
                    "Tick rows to commit. 'Save selected locally' moves "
                    "files from _staging/ into the output folder; "
                    "'Save & upload' additionally pushes them into your "
                    "GC AI folder."
                ).classes("gc-section-caption")

                staging_table = ui.table(
                    columns=[
                        {"name": "filed", "label": "Filed", "field": "filed", "align": "left", "sortable": True},
                        {"name": "form", "label": "Form", "field": "form", "align": "left"},
                        {"name": "exhibit", "label": "Exhibit", "field": "exhibit", "align": "left"},
                        {"name": "company", "label": "Company", "field": "company", "align": "left"},
                        {"name": "document", "label": "Document", "field": "document", "align": "left"},
                        {"name": "source", "label": "Source", "field": "source", "align": "left"},
                    ],
                    rows=[],
                    row_key="saved_as",
                    selection="multiple",
                    pagination=15,
                ).classes("w-full")

                def refresh_staging() -> None:
                    out_dir = Path((sec_output_dir.value or "").strip() or "./contracts")
                    rows = _load_manifest_records(out_dir / STAGING_DIRNAME)
                    staging_table.rows = rows
                    staging_table.update()
                    staging_count.text = f"{len(rows)} candidate(s) staged"

                async def commit_staged(upload: bool) -> None:
                    selected = staging_table.selected or []
                    if not selected:
                        ui.notify("Pick at least one row.", type="warning")
                        return
                    out_dir = Path((sec_output_dir.value or "").strip() or "./contracts")
                    staging_dir = out_dir / STAGING_DIRNAME
                    gc_ai_client: GCAIClient | None = None
                    gc_folder_id: str | None = None
                    if upload:
                        if sec_gc_folder_mode.value == "none":
                            ui.notify(
                                "Set GC AI folder mode to 'Create new folder' "
                                "or 'Use existing folder ID' before uploading.",
                                type="negative", multi_line=True,
                            )
                            return
                        if not gc_ai_key.value.strip():
                            ui.notify(
                                "GC AI key required to upload.", type="negative"
                            )
                            return
                        if sec_gc_folder_mode.value == "existing":
                            existing_id = (sec_gc_folder_id.value or "").strip()
                            if not existing_id:
                                ui.notify(
                                    "Folder UUID required when using existing folder.",
                                    type="negative",
                                )
                                return
                        try:
                            gc_ai_client = GCAIClient(
                                gc_ai_key.value.strip(),
                                base_url=gc_ai_base_url.value.strip() or None,
                            )
                            if sec_gc_folder_mode.value == "existing":
                                gc_folder_id = existing_id
                            else:
                                folder = gc_ai_client.create_folder(
                                    name=sec_gc_folder_name.value.strip()
                                    or "Staged Contracts",
                                    description=(
                                        sec_gc_folder_description.value or ""
                                    ).strip() or None,
                                )
                                gc_folder_id = folder["id"]
                                ui.notify(
                                    f"GC AI folder created: "
                                    f"{folder.get('path', '?')}"
                                )
                        except Exception as exc:
                            ui.notify(
                                f"GC AI setup failed: {exc}", type="negative"
                            )
                            return

                    selected_records = list(selected)

                    def do_commit() -> dict:
                        return commit_selection(
                            staging_dir=staging_dir,
                            output_dir=out_dir,
                            selected_records=selected_records,
                            gc_ai=gc_ai_client,
                            gc_ai_folder_id=gc_folder_id,
                        )

                    result = await run.io_bound(do_commit)
                    msg = f"Saved {result['moved']} file(s)."
                    if upload:
                        msg += f" Uploaded {result['uploaded']} to GC AI."
                    if result["failed"]:
                        msg += f" {len(result['failed'])} failure(s)."
                        ui.notify(msg, type="warning", multi_line=True)
                    else:
                        ui.notify(msg, type="positive")
                    refresh_staging()
                    refresh_saved()

                def clear_staging() -> None:
                    out_dir = Path((sec_output_dir.value or "").strip() or "./contracts")
                    staging_dir = out_dir / STAGING_DIRNAME
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir)
                    ui.notify(f"Cleared {staging_dir}")
                    refresh_staging()

                with ui.row().classes("gap-2 mt-3"):
                    ui.button(
                        "Save selected locally",
                        on_click=lambda: commit_staged(upload=False),
                    ).props("rounded color=primary unelevated")
                    ui.button(
                        "Save & upload selected to GC AI",
                        on_click=lambda: commit_staged(upload=True),
                    ).props("rounded color=primary unelevated")
                    ui.button(
                        "Clear all staging", on_click=clear_staging,
                    ).props("rounded outline color=primary")

            # ===================================================================
            # Saved contracts (with re-upload to GC AI)
            # ===================================================================
            saved_card = ui.element("div").classes("gc-card w-full")
            nav_targets["saved"] = saved_card
            with saved_card:
                with ui.row().classes("w-full justify-between items-center"):
                    ui.label("Saved contracts").classes("gc-section-title")
                    saved_count = ui.label("0 contract(s) saved").classes(
                        "gc-section-caption"
                    )
                ui.label(
                    "Tick rows below to re-upload existing contracts to "
                    "GC AI — useful when a previous upload failed or you "
                    "want to send already-saved contracts to a new folder."
                ).classes("gc-section-caption")

                saved_table = ui.table(
                    columns=[
                        {"name": "filed", "label": "Filed", "field": "filed", "sortable": True},
                        {"name": "form", "label": "Form", "field": "form"},
                        {"name": "exhibit", "label": "Exhibit", "field": "exhibit"},
                        {"name": "company", "label": "Company", "field": "company", "sortable": True},
                        {"name": "document", "label": "Document", "field": "document"},
                        {"name": "source", "label": "Source", "field": "source"},
                        {"name": "gc_ai_status", "label": "GC AI status", "field": "gc_ai_status"},
                    ],
                    rows=[],
                    row_key="saved_as",
                    selection="multiple",
                    pagination=15,
                ).classes("w-full")

                def refresh_saved() -> None:
                    out_dir = Path((sec_output_dir.value or "").strip() or "./contracts")
                    rows = _load_manifest_records(out_dir)
                    saved_table.rows = rows
                    saved_table.update()
                    saved_count.text = f"{len(rows)} contract(s) saved"

                async def reupload_saved() -> None:
                    selected = saved_table.selected or []
                    if not selected:
                        ui.notify("Pick at least one row.", type="warning")
                        return
                    if not gc_ai_key.value.strip():
                        ui.notify("GC AI key required.", type="negative")
                        return
                    folder_mode = sec_gc_folder_mode.value
                    if folder_mode == "none":
                        ui.notify(
                            "Set GC AI folder mode to 'Create new folder' "
                            "or 'Use existing folder ID' before uploading.",
                            type="negative", multi_line=True,
                        )
                        return
                    folder_id = (sec_gc_folder_id.value or "").strip()
                    if folder_mode == "existing" and not folder_id:
                        ui.notify(
                            "Folder UUID required when using existing folder.",
                            type="negative",
                        )
                        return

                    out_dir = Path((sec_output_dir.value or "").strip() or "./contracts")
                    selected_records = list(selected)
                    gc_key = gc_ai_key.value.strip()
                    gc_base = gc_ai_base_url.value.strip()
                    folder_name = sec_gc_folder_name.value.strip()
                    folder_description = (sec_gc_folder_description.value or "").strip() or None

                    def do_reupload() -> tuple[int, list[str]]:
                        gc = GCAIClient(gc_key, base_url=gc_base or None)
                        if folder_mode == "existing":
                            fid = folder_id
                        else:
                            folder = gc.create_folder(
                                name=folder_name or "Re-uploaded Contracts",
                                description=folder_description,
                            )
                            fid = folder["id"]
                        uploaded = 0
                        failed: list[str] = []
                        for row in selected_records:
                            saved_as = row.get("saved_as")
                            if not saved_as:
                                failed.append(
                                    f"{row.get('document', '?')} (no saved_as)"
                                )
                                continue
                            path = out_dir / str(saved_as)
                            if not path.exists():
                                failed.append(f"{saved_as} (file missing)")
                                continue
                            try:
                                gc.upload_file(path, folder_id=fid)
                                uploaded += 1
                            except Exception as exc:
                                failed.append(f"{saved_as} ({exc})")
                        return uploaded, failed

                    try:
                        uploaded, failed = await run.io_bound(do_reupload)
                    except Exception as exc:
                        ui.notify(f"GC AI error: {exc}", type="negative")
                        return
                    msg = f"Re-uploaded {uploaded} file(s) to GC AI."
                    if failed:
                        msg += f" {len(failed)} failure(s): " + "; ".join(failed[:3])
                        ui.notify(msg, type="warning", multi_line=True)
                    else:
                        ui.notify(msg, type="positive")

                ui.button(
                    "Upload selected to GC AI", on_click=reupload_saved,
                ).props("rounded color=primary unelevated").classes("mt-3")

            # ===================================================================
            # PACER Case Locator search
            # ===================================================================
            with ui.element("div").classes("gc-card w-full"):
                ui.label("PACER Case Locator search").classes("gc-section-title")
                ui.label(
                    "Log in to PACER in the sidebar before searching. QA is "
                    "free; Production charges $0.10 per search page (~54 hits)."
                ).classes("gc-section-caption")

                pcl_search_type = ui.toggle(
                    {"party": "Party / entity name", "case": "Case (title or number)"},
                    value="party",
                ).props("inline")

                with ui.row().classes("w-full gap-4 items-start"):
                    with ui.column().classes("flex-grow gap-3").style("min-width: 320px;"):
                        pcl_party_name = ui.input(
                            label="Party / entity name", value="DEMO_ENTITY_001",
                        ).props("outlined dense rounded").classes("w-full")
                        pcl_first_name = ui.input(
                            label="First name (individuals only)",
                        ).props("outlined dense rounded").classes("w-full")
                        pcl_exact_match = ui.checkbox("Exact name match", value=False)
                        pcl_case_title = ui.input(
                            label="Case title contains",
                        ).props("outlined dense rounded").classes("w-full")
                        pcl_case_number = ui.input(
                            label="Case number (full or partial)",
                        ).props("outlined dense rounded").classes("w-full")
                    with ui.column().classes("flex-grow gap-3").style("min-width: 320px;"):
                        pcl_jurisdiction = ui.select(
                            options={
                                "": "Any", "bk": "Bankruptcy", "cv": "Civil",
                                "cr": "Criminal", "ap": "Appellate", "mdl": "MDL",
                            },
                            value="",
                            label="Jurisdiction",
                        ).props("outlined dense rounded").classes("w-full")
                        pcl_court_ids = ui.input(
                            label="Court IDs (comma-separated)",
                            placeholder="nysdc,cacdc,ilndc",
                        ).props("outlined dense rounded").classes("w-full")
                        with ui.row().classes("gap-3 w-full"):
                            pcl_date_from = ui.input(
                                label="Filed from", placeholder="YYYY-MM-DD",
                            ).props("outlined dense rounded").classes("flex-grow")
                            pcl_date_to = ui.input(
                                label="Filed to", placeholder="YYYY-MM-DD",
                            ).props("outlined dense rounded").classes("flex-grow")
                        pcl_page = ui.number(
                            label="Page (0 = first; 54 hits/page)",
                            value=0, min=0, step=1, format="%d",
                        ).props("outlined dense rounded").classes("w-full")

                pcl_confirm_billing = ui.checkbox(
                    "I confirm this Production search will be billed $0.10",
                    value=False,
                )
                pcl_status = ui.label("").classes("gc-section-caption")
                pcl_table = ui.table(
                    columns=[
                        {"name": "filed", "label": "Filed", "field": "filed", "sortable": True},
                        {"name": "court", "label": "Court", "field": "court"},
                        {"name": "case_number", "label": "Case #", "field": "case_number"},
                        {"name": "case_title", "label": "Case title", "field": "case_title"},
                        {"name": "party", "label": "Party", "field": "party"},
                        {"name": "role", "label": "Role", "field": "role"},
                        {"name": "jurisdiction", "label": "Jur.", "field": "jurisdiction"},
                        {"name": "chapter", "label": "Ch.", "field": "chapter"},
                        {"name": "case_link", "label": "CM/ECF", "field": "case_link"},
                    ],
                    rows=[],
                    pagination=15,
                ).classes("w-full")

                async def run_pcl_search() -> None:
                    sess = state["pacer"]
                    if not sess:
                        ui.notify(
                            "Log in to PACER in the sidebar first.", type="warning",
                        )
                        return
                    if sess["env"] == "production" and not pcl_confirm_billing.value:
                        ui.notify(
                            "Tick the billing-confirmation box for Production.",
                            type="warning",
                        )
                        return

                    court_list = [
                        c.strip() for c in (pcl_court_ids.value or "").split(",")
                        if c.strip()
                    ]
                    case_filters: dict = {}
                    if pcl_jurisdiction.value:
                        case_filters["jurisdictionType"] = pcl_jurisdiction.value
                    if court_list:
                        case_filters["courtId"] = court_list
                    if pcl_date_from.value.strip():
                        case_filters["dateFiledFrom"] = pcl_date_from.value.strip()
                    if pcl_date_to.value.strip():
                        case_filters["dateFiledTo"] = pcl_date_to.value.strip()

                    is_party = pcl_search_type.value == "party"
                    if is_party:
                        last_name = (pcl_party_name.value or "").strip()
                        if not last_name:
                            ui.notify(
                                "Party name is required for a party search.",
                                type="warning",
                            )
                            return
                        criteria: dict = {
                            "lastName": last_name,
                            "firstName": (pcl_first_name.value or "").strip() or None,
                            "exactNameMatch": bool(pcl_exact_match.value),
                            "courtCase": case_filters or None,
                        }
                    else:
                        case_title = (pcl_case_title.value or "").strip() or None
                        case_number = (pcl_case_number.value or "").strip() or None
                        if not case_title and not case_number:
                            ui.notify(
                                "Provide a case title or case number.",
                                type="warning",
                            )
                            return
                        criteria = {
                            "caseTitle": case_title,
                            "caseNumberFull": case_number,
                            **case_filters,
                        }

                    token = sess["token"]
                    env = sess["env"]
                    client_code = sess.get("client_code")
                    page_n = int(pcl_page.value or 0)

                    def do_search() -> Any:
                        client = PCLClient(
                            token=token, environment=env, client_code=client_code,
                        )
                        if is_party:
                            return client.search_parties(criteria, page=page_n)
                        return client.search_cases(criteria, page=page_n)

                    pcl_run_btn.props("loading")
                    pcl_status.text = "Calling PACER…"
                    try:
                        result = await run.io_bound(do_search)
                    except PCLError as exc:
                        pcl_status.text = ""
                        ui.notify(
                            f"PACER search failed ({exc.status_code}): {exc}",
                            type="negative", multi_line=True,
                        )
                        if exc.status_code == 401:
                            state["pacer"] = None
                            _render_pacer_status()
                        return
                    except Exception as exc:
                        pcl_status.text = ""
                        ui.notify(f"PACER request error: {exc}", type="negative")
                        return
                    finally:
                        pcl_run_btn.props(remove="loading")

                    if result.new_token:
                        sess["token"] = result.new_token
                    fee = result.search_fee_dollars
                    sess["cost"] = sess.get("cost", 0.0) + fee
                    _render_pacer_status()

                    page_info = result.page_info or {}
                    total = page_info.get("totalElements", len(result.content))
                    shown = len(result.content)
                    page_num = page_info.get("number", 0)
                    total_pages = page_info.get("totalPages", 1)
                    pcl_status.text = (
                        f"PACER returned {shown} hit(s) on page {page_num + 1} "
                        f"of {total_pages} (total: {total}). "
                        f"Search fee: ${fee:.2f}."
                    )

                    rows = []
                    for hit in result.content:
                        cc = hit.get("courtCase") or {}
                        case_link = cc.get("caseLink") or hit.get("caseLink")
                        rows.append({
                            "filed": hit.get("dateFiled") or cc.get("dateFiled"),
                            "court": cc.get("courtId") or hit.get("courtId"),
                            "case_number": (
                                cc.get("caseNumberFull") or hit.get("caseNumberFull")
                            ),
                            "case_title": (
                                cc.get("caseTitle") or hit.get("caseTitle")
                            ),
                            "party": " ".join(
                                x for x in (
                                    hit.get("firstName"), hit.get("middleName"),
                                    hit.get("lastName"), hit.get("generation"),
                                ) if x and x.strip()
                            ).strip() or None,
                            "role": hit.get("partyRole"),
                            "jurisdiction": (
                                hit.get("jurisdictionType")
                                or cc.get("jurisdictionType")
                            ),
                            "chapter": (
                                hit.get("bankruptcyChapter")
                                or cc.get("bankruptcyChapter")
                            ),
                            "case_link": case_link or "",
                        })
                    pcl_table.rows = rows
                    pcl_table.update()

                pcl_run_btn = ui.button(
                    "Run PACER search", on_click=run_pcl_search,
                ).props("rounded color=primary unelevated").classes("w-full mt-3")

            # ===================================================================
            # CourtListener / RECAP search
            # ===================================================================
            with ui.element("div").classes("gc-card w-full"):
                ui.label("CourtListener / RECAP search (free)").classes(
                    "gc-section-title"
                )
                ui.label(
                    "Federal court documents previously fetched from PACER, "
                    "mirrored for free. Stage matching PDFs into the staging "
                    "picker above; pray for documents not yet in RECAP."
                ).classes("gc-section-caption")

                with ui.row().classes("w-full gap-4 items-start"):
                    with ui.column().classes("flex-grow gap-3").style("min-width: 320px;"):
                        cl_query = ui.input(
                            label="Phrase / query", value="DEMO_ENTITY_001",
                        ).props("outlined dense rounded").classes("w-full")
                        cl_party = ui.input(
                            label="Party name (optional)",
                        ).props("outlined dense rounded").classes("w-full")
                        cl_search_granularity = ui.toggle(
                            {"r": "Dockets", "rd": "Documents"},
                            value="r",
                        ).props("inline")
                    with ui.column().classes("flex-grow gap-3").style("min-width: 320px;"):
                        cl_courts_raw = ui.input(
                            label="Court IDs (comma-separated)",
                            placeholder="nysd,cacd,ilnd",
                        ).props("outlined dense rounded").classes("w-full")
                        with ui.row().classes("gap-3 w-full"):
                            cl_after = ui.input(
                                label="Filed after", placeholder="YYYY-MM-DD",
                            ).props("outlined dense rounded").classes("flex-grow")
                            cl_before = ui.input(
                                label="Filed before", placeholder="YYYY-MM-DD",
                            ).props("outlined dense rounded").classes("flex-grow")
                        cl_order_by = ui.select(
                            options={
                                "": "Relevance",
                                "dateFiled desc": "Date filed (newest first)",
                                "dateFiled asc": "Date filed (oldest first)",
                                "entry_date_filed desc": "Entry date filed (newest first)",
                            },
                            value="",
                            label="Order by",
                        ).props("outlined dense rounded").classes("w-full")

                cl_status = ui.label("").classes("gc-section-caption")
                cl_triage = ui.toggle(
                    {
                        "all": "All",
                        "asserted_claim": "🔴 Asserted claims",
                        "contract": "Contracts",
                        "lawsuit": "Lawsuits",
                        "other": "Other",
                    },
                    value="all",
                ).props("inline")
                cl_table = ui.table(
                    columns=[
                        {"name": "kind", "label": "Kind", "field": "kind"},
                        {"name": "claim_amount", "label": "Claim", "field": "claim_amount"},
                        {"name": "filed", "label": "Filed", "field": "filed", "sortable": True},
                        {"name": "court", "label": "Court", "field": "court"},
                        {"name": "case", "label": "Case", "field": "case"},
                        {"name": "docket", "label": "Docket", "field": "docket"},
                        {"name": "description", "label": "Description", "field": "description"},
                        {"name": "doc_type", "label": "Doc type", "field": "doc_type"},
                        {"name": "snippet", "label": "Snippet", "field": "snippet"},
                        {"name": "courtlistener", "label": "CL link", "field": "courtlistener"},
                        {"name": "pdf", "label": "PDF", "field": "pdf"},
                    ],
                    rows=[],
                    row_key="row_id",
                    selection="multiple",
                    pagination=15,
                ).classes("w-full")

                def _cl_render_table() -> None:
                    res = state["cl_last_result"]
                    if not res:
                        cl_table.rows = []
                        cl_table.selected = []
                        cl_table.update()
                        return
                    all_rows = res.get("rows", [])
                    flt = cl_triage.value
                    if flt == "all":
                        visible = all_rows
                    elif flt == "other":
                        visible = [r for r in all_rows if r.get("kind") == ""]
                    else:
                        visible = [r for r in all_rows if r.get("kind") == flt]
                    cl_table.rows = visible
                    cl_table.selected = []
                    cl_table.update()

                cl_triage.on_value_change(lambda _e: _cl_render_table())

                async def run_cl_search() -> None:
                    if not cl_token.value.strip():
                        ui.notify(
                            "Add a CourtListener API token in the sidebar.",
                            type="warning",
                        )
                        return
                    token = cl_token.value.strip()
                    query = (cl_query.value or "").strip()
                    if not query:
                        ui.notify("Enter a query.", type="warning")
                        return
                    courts = [
                        c.strip() for c in (cl_courts_raw.value or "").split(",")
                        if c.strip()
                    ]
                    after = (cl_after.value or "").strip() or None
                    before = (cl_before.value or "").strip() or None
                    party = (cl_party.value or "").strip() or None
                    order_by = cl_order_by.value or None
                    search_type = cl_search_granularity.value

                    def do_search() -> Any:
                        client = CourtListenerClient(token)
                        return client.search_recap(
                            query,
                            court_ids=courts or None,
                            filed_after=after, filed_before=before,
                            party_name=party, order_by=order_by,
                            search_type=search_type,
                        )

                    cl_run_btn.props("loading")
                    cl_status.text = "Calling CourtListener…"
                    try:
                        result = await run.io_bound(do_search)
                    except CourtListenerError as exc:
                        ui.notify(
                            f"CourtListener error ({exc.status_code}): {exc}",
                            type="negative", multi_line=True,
                        )
                        cl_status.text = ""
                        return
                    except Exception as exc:
                        ui.notify(f"CourtListener request error: {exc}", type="negative")
                        cl_status.text = ""
                        return
                    finally:
                        cl_run_btn.props(remove="loading")

                    state["cl_last_result"] = _cl_normalize(result, token)
                    res = state["cl_last_result"]
                    n = len(res["rows"])
                    total = result.count
                    count_label = f"about {total:,}" if total > 2000 else f"{total:,}"
                    cl_status.text = (
                        f"RECAP page returned {n} hit(s) (total: {count_label})."
                        + (" Total has ±6% error above 2,000 results." if total > 2000 else "")
                    )
                    _cl_render_table()

                async def cl_stage_selected() -> None:
                    res = state["cl_last_result"]
                    if not res:
                        ui.notify("Run a search first.", type="warning")
                        return
                    selected = cl_table.selected or []
                    rows_with_pdf = [
                        r for r in selected
                        if r.get("pdf") and str(r.get("pdf")).strip()
                    ]
                    if not rows_with_pdf:
                        ui.notify(
                            "Tick rows with a PDF link to stage.", type="warning",
                        )
                        return
                    out_dir = Path((sec_output_dir.value or "").strip() or "./contracts")
                    staging = out_dir / STAGING_DIRNAME
                    staging.mkdir(parents=True, exist_ok=True)
                    manifest_path = staging / "manifest.jsonl"
                    token = res["token"]
                    rows_snapshot = list(rows_with_pdf)

                    def do_stage() -> tuple[int, list[str]]:
                        cl_client = CourtListenerClient(token)
                        staged = 0
                        failures: list[str] = []
                        with manifest_path.open("a", encoding="utf-8") as mf:
                            for row in rows_snapshot:
                                pdf_url = str(row["pdf"]).strip()
                                # Strip query string / fragment so the
                                # filename doesn't end up "doc.pdf?download=1"
                                basename = pdf_url.rsplit("/", 1)[-1]
                                basename = basename.split("?", 1)[0].split("#", 1)[0]
                                original_name = basename or "recap.pdf"
                                try:
                                    pdf_bytes = cl_client.download_pdf(pdf_url)
                                except CourtListenerError as exc:
                                    failures.append(f"{original_name} ({exc})")
                                    continue
                                except Exception as exc:
                                    failures.append(
                                        f"{original_name} (network: {exc})"
                                    )
                                    continue
                                docket_safe = (
                                    str(row.get("docket") or "")
                                    .replace(":", "_").replace("/", "_")
                                )
                                # When the row carries structured claim
                                # metadata, embed an "AssertedClaim_<amount>"
                                # marker in the filename so the GC AI Skill
                                # picks up the document type from the
                                # filename. Otherwise fall back to the
                                # original RECAP basename.
                                asserted = bool(row.get("_claim_is_asserted"))
                                amount_tag = ""
                                if asserted:
                                    amt = format_amount(
                                        row.get("_claim_amount_raw"),
                                        row.get("_claim_currency"),
                                    ).replace(" ", "") or "Unspec"
                                    amount_tag = f"AssertedClaim_{amt}"
                                target_name = safe_filename(
                                    str(row.get("filed") or "undated"),
                                    str(row.get("court") or "RECAP").upper(),
                                    docket_safe or "no-docket",
                                    amount_tag or original_name,
                                    original_name if amount_tag else "",
                                )
                                if not target_name.lower().endswith(".pdf"):
                                    target_name += ".pdf"
                                (staging / target_name).write_bytes(pdf_bytes)
                                record = {
                                    "source": "recap",
                                    "accession": str(row.get("docket") or original_name),
                                    "cik": "",
                                    "form": "RECAP",
                                    "filed": row.get("filed"),
                                    "company": row.get("case"),
                                    "document": original_name,
                                    "exhibit": "recap",
                                    "url": pdf_url,
                                    "saved_as": target_name,
                                    "court": row.get("court"),
                                    # Structured claim metadata for the
                                    # Skill's Asserted Claim Materiality
                                    # Rule (Item 105 escalation trigger).
                                    "is_asserted_claim": asserted,
                                    "claim_amount": row.get("_claim_amount_raw"),
                                    "claim_currency": row.get("_claim_currency"),
                                    "claim_claimant": row.get("_claim_claimant"),
                                    "claim_legal_basis": row.get("_claim_legal_basis"),
                                    "claim_document_type": row.get("_claim_document_type"),
                                }
                                clean = {
                                    k: v for k, v in record.items()
                                    # Drop falsy values EXCEPT preserve
                                    # is_asserted_claim=False explicitly so
                                    # downstream consumers can distinguish
                                    # "tested, not asserted" from "untested".
                                    if v not in (None, "", [])
                                    or k == "is_asserted_claim"
                                }
                                _append_manifest_line(
                                    mf, json.dumps(clean) + "\n"
                                )
                                staged += 1
                        return staged, failures

                    cl_stage_btn.props("loading")
                    try:
                        staged, failures = await run.io_bound(do_stage)
                    finally:
                        cl_stage_btn.props(remove="loading")
                    if staged:
                        ui.notify(
                            f"Staged {staged} PDF(s). Scroll up to "
                            "'Staged candidates' to commit.",
                            type="positive", multi_line=True,
                        )
                    if failures:
                        ui.notify(
                            f"{len(failures)} download failure(s): "
                            + "; ".join(failures[:3])
                            + ("…" if len(failures) > 3 else ""),
                            type="warning", multi_line=True,
                        )
                    refresh_staging()

                async def cl_pray_selected() -> None:
                    res = state["cl_last_result"]
                    if not res:
                        ui.notify("Run a search first.", type="warning")
                        return
                    selected = cl_table.selected or []
                    prayable = [
                        r for r in selected
                        if (not r.get("pdf") or not str(r.get("pdf")).strip())
                        and r.get("recap_doc_id")
                    ]
                    if not prayable:
                        ui.notify(
                            "Tick rows with no PDF and a known recap doc id.",
                            type="warning",
                        )
                        return
                    token = res["token"]
                    rows_snapshot = list(prayable)

                    def do_pray() -> tuple[int, int, list[str]]:
                        cl_client = CourtListenerClient(token)
                        created = 0
                        already = 0
                        failures: list[str] = []
                        for row in rows_snapshot:
                            rdid = str(row["recap_doc_id"]).strip()
                            try:
                                resp = cl_client.create_prayer(rdid)
                                if resp.get("status") == 1:
                                    created += 1
                                else:
                                    already += 1
                            except CourtListenerError as exc:
                                label = str(row.get("docket") or rdid)
                                failures.append(f"{label} ({exc})")
                            except Exception as exc:
                                failures.append(f"{rdid} (network: {exc})")
                        return created, already, failures

                    cl_pray_btn.props("loading")
                    try:
                        created, already, failures = await run.io_bound(do_pray)
                    finally:
                        cl_pray_btn.props(remove="loading")
                    parts: list[str] = []
                    if created:
                        parts.append(f"created {created} prayer(s)")
                    if already:
                        parts.append(f"{already} already prayed for")
                    if parts:
                        ui.notify(
                            "Pray-and-Pay: " + ", ".join(parts)
                            + ". CourtListener will email you when "
                            "the document becomes available.",
                            type="positive", multi_line=True,
                        )
                    if failures:
                        ui.notify(
                            f"{len(failures)} prayer(s) rejected (likely quota): "
                            + "; ".join(failures[:3])
                            + ("…" if len(failures) > 3 else ""),
                            type="warning", multi_line=True,
                        )

                async def cl_next_page() -> None:
                    res = state["cl_last_result"]
                    if not res or not res.get("has_next"):
                        ui.notify("No next page.", type="info")
                        return
                    token = res["token"]
                    raw = res["raw"]

                    def do_next() -> Any:
                        return CourtListenerClient(token).next_page(raw)

                    try:
                        new_raw = await run.io_bound(do_next)
                    except CourtListenerError as exc:
                        ui.notify(
                            f"CourtListener error ({exc.status_code}): {exc}",
                            type="negative",
                        )
                        return
                    state["cl_last_result"] = _cl_normalize(new_raw, token)
                    _cl_render_table()

                def cl_clear() -> None:
                    state["cl_last_result"] = None
                    cl_status.text = ""
                    _cl_render_table()

                with ui.row().classes("gap-2 mt-3 w-full"):
                    cl_run_btn = ui.button(
                        "Search RECAP", on_click=run_cl_search,
                    ).props("rounded color=primary unelevated").classes("flex-grow")
                    cl_stage_btn = ui.button(
                        "Stage selected PDFs", on_click=cl_stage_selected,
                    ).props("rounded color=primary unelevated").classes("flex-grow")
                    cl_pray_btn = ui.button(
                        "Pray for selected (no PDF)", on_click=cl_pray_selected,
                    ).props("rounded outline color=primary").classes("flex-grow")
                    ui.button(
                        "Next page", on_click=cl_next_page,
                    ).props("rounded outline color=primary").classes("flex-grow")
                    ui.button(
                        "Clear results", on_click=cl_clear,
                    ).props("rounded outline color=primary").classes("flex-grow")

            # ===================================================================
            # Citation lookup & verification
            # ===================================================================
            citations_card = ui.element("div").classes("gc-card w-full")
            nav_targets["citations"] = citations_card
            with citations_card:
                ui.label("Citation lookup & verification").classes(
                    "gc-section-title"
                )
                ui.label(
                    "Paste contract text or any prose to extract every legal "
                    "citation via Eyecite, and verify each one against "
                    "CourtListener. Useful for catching hallucinated cites or "
                    "stale references. Throttle: 60 valid citations/min, 250 "
                    "per request, 64 KB text."
                ).classes("gc-section-caption")
                cit_text = ui.textarea(
                    label="Text to scan",
                    placeholder=(
                        "Paste a contract clause, brief, or any text containing "
                        "legal citations to verify."
                    ),
                ).props("outlined").classes("w-full").style("min-height: 180px;")
                cit_status = ui.label("").classes("gc-section-caption")
                cit_table = ui.table(
                    columns=[
                        {"name": "citation", "label": "Citation", "field": "citation"},
                        {"name": "normalized", "label": "Normalized", "field": "normalized"},
                        {"name": "status", "label": "Status", "field": "status"},
                        {"name": "matched_case", "label": "Matched case", "field": "matched_case"},
                        {"name": "error", "label": "Error", "field": "error"},
                    ],
                    rows=[],
                    pagination=15,
                ).classes("w-full")

                async def run_citation_lookup() -> None:
                    if not cl_token.value.strip():
                        ui.notify(
                            "Add a CourtListener API token in the sidebar.",
                            type="warning",
                        )
                        return
                    text = (cit_text.value or "").strip()
                    if not text:
                        ui.notify("Paste some text first.", type="warning")
                        return
                    token = cl_token.value.strip()

                    def do_lookup() -> Any:
                        return CourtListenerClient(token).lookup_citations(text=text)

                    cit_run_btn.props("loading")
                    cit_status.text = "Calling CourtListener…"
                    try:
                        results = await run.io_bound(do_lookup)
                    except CourtListenerError as exc:
                        cit_status.text = ""
                        ui.notify(
                            f"CourtListener error ({exc.status_code}): {exc}",
                            type="negative", multi_line=True,
                        )
                        return
                    except Exception as exc:
                        cit_status.text = ""
                        ui.notify(f"Citation lookup error: {exc}", type="negative")
                        return
                    finally:
                        cit_run_btn.props(remove="loading")

                    if not results:
                        cit_status.text = "No citations found in the text."
                        cit_table.rows = []
                        cit_table.update()
                        return
                    status_labels = {
                        200: "✓ found",
                        404: "✗ not in CL",
                        300: "? ambiguous",
                        400: "✗ unknown reporter",
                        429: "⏸ throttled (>250)",
                    }
                    rows = []
                    for c in results:
                        clusters = c.get("clusters") or []
                        cluster_names = ", ".join(
                            cl.get("case_name") or "" for cl in clusters[:3]
                        )
                        try:
                            status_code = int(c.get("status") or 0)
                        except (TypeError, ValueError):
                            status_code = 0
                        rows.append({
                            "citation": c.get("citation"),
                            "normalized": ", ".join(c.get("normalized_citations") or []),
                            "status": status_labels.get(
                                status_code, str(c.get("status"))
                            ),
                            "matched_case": cluster_names or None,
                            "error": c.get("error_message") or None,
                        })
                    cit_table.rows = rows
                    cit_table.update()
                    n_found = sum(1 for c in results if c.get("status") == 200)
                    n_missing = sum(1 for c in results if c.get("status") == 404)
                    n_ambig = sum(1 for c in results if c.get("status") == 300)
                    cit_status.text = (
                        f"Found {len(results)} citation(s). "
                        f"{n_found} verified, {n_missing} not in CL, "
                        f"{n_ambig} ambiguous."
                    )

                cit_run_btn = ui.button(
                    "Look up citations", on_click=run_citation_lookup,
                ).props("rounded color=primary unelevated").classes("w-full mt-3")

            # ===================================================================
            # Save profile / Schedule recurring runs (macOS launchd)
            # ===================================================================
            schedule_anchor = ui.expansion(
                "Save profile / Schedule recurring runs", value=False,
            ).classes("w-full")
            nav_targets["schedule"] = schedule_anchor
            with schedule_anchor:
                with ui.element("div").classes("gc-card w-full"):
                    ui.label(
                        "Save the current SEC form values as a JSON profile, "
                        "then use launchd (macOS) to run them on a schedule. "
                        "The API key is not written to the profile — it stays "
                        "in GC_AI_API_KEY and is injected by launchd."
                    ).classes("gc-section-caption")
                    profile_name = ui.input(
                        label="Profile name", value="demo-entity-001",
                    ).props("outlined dense rounded").classes("w-full")
                    plist_output = ui.code("", language="xml").classes(
                        "w-full"
                    )
                    install_output = ui.code("", language="bash").classes(
                        "w-full"
                    )
                    plist_caption = ui.label("").classes("gc-section-caption")

                    def _safe_profile_name() -> tuple[str, Path, Path]:
                        project_dir = Path(__file__).resolve().parent
                        profiles_dir = project_dir / "profiles"
                        safe = (
                            re.sub(
                                r"[^A-Za-z0-9._-]+", "-",
                                (profile_name.value or "").strip(),
                            ).strip("-") or "unnamed"
                        )
                        return safe, project_dir, profiles_dir

                    def save_profile() -> None:
                        if not sec_user_agent.value or "@" not in sec_user_agent.value:
                            ui.notify(
                                "Set the SEC User-Agent first.", type="negative",
                            )
                            return
                        if not (profile_name.value or "").strip():
                            ui.notify("Profile name is required.", type="negative")
                            return
                        safe, _, profiles_dir = _safe_profile_name()
                        folder_mode = sec_gc_folder_mode.value
                        profile_data = {
                            "name": profile_name.value.strip(),
                            "user_agent": sec_user_agent.value.strip(),
                            "query": (sec_phrase.value or "").strip(),
                            "forms": list(sec_forms.value or DEFAULT_FORMS),
                            "no_exhibits": not bool(sec_include_exhibits.value),
                            "start": (sec_start.value or "").strip() or None,
                            "end": (sec_end.value or "").strip() or None,
                            "max_filings": (
                                int(sec_max_filings.value)
                                if sec_max_filings.value
                                else None
                            ),
                            "output": str(
                                Path(
                                    (sec_output_dir.value or "./contracts").strip()
                                ).resolve()
                            ),
                            "gc_ai_base_url": (
                                (gc_ai_base_url.value or "").strip()
                                or GC_AI_DEFAULT_BASE_URL
                            ),
                            "gc_ai_folder_id": (
                                (sec_gc_folder_id.value or "").strip()
                                if folder_mode == "existing" else None
                            ),
                            "gc_ai_folder_name": (
                                (sec_gc_folder_name.value or "").strip()
                                if folder_mode == "create" else None
                            ),
                            "gc_ai_folder_description": (
                                (sec_gc_folder_description.value or "").strip() or None
                            ),
                        }
                        profiles_dir.mkdir(exist_ok=True)
                        path = profiles_dir / f"{safe}.json"
                        path.write_text(
                            json.dumps(profile_data, indent=2), encoding="utf-8",
                        )
                        ui.notify(f"Saved {path}", type="positive")

                    def show_plist() -> None:
                        safe, project_dir, profiles_dir = _safe_profile_name()
                        path = profiles_dir / f"{safe}.json"
                        if not path.exists():
                            ui.notify(
                                f"No profile yet at {path}. Click 'Save profile' first.",
                                type="warning", multi_line=True,
                            )
                            return
                        python_path = project_dir / ".venv" / "bin" / "python"
                        script_path = project_dir / "sec_contract_scanner.py"
                        log_path = (
                            Path.home() / "Library" / "Logs"
                            / f"contractreview-{safe}.log"
                        )
                        label = f"com.contractreview.{safe}"
                        plist_path = (
                            Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
                        )
                        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
        <string>--profile</string>
        <string>{path}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>GC_AI_API_KEY</key>
        <string>PASTE_YOUR_USER_SCOPED_KEY_HERE</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""
                        plist_output.content = plist
                        install_output.content = (
                            f"# Save the plist above as:\n"
                            f"#   {plist_path}\n"
                            f"# Then install:\n"
                            f"chmod 600 {plist_path}\n"
                            f"launchctl bootstrap gui/$(id -u) {plist_path}\n\n"
                            f"# To remove later:\n"
                            f"launchctl bootout gui/$(id -u) {plist_path}\n"
                            f"rm {plist_path}"
                        )
                        plist_caption.text = (
                            "The displayed plist always contains a placeholder so "
                            "a live key cannot leak into copied output or screenshots. "
                            "Replace PASTE_YOUR_USER_SCOPED_KEY_HERE locally before "
                            "saving. The resulting plist holds the key in plaintext; "
                            "chmod 600 makes it readable only by your user."
                        )

                    with ui.row().classes("gap-2 mt-3 w-full"):
                        ui.button(
                            "Save profile", on_click=save_profile,
                        ).props("rounded color=primary unelevated").classes("flex-grow")
                        ui.button(
                            "Show launchd plist", on_click=show_plist,
                        ).props("rounded outline color=primary").classes("flex-grow")

            # Initial population
            refresh_staging()
            refresh_saved()


ui.run(
    title="ContractReview · GC AI",
    favicon=None,
    port=8080,
    reload=False,
    show=False,
)
