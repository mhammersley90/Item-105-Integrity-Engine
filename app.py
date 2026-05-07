"""Streamlit GUI for the SEC -> GC AI contract scanner.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from contextlib import redirect_stderr, redirect_stdout
from io import TextIOBase
from pathlib import Path

import pandas as pd
import streamlit as st

from sec_contract_scanner import (
    GC_AI_DEFAULT_BASE_URL,
    PROFILE_DEFAULT_FORMS,
    STAGING_DIRNAME,
    EdgarClient,
    GCAIClient,
    _append_manifest_line,
    commit_selection,
    safe_filename,
    scan_and_download,
)
from pacer_client import PACERAuthError, PACERClient, PCLClient, PCLError
from courtlistener_client import CourtListenerClient, CourtListenerError
from gc_triage import classify_filing as _classify_filing


DEFAULT_FORMS = list(PROFILE_DEFAULT_FORMS)
# Wider options exposed in the multiselect — pick into the form list as
# needed. Note: EDGAR's full-text endpoint is fussy about which forms it
# accepts; if a scan returns zero unexpectedly, try removing the most
# recently added form first.
ALL_FORMS = DEFAULT_FORMS + [
    "10-K/A", "10-Q/A", "8-K/A", "S-1/A", "S-3", "S-3/A", "S-4/A",
    "20-F/A", "6-K",
    "DEF 14A", "DEFM14A",
    "F-1", "F-3", "F-4",
    "NT 10-K", "NT 10-Q", "POS AM", "424B3", "424B5",
]


class QueueWriter(TextIOBase):
    """File-like that pushes complete lines onto a queue."""

    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.q.put(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self.q.put(self._buf)
            self._buf = ""


# Module-level lock so two concurrent scans (e.g. two browser tabs of the
# same app) don't both try to redirect_stdout simultaneously and scramble
# each other's log output. The second scan refuses to start, with a clear
# error returned through the worker's error channel.
_active_scan_lock = threading.Lock()


def run_scan(
    params: dict,
    q: "queue.Queue[str]",
    done: threading.Event,
    error_box: list,
) -> None:
    if not _active_scan_lock.acquire(blocking=False):
        msg = (
            "Another scan is already running in this Python process "
            "(probably a different browser tab). Wait for it to finish "
            "before starting a new one."
        )
        error_box.append(msg)
        try:
            q.put(f"ERROR: {msg}")
        except Exception:
            pass
        done.set()
        return
    writer = QueueWriter(q)
    try:
        with redirect_stdout(writer), redirect_stderr(writer):
            edgar = EdgarClient(params["user_agent"])
            gc_ai_client: GCAIClient | None = None
            gc_ai_folder_id = params.get("gc_ai_folder_id") or None
            wants_upload = bool(params.get("gc_ai_key")) and (
                params.get("gc_ai_folder_name") or gc_ai_folder_id
            )
            if wants_upload:
                gc_ai_client = GCAIClient(
                    params["gc_ai_key"], base_url=params["gc_ai_base_url"]
                )
                if not gc_ai_folder_id and params["gc_ai_folder_name"]:
                    folder = gc_ai_client.create_folder(
                        name=params["gc_ai_folder_name"],
                        description=params.get("gc_ai_folder_description") or None,
                    )
                    gc_ai_folder_id = folder["id"]
                    print(
                        f"Created GC AI folder {folder.get('path', '?')} "
                        f"(id={gc_ai_folder_id})"
                    )
            scan_and_download(
                client=edgar,
                phrase=params["query"],
                output_dir=Path(params["output_dir"]),
                forms=params["forms"],
                start=params.get("start") or None,
                end=params.get("end") or None,
                max_filings=params.get("max_filings"),
                gc_ai=gc_ai_client,
                gc_ai_folder_id=gc_ai_folder_id,
                include_exhibits=params.get("include_exhibits", True),
                staging_only=params.get("staging_only", False),
            )
    except Exception as exc:
        error_box.append(f"{type(exc).__name__}: {exc}")
        try:
            writer.write(f"\nERROR: {exc}\n")
        except Exception:
            pass
    finally:
        writer.flush()
        if _active_scan_lock.locked():
            try:
                _active_scan_lock.release()
            except RuntimeError:
                # Already released (shouldn't happen, but be defensive).
                pass
        done.set()


def load_manifest(output_dir: Path) -> pd.DataFrame:
    manifest = output_dir / "manifest.jsonl"
    if not manifest.exists():
        return pd.DataFrame()
    rows = []
    with manifest.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(rows)


st.set_page_config(
    page_title="ContractReview · GC AI",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

# gc.ai-inspired styling: cream surfaces, deep-navy text, rounded pill
# buttons / chips, card-style expanders, soft separators. Streamlit's
# config.toml handles the base palette; this CSS handles the finer
# details its theming doesn't expose. Selectors are intentionally narrow
# so they survive Streamlit's frequent DOM tweaks.
st.markdown(
    """
    <style>
    /* Primary buttons — rounded, deep navy fill */
    .stButton > button[kind="primary"],
    .stDownloadButton > button[kind="primary"] {
        border-radius: 9999px;
        background-color: #0E1A2D;
        color: #FAF5EE;
        border: 1px solid #0E1A2D;
        padding: 0.55rem 1.5rem;
        font-weight: 500;
        letter-spacing: 0.01em;
    }
    .stButton > button[kind="primary"]:hover,
    .stDownloadButton > button[kind="primary"]:hover {
        background-color: #1A2C44;
        border-color: #1A2C44;
        color: #FAF5EE;
    }
    /* Secondary buttons — outlined, matching pill shape */
    .stButton > button[kind="secondary"] {
        border-radius: 9999px;
        background-color: transparent;
        color: #0E1A2D;
        border: 1px solid #0E1A2D;
        padding: 0.55rem 1.25rem;
        font-weight: 500;
    }
    .stButton > button[kind="secondary"]:hover {
        background-color: #F2EBDC;
        color: #0E1A2D;
    }
    /* Form-submit buttons (PACER login) — same primary styling */
    .stFormSubmitButton > button {
        border-radius: 9999px;
        background-color: #0E1A2D;
        color: #FAF5EE;
        border: 1px solid #0E1A2D;
        padding: 0.55rem 1.5rem;
        font-weight: 500;
    }
    /* Headings — deep navy, slightly heavier */
    h1, h2, h3, h4 {
        color: #0E1A2D !important;
        font-weight: 700 !important;
        letter-spacing: -0.01em;
    }
    /* Multiselect chips — rounded pill */
    [data-baseweb="tag"] {
        border-radius: 9999px !important;
    }
    /* Expanders — card with soft border */
    [data-testid="stExpander"] > details {
        background-color: #FFFFFF;
        border-radius: 14px;
        border: 1px solid #E8E1D2;
        padding: 0.25rem 1rem;
    }
    /* Text inputs / selects — soft rounded */
    [data-baseweb="input"], [data-baseweb="select"] {
        border-radius: 10px !important;
    }
    /* Caption text — softer slate so it recedes */
    [data-testid="stCaptionContainer"] {
        color: #5A6273 !important;
    }
    /* Privacy banner — cream pill, mirrors gc.ai's pattern */
    .gc-privacy-banner {
        background-color: #F2EBDC;
        border: 1px solid #E8E1D2;
        border-radius: 9999px;
        padding: 0.55rem 1.4rem;
        text-align: center;
        color: #0E1A2D;
        font-size: 0.88rem;
        margin: 0.75rem auto 1.5rem;
        max-width: 720px;
    }
    /* Section divider — softer than the default rule */
    hr {
        border-color: #E8E1D2 !important;
        margin: 1.75rem 0 !important;
    }
    /* Sidebar — slightly warmer cream */
    [data-testid="stSidebar"] {
        background-color: #F5EDDF;
    }
    [data-testid="stSidebar"] .stTextInput > div {
        background-color: #FFFFFF;
        border-radius: 10px;
    }
    /* Saved contracts / staged candidates dataframes — card framing */
    [data-testid="stDataFrame"], [data-testid="stDataEditor"] {
        border: 1px solid #E8E1D2;
        border-radius: 12px;
        overflow: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("ContractReview · GC AI")
st.caption(
    "Find SEC filings, federal court records, and PACER documents that "
    "contain a target phrase — then stage the matches into a GC AI "
    "folder for risk-disclosure mapping."
)
st.markdown(
    '<div class="gc-privacy-banner">'
    "Credentials stay on your machine. Uploads to GC AI are encrypted "
    "in transit."
    '</div>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Credentials")
    user_agent = st.text_input(
        "SEC User-Agent",
        value=st.session_state.get("user_agent", ""),
        placeholder="DEMO_OPERATOR contact@example.invalid",
        help="SEC requires your name + a contact email on every request.",
    )
    st.session_state["user_agent"] = user_agent

    st.divider()
    st.subheader("GC AI")
    gc_ai_key = st.text_input(
        "API key (user-scoped)",
        value=os.environ.get("GC_AI_API_KEY", ""),
        type="password",
        placeholder="u:gcai_xxxxxxxxxxxxxxxx",
        help="User-scoped key. Created in GC AI under Settings -> Organization -> API Keys.",
    )
    gc_ai_base_url = st.text_input("API base URL", value=GC_AI_DEFAULT_BASE_URL)

    st.divider()
    st.subheader("PACER")
    pacer_env = st.radio(
        "Environment",
        ["QA (test, free)", "Production (billable)"],
        index=0,
        horizontal=True,
        help=(
            "QA is the PACER test environment — free, separate account "
            "required, register at qa-pacer.uscourts.gov. Use it while "
            "developing or trying queries. Production uses your real "
            "billable PACER account."
        ),
    )
    pacer_environment = "production" if pacer_env.startswith("Production") else "qa"

    pacer_status = st.session_state.get("pacer_status")
    if pacer_status and pacer_status.get("environment") == pacer_environment:
        st.success(
            f"Logged in as **{pacer_status['username']}** "
            f"({pacer_environment})"
        )
        if pacer_status.get("warning"):
            st.warning(pacer_status["warning"])
        if st.button("Logout PACER", use_container_width=True):
            try:
                client = PACERClient(environment=pacer_environment)
                client.logout(pacer_status["token"])
            except Exception as exc:
                st.warning(f"Logout call failed: {exc}")
            st.session_state.pop("pacer_status", None)
            st.rerun()
    else:
        if pacer_status and pacer_status.get("environment") != pacer_environment:
            # User toggled environments while logged in; treat as logged out.
            st.session_state.pop("pacer_status", None)
        with st.form("pacer_login_form", clear_on_submit=False):
            pacer_username = st.text_input("Username")
            pacer_password = st.text_input("Password", type="password")
            pacer_client_code = st.text_input(
                "Client code (optional)",
                help="Required by some firms for billing attribution.",
            )
            pacer_otp = st.text_input(
                "MFA one-time passcode (if enrolled)",
                help="6-digit code from your authenticator app.",
            )
            pacer_redact = st.checkbox(
                "I am a filer and confirm redaction compliance",
                help=(
                    "Required only for accounts with CM/ECF filing "
                    "privileges. Leave unchecked for search-only accounts."
                ),
            )
            login_submitted = st.form_submit_button(
                "Login to PACER", use_container_width=True
            )
        if login_submitted:
            if not pacer_username or not pacer_password:
                st.error("PACER username and password are required.")
            else:
                try:
                    pacer_client = PACERClient(environment=pacer_environment)
                    result = pacer_client.authenticate(
                        username=pacer_username.strip(),
                        password=pacer_password,
                        client_code=pacer_client_code.strip() or None,
                        otp_code=pacer_otp.strip() or None,
                        redact_flag=pacer_redact,
                    )
                    st.session_state["pacer_status"] = {
                        "token": result.token,
                        "username": pacer_username.strip(),
                        "client_code": result.client_code,
                        "warning": result.warning,
                        "environment": pacer_environment,
                    }
                    st.rerun()
                except PACERAuthError as exc:
                    st.error(
                        f"PACER login failed (loginResult={exc.login_result}): {exc}"
                    )
                except Exception as exc:
                    st.error(f"PACER login error: {exc}")

    st.divider()
    st.subheader("CourtListener / RECAP")
    cl_token = st.text_input(
        "API token (free)",
        value=os.environ.get("COURTLISTENER_TOKEN", ""),
        type="password",
        placeholder="40-char token",
        help=(
            "Get one free at courtlistener.com/profile/api-token/. "
            "Free tier: 5 req/min, 50/hr, 125/day. RECAP archive holds "
            "documents previously paid for via PACER — free to download."
        ),
    )
    if cl_token.strip():
        st.session_state["cl_token"] = cl_token.strip()
    elif "cl_token" in st.session_state:
        del st.session_state["cl_token"]

left, right = st.columns(2)

with left:
    st.subheader("Search")
    query = st.text_input(
        "Phrase to find",
        value="DEMO_ENTITY_001",
        help="Synthetic placeholder token; replace it with your own search term.",
    )
    forms = st.multiselect(
        "Form types", options=ALL_FORMS, default=DEFAULT_FORMS,
        help=(
            "Form types to search. Default covers periodic reports, "
            "registration statements, proxy statements, and foreign filings."
        ),
    )
    include_exhibits = st.checkbox(
        "Also scan exhibits attached to each filing",
        value=True,
        help=(
            "The primary filing body (the 10-K text itself, the 8-K body, "
            "the prospectus) is always scanned for the phrase.\n\n"
            "On (default): exhibits attached to each matching filing are "
            "also scanned. Ex 2/4/9/10 are captured outright; ambiguous "
            "exhibits and Ex 99 must contain an agreement-style header.\n\n"
            "Off: only the primary filing body is downloaded."
        ),
    )
    c1, c2 = st.columns(2)
    start = c1.text_input("Start date", placeholder="YYYY-MM-DD")
    end = c2.text_input("End date", placeholder="YYYY-MM-DD")
    max_filings = st.number_input(
        "Max filings (0 = no limit)", min_value=0, value=0, step=10
    )
    output_dir = st.text_input("Local output folder", value="./contracts")

with right:
    st.subheader("GC AI folder")
    folder_mode = st.radio(
        "Destination",
        ["Create new folder", "Use existing folder ID", "Don't upload"],
        index=0,
    )
    gc_ai_folder_name = ""
    gc_ai_folder_description = ""
    gc_ai_folder_id = ""
    if folder_mode == "Create new folder":
        gc_ai_folder_name = st.text_input(
            "New folder name", value="DEMO_ENTITY_001 — SYNTHETIC"
        )
        gc_ai_folder_description = st.text_input(
            "Description (optional)",
            value="Auto-uploaded by SEC contract scanner.",
        )
    elif folder_mode == "Use existing folder ID":
        gc_ai_folder_id = st.text_input("Folder UUID")

st.divider()
scan_mode = st.radio(
    "After matching, what should the scanner do?",
    [
        "Preview — let me pick which to save / upload",
        "Save & upload everything that matches (no review)",
    ],
    index=0,
    help=(
        "Preview mode (default) downloads matched files into a "
        "_staging/ folder so you can review them in a checkbox table "
        "before committing anything to the final folder or to GC AI. "
        "The other mode is the original behavior: save and upload "
        "every match automatically."
    ),
)
preview_mode = scan_mode.startswith("Preview")
run_clicked = st.button("Run scan", type="primary", use_container_width=True)

log_box = st.empty()
status_box = st.empty()

if run_clicked:
    errors: list[str] = []
    if not user_agent or "@" not in user_agent:
        errors.append("SEC User-Agent must include a contact email.")
    if not query.strip():
        errors.append("Search phrase is required.")
    if folder_mode != "Don't upload" and not gc_ai_key:
        errors.append("GC AI API key is required to upload.")
    if folder_mode == "Create new folder" and not gc_ai_folder_name.strip():
        errors.append("Folder name is required when creating a new folder.")
    if folder_mode == "Use existing folder ID" and not gc_ai_folder_id.strip():
        errors.append("Folder UUID is required.")

    if errors:
        for e in errors:
            st.error(e)
    else:
        params = {
            "user_agent": user_agent.strip(),
            "query": query.strip(),
            "forms": forms or DEFAULT_FORMS,
            "include_exhibits": include_exhibits,
            "start": start.strip(),
            "end": end.strip(),
            "max_filings": int(max_filings) if max_filings > 0 else None,
            "output_dir": output_dir.strip() or "./contracts",
            "staging_only": preview_mode,
            # In preview mode the scanner stages locally and skips uploads;
            # the user picks rows in the table afterwards. Don't even create
            # the GC AI folder up-front, since there's no point if nothing
            # gets uploaded yet.
            "gc_ai_key": (
                gc_ai_key.strip()
                if folder_mode != "Don't upload" and not preview_mode
                else ""
            ),
            "gc_ai_base_url": gc_ai_base_url.strip() or GC_AI_DEFAULT_BASE_URL,
            "gc_ai_folder_name": (
                gc_ai_folder_name.strip()
                if folder_mode == "Create new folder" and not preview_mode
                else ""
            ),
            "gc_ai_folder_description": gc_ai_folder_description.strip(),
            "gc_ai_folder_id": (
                gc_ai_folder_id.strip()
                if folder_mode == "Use existing folder ID" and not preview_mode
                else ""
            ),
        }

        q: queue.Queue[str] = queue.Queue()
        done = threading.Event()
        error_holder: list[str] = []
        # Snapshot the manifest counts so we can tell the user how many NEW
        # rows actually landed after the scan (vs everything being deduped
        # against previous runs).
        scan_target_dir = (
            (Path(params["output_dir"]) / STAGING_DIRNAME)
            if params.get("staging_only")
            else Path(params["output_dir"])
        )
        rows_before_scan = len(load_manifest(scan_target_dir))
        worker = threading.Thread(
            target=run_scan, args=(params, q, done, error_holder), daemon=True
        )
        worker.start()

        log_lines: list[str] = []
        status_box.info("Scan running...")
        while not done.is_set() or not q.empty():
            try:
                line = q.get(timeout=0.25)
                log_lines.append(line)
                log_box.code("\n".join(log_lines[-400:]))
            except queue.Empty:
                continue
        worker.join(timeout=5)

        if error_holder:
            status_box.error(f"Scan failed: {error_holder[0]}")
        else:
            rows_after_scan = len(load_manifest(scan_target_dir))
            new_rows = max(0, rows_after_scan - rows_before_scan)
            if new_rows > 0:
                if params.get("staging_only"):
                    status_box.success(
                        f"Scan complete. **{new_rows} new candidate(s) "
                        "staged** — pick which to commit in the **Staged "
                        "candidates** picker below."
                    )
                else:
                    status_box.success(
                        f"Scan complete. **{new_rows} new contract(s) "
                        "saved** to the main manifest."
                    )
            else:
                status_box.info(
                    "Scan complete — but **0 new contracts** were "
                    "captured. Every match was either already saved "
                    "(see **Saved contracts** below — you can re-upload "
                    "them to GC AI from there) or filtered out as "
                    "boilerplate / metadata-only (see scan log for "
                    "per-filing reasons)."
                )

out_dir = Path((output_dir or "./contracts").strip())
staging_dir = out_dir / STAGING_DIRNAME

PREFERRED_COLS = [
    "source", "filed", "form", "court", "exhibit", "company", "document",
    "match_via_raw_bytes",
    "gc_ai_status", "gc_ai_file_id", "gc_ai_folder_id",
    "url", "saved_as", "accession", "cik", "gc_ai_error",
]


def _ordered_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in PREFERRED_COLS if c in df.columns] + [
        c for c in df.columns if c not in PREFERRED_COLS
    ]


# ---- Staging review: pick which staged rows to commit ---------------------
staging_df = load_manifest(staging_dir)
if not staging_df.empty:
    st.divider()
    st.subheader(f"Staged candidates — pick which to keep ({len(staging_df)})")
    st.caption(
        f"Files are in {staging_dir} (not yet committed). Tick the rows you "
        "want, then click one of the buttons below."
    )

    editor_df = staging_df.copy()
    editor_df.insert(0, "Select", False)

    column_config: dict = {
        "Select": st.column_config.CheckboxColumn(label="✓", default=False),
    }
    for hidden in ("cik", "accession", "saved_as", "url",
                   "gc_ai_file_id", "gc_ai_folder_id", "gc_ai_status",
                   "gc_ai_error"):
        if hidden in editor_df.columns:
            column_config[hidden] = None

    ordered = editor_df[_ordered_cols(editor_df)]
    # Lock every column except the Select checkbox so a stray click in a
    # cell can't corrupt accession / document / saved_as values that
    # commit_selection relies on.
    locked_cols = [c for c in ordered.columns if c != "Select"]
    edited = st.data_editor(
        ordered,
        key="staging_editor",
        column_config=column_config,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        disabled=locked_cols,
    )

    selected_rows = edited[edited["Select"] == True]  # noqa: E712
    n_sel = len(selected_rows)

    btn1, btn2, btn3 = st.columns(3)
    save_local = btn1.button(
        f"Save {n_sel} selected locally",
        disabled=n_sel == 0,
        use_container_width=True,
    )
    save_and_upload = btn2.button(
        f"Save & upload {n_sel} to GC AI",
        disabled=n_sel == 0 or folder_mode == "Don't upload" or not gc_ai_key,
        use_container_width=True,
        help=(
            "Disabled until you select rows AND configure a GC AI folder "
            "destination + API key in the sidebar."
        ),
    )
    clear_staging = btn3.button(
        "Clear all staging",
        use_container_width=True,
        help="Delete every staged file and the staging manifest.",
    )

    if save_local or save_and_upload:
        records = selected_rows.drop(columns=["Select"]).to_dict("records")
        gc_ai_client_for_commit: GCAIClient | None = None
        gc_ai_folder_id_for_commit: str | None = None
        if save_and_upload:
            try:
                gc_ai_client_for_commit = GCAIClient(
                    gc_ai_key.strip(), base_url=gc_ai_base_url.strip()
                )
                if folder_mode == "Use existing folder ID":
                    gc_ai_folder_id_for_commit = gc_ai_folder_id.strip()
                else:
                    folder = gc_ai_client_for_commit.create_folder(
                        name=gc_ai_folder_name.strip(),
                        description=gc_ai_folder_description.strip() or None,
                    )
                    gc_ai_folder_id_for_commit = folder["id"]
                    st.info(
                        f"Created GC AI folder {folder.get('path', '?')} "
                        f"(id={gc_ai_folder_id_for_commit})"
                    )
            except Exception as exc:
                st.error(f"GC AI setup failed: {exc}")
                gc_ai_client_for_commit = None

        result = commit_selection(
            staging_dir=staging_dir,
            output_dir=out_dir,
            selected_records=records,
            gc_ai=gc_ai_client_for_commit,
            gc_ai_folder_id=gc_ai_folder_id_for_commit,
        )
        msg = f"Saved {result['moved']} file(s) to {out_dir}."
        if save_and_upload:
            msg += f" Uploaded {result['uploaded']} to GC AI."
        if result["failed"]:
            st.warning(msg + f" {len(result['failed'])} failure(s): "
                       + "; ".join(result["failed"][:5]))
        else:
            st.success(msg)
        st.rerun()

    if clear_staging:
        import shutil
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        st.success(f"Cleared {staging_dir}.")
        st.rerun()

# ---- Final-folder manifest (already-committed + auto-saved rows) ----------
st.divider()
st.subheader("Saved contracts")
df = load_manifest(out_dir)
if df.empty:
    st.info(
        f"No manifest yet at {out_dir / 'manifest.jsonl'}. "
        "Run a scan and commit selections (preview mode) "
        "or run with auto-save to populate it."
    )
else:
    st.caption(
        f"Source: {out_dir / 'manifest.jsonl'} ({len(df)} row(s)). "
        "Tick rows below to re-upload existing contracts to GC AI — "
        "useful when a previous upload failed or you want to send "
        "already-saved contracts to a new folder."
    )
    saved_editor_df = df.copy()
    saved_editor_df.insert(0, "Select", False)
    saved_locked = [c for c in saved_editor_df.columns if c != "Select"]
    saved_column_config: dict = {
        "Select": st.column_config.CheckboxColumn(label="✓", default=False),
    }
    for hidden in ("cik", "accession", "url",
                   "gc_ai_file_id", "gc_ai_folder_id"):
        if hidden in saved_editor_df.columns:
            saved_column_config[hidden] = None
    saved_edited = st.data_editor(
        saved_editor_df[_ordered_cols(saved_editor_df)],
        key="saved_editor",
        column_config=saved_column_config,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        disabled=saved_locked,
    )

    saved_selected = saved_edited[saved_edited["Select"] == True]  # noqa: E712
    n_saved_sel = len(saved_selected)
    saved_can_upload = (
        n_saved_sel > 0
        and folder_mode != "Don't upload"
        and bool(gc_ai_key)
    )
    upload_existing = st.button(
        f"Upload {n_saved_sel} selected to GC AI",
        disabled=not saved_can_upload,
        use_container_width=True,
        key="saved_upload_existing",
        help=(
            "Re-uploads already-saved contracts to a GC AI folder. "
            "Disabled until rows are picked AND a GC AI folder destination "
            "+ API key are configured in the sidebar / right panel."
        ),
    )

    if upload_existing:
        try:
            gc_ai_existing = GCAIClient(
                gc_ai_key.strip(), base_url=gc_ai_base_url.strip()
            )
            if folder_mode == "Use existing folder ID":
                gc_folder_id = gc_ai_folder_id.strip()
            else:
                folder = gc_ai_existing.create_folder(
                    name=gc_ai_folder_name.strip(),
                    description=gc_ai_folder_description.strip() or None,
                )
                gc_folder_id = folder["id"]
                st.info(
                    f"Created GC AI folder {folder.get('path', '?')} "
                    f"(id={gc_folder_id})"
                )
        except Exception as exc:
            st.error(f"GC AI setup failed: {exc}")
        else:
            uploaded_n = 0
            upload_failed: list[str] = []
            with st.spinner(f"Uploading {n_saved_sel} file(s) to GC AI…"):
                for _, row in saved_selected.iterrows():
                    saved_as = row.get("saved_as")
                    if not saved_as or pd.isna(saved_as):
                        upload_failed.append(
                            f"{row.get('document', '?')} (no saved_as)"
                        )
                        continue
                    file_path = out_dir / str(saved_as)
                    if not file_path.exists():
                        upload_failed.append(
                            f"{saved_as} (file missing on disk)"
                        )
                        continue
                    try:
                        resp = gc_ai_existing.upload_file(
                            file_path, folder_id=gc_folder_id
                        )
                        uploaded_n += 1
                        # Hint at status; don't rewrite the manifest here
                        # (re-uploads can co-exist; updating the row would
                        # require a manifest rewrite which complicates dedup).
                    except Exception as exc:
                        upload_failed.append(f"{saved_as} ({exc})")
            if uploaded_n:
                st.success(
                    f"Re-uploaded {uploaded_n} contract(s) to GC AI folder "
                    f"{gc_folder_id}. The new GC AI file IDs are not "
                    "written back to manifest.jsonl — query GC AI for the "
                    "current folder contents if you need them."
                )
            if upload_failed:
                st.warning(
                    f"{len(upload_failed)} upload failure(s): "
                    + "; ".join(upload_failed[:5])
                )


# ---- PACER Case Locator search ------------------------------------------
st.divider()
st.subheader("PACER Case Locator search")
pacer_session = st.session_state.get("pacer_status")
if not pacer_session:
    st.info(
        "Log in to PACER in the sidebar to search the federal court "
        "case index. Use QA for free testing; Production charges $0.10 "
        "per page (~54 hits)."
    )
else:
    pacer_session_env = pacer_session.get("environment", "qa")
    if pacer_session_env == "production":
        st.warning(
            f"**Production PACER active.** Each search page is billed "
            f"$0.10 to **{pacer_session['username']}**. Session cost so "
            f"far: **${st.session_state.get('pacer_cost_dollars', 0.0):.2f}**."
        )
    else:
        st.success(
            f"PACER QA (free) — logged in as **{pacer_session['username']}**. "
            f"Session cost: **${st.session_state.get('pacer_cost_dollars', 0.0):.2f}** (none, QA is free)."
        )

    pcl_left, pcl_right = st.columns(2)
    with pcl_left:
        pcl_search_type = st.radio(
            "Search by",
            ["Party / entity name", "Case (title or number)"],
            index=0,
            horizontal=True,
            help=(
                "Party search finds court matters where the named "
                "person or entity is a party (debtor, defendant, "
                "plaintiff). Case search finds matters by case title "
                "substring or full case number."
            ),
        )
        if pcl_search_type.startswith("Party"):
            pcl_party_name = st.text_input(
                "Party / entity name",
                value=query if query else "DEMO_ENTITY_001",
                help="Defaults to 'starts with' match. Tick the exact-match "
                     "box below for strict equality.",
            )
            pcl_first_name = st.text_input(
                "First name (individuals only — leave blank for entities)"
            )
            pcl_exact_match = st.checkbox(
                "Exact name match", value=False
            )
        else:
            pcl_case_title = st.text_input(
                "Case title contains",
                help="'Starts with' match on case title.",
            )
            pcl_case_number = st.text_input(
                "Case number (full or partial)",
                help=(
                    "Use a full or partial case number in a format accepted "
                    "by PACER Case Locator."
                ),
            )

    with pcl_right:
        # Common case-level filters for both modes.
        pcl_jurisdiction = st.selectbox(
            "Jurisdiction",
            ["", "bk", "cv", "cr", "ap", "mdl"],
            format_func=lambda v: {
                "": "Any", "bk": "Bankruptcy", "cv": "Civil",
                "cr": "Criminal", "ap": "Appellate", "mdl": "MDL",
            }.get(v, v),
        )
        pcl_court_ids = st.text_input(
            "Court IDs (comma-separated, e.g. nysdc,cacdc)",
            help=(
                "Use the codes from PCL Appendix A. Common ones: "
                "nysdc (NY Southern DC), cacdc (Cal Central DC), "
                "ilndc (Illinois Northern DC). Leave blank for all courts."
            ),
        )
        c_a, c_b = st.columns(2)
        pcl_date_from = c_a.text_input("Date filed from", placeholder="YYYY-MM-DD")
        pcl_date_to = c_b.text_input("Date filed to", placeholder="YYYY-MM-DD")
        pcl_page = st.number_input(
            "Page (0 = first)", min_value=0, value=0, step=1,
            help="PCL returns 54 hits per page. Bump this to paginate.",
        )

    if pacer_session_env == "production":
        confirm = st.checkbox(
            "I confirm this Production PACER search will be billed $0.10 to my account",
            value=False,
            key="pcl_confirm_billing",
        )
    else:
        confirm = True  # QA is free

    pcl_run = st.button(
        "Run PACER search",
        type="primary",
        disabled=not confirm,
        use_container_width=True,
    )

    if pcl_run:
        # Build criteria
        court_list = [c.strip() for c in pcl_court_ids.split(",") if c.strip()]
        case_filters: dict = {}
        if pcl_jurisdiction:
            case_filters["jurisdictionType"] = pcl_jurisdiction
        if court_list:
            case_filters["courtId"] = court_list
        if pcl_date_from.strip():
            case_filters["dateFiledFrom"] = pcl_date_from.strip()
        if pcl_date_to.strip():
            case_filters["dateFiledTo"] = pcl_date_to.strip()

        if pcl_search_type.startswith("Party"):
            last_name = pcl_party_name.strip()
            if not last_name:
                st.error("Party name is required for a party search.")
                st.stop()
            criteria = {
                "lastName": last_name,
                "firstName": pcl_first_name.strip() or None,
                "exactNameMatch": pcl_exact_match,
                "courtCase": case_filters or None,
            }
        else:
            case_title_v = pcl_case_title.strip() or None
            case_number_v = pcl_case_number.strip() or None
            if not case_title_v and not case_number_v:
                st.error("Provide a case title or case number.")
                st.stop()
            criteria = {
                "caseTitle": case_title_v,
                "caseNumberFull": case_number_v,
                **case_filters,
            }

        try:
            pcl_client = PCLClient(
                token=pacer_session["token"],
                environment=pacer_session_env,
                client_code=pacer_session.get("client_code"),
            )
            with st.spinner("Calling PACER…"):
                if pcl_search_type.startswith("Party"):
                    result = pcl_client.search_parties(criteria, page=int(pcl_page))
                else:
                    result = pcl_client.search_cases(criteria, page=int(pcl_page))
        except PCLError as exc:
            st.error(f"PACER search failed ({exc.status_code}): {exc}")
            if exc.status_code == 401:
                st.session_state.pop("pacer_status", None)
        except Exception as exc:
            st.error(f"PACER request error: {exc}")
        else:
            # Store rotated token so subsequent searches use the fresh one.
            if result.new_token:
                pacer_session["token"] = result.new_token
                st.session_state["pacer_status"] = pacer_session

            fee = result.search_fee_dollars
            st.session_state["pacer_cost_dollars"] = (
                st.session_state.get("pacer_cost_dollars", 0.0) + fee
            )

            page_info = result.page_info
            total = page_info.get("totalElements", len(result.content))
            shown = len(result.content)
            page_n = page_info.get("number", 0)
            total_pages = page_info.get("totalPages", 1)
            st.success(
                f"PACER returned {shown} hit(s) on page {page_n + 1} "
                f"of {total_pages} (total: {total}). "
                f"Search fee: ${fee:.2f}."
            )

            if not result.content:
                st.info("No results.")
            else:
                rows = []
                for hit in result.content:
                    cc = hit.get("courtCase") or {}
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
                        "case_link": cc.get("caseLink") or hit.get("caseLink"),
                    })
                pcl_df = pd.DataFrame(rows)
                st.dataframe(
                    pcl_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "case_link": st.column_config.LinkColumn(
                            "CM/ECF link",
                            help="Opens the case in the originating court's CM/ECF.",
                        ),
                    },
                )

                with st.expander(f"Receipt — search fee ${fee:.2f}"):
                    st.json(result.receipt)


# ---- CourtListener / RECAP search ---------------------------------------
st.divider()
st.subheader("CourtListener / RECAP search (free)")
cl_token_active = st.session_state.get("cl_token")
if not cl_token_active:
    st.info(
        "Add a CourtListener API token in the sidebar to search the "
        "RECAP archive — federal court documents previously fetched "
        "from PACER, mirrored for free."
    )
else:
    st.caption(
        "Search uses CourtListener's open `/search/?type=r` endpoint — no "
        "elevated access required. Direct browse of an individual docket's "
        "documents / parties / attorneys is restricted to select users; "
        "see the badge on the result rows."
    )
    cl_l, cl_r = st.columns(2)
    with cl_l:
        cl_query = st.text_input(
            "Phrase / query",
            value=query if query else "DEMO_ENTITY_001",
            help=(
                "Lucene-style query syntax supported — quote phrases, "
                "use AND/OR/NOT, parens, etc. Default is keyword search."
            ),
            key="cl_query_input",
        )
        cl_party = st.text_input(
            "Party name (optional)",
            help="Narrow to dockets where this party appears.",
        )
        cl_search_type = st.radio(
            "Search granularity",
            ["Dockets (one row per case)", "Documents (one row per filing)"],
            index=0,
            horizontal=True,
            help=(
                "Dockets (`type=r`) returns up to 3 nested matching "
                "documents per case; the `more_docs` column flags cases "
                "with extra matches. Documents (`type=rd`) returns one "
                "row per filing — useful when you care about specific "
                "documents rather than cases."
            ),
        )
    with cl_r:
        cl_courts_raw = st.text_input(
            "Court IDs (optional, comma-separated)",
            help=(
                "RECAP court codes: 'nysd' (NY S DC), 'cacd' (CA C DC), "
                "'ilnd' (IL N DC), etc. Differ slightly "
                "from PACER's PCL codes — see CourtListener's jurisdiction "
                "page. Leave blank for all courts."
            ),
        )
        clc1, clc2 = st.columns(2)
        cl_after = clc1.text_input("Filed after", placeholder="YYYY-MM-DD")
        cl_before = clc2.text_input("Filed before", placeholder="YYYY-MM-DD")
        cl_order_label = st.selectbox(
            "Order by",
            [
                "Relevance",
                "Date filed (newest first)",
                "Date filed (oldest first)",
                "Entry date filed (newest first)",
            ],
            index=0,
            help=(
                "Mirrors the dropdown on the CourtListener web UI. "
                "Relevance uses the Citegeist BM25 score."
            ),
        )

    cl_order_by_map = {
        "Relevance": None,  # default; let server pick
        "Date filed (newest first)": "dateFiled desc",
        "Date filed (oldest first)": "dateFiled asc",
        "Entry date filed (newest first)": "entry_date_filed desc",
    }
    cl_search_type_code = (
        "rd" if cl_search_type.startswith("Documents") else "r"
    )

    cl_run = st.button(
        "Search RECAP", type="primary", use_container_width=True,
    )

    if cl_run:
        court_list = [c.strip() for c in cl_courts_raw.split(",") if c.strip()]
        try:
            cl_client = CourtListenerClient(cl_token_active)
            with st.spinner("Calling CourtListener…"):
                cl_result = cl_client.search_recap(
                    cl_query.strip(),
                    court_ids=court_list or None,
                    filed_after=cl_after.strip() or None,
                    filed_before=cl_before.strip() or None,
                    party_name=cl_party.strip() or None,
                    order_by=cl_order_by_map[cl_order_label],
                    search_type=cl_search_type_code,
                )
        except CourtListenerError as exc:
            st.error(f"CourtListener error ({exc.status_code}): {exc}")
        except Exception as exc:
            st.error(f"CourtListener request error: {exc}")
        else:
            st.session_state["cl_last_result"] = cl_result
            st.session_state["cl_token_for_pagination"] = cl_token_active

    cl_result = st.session_state.get("cl_last_result")
    if cl_result is not None:
        # Count caveat documented in the Search API page.
        count_label = (
            f"about {cl_result.count:,}"
            if cl_result.count > 2000
            else f"{cl_result.count:,}"
        )
        st.success(
            f"RECAP page returned {len(cl_result.results)} hit(s) "
            f"(total: {count_label}). "
            + ("The total has ±6% error above 2,000 results." if cl_result.count > 2000 else "")
        )
        if not cl_result.results:
            st.info("No matches on this page.")
        else:
            cl_rows = []
            for hit in cl_result.results:
                abs_url = hit.get("absolute_url")
                cl_link = (
                    f"https://www.courtlistener.com{abs_url}"
                    if abs_url and abs_url.startswith("/")
                    else abs_url
                )
                # type=r hits often nest the matching document; check the
                # top level first, then the first nested entry. Same for
                # the recap_document id, which Pray-and-Pay needs.
                filepath = hit.get("filepath_local")
                # type=rd: hit['id'] IS the recap_document id (no docket_id
                # at top). type=r: top has docket_id, nested docs carry
                # their own id.
                if hit.get("docket_id"):
                    nested = hit.get("recap_documents") or []
                    nested = nested if isinstance(nested, list) else []
                    if not filepath and nested:
                        filepath = (
                            nested[0].get("filepath_local")
                            or nested[0].get("local_path")
                        )
                    # Pick the first nested doc whose PDF isn't yet in
                    # RECAP — that's the one a prayer would actually help.
                    recap_doc_id = next(
                        (
                            d.get("id") for d in nested
                            if d.get("id") and not d.get("filepath_local")
                        ),
                        nested[0].get("id") if nested else None,
                    )
                else:
                    if not filepath:
                        nested = (
                            hit.get("opinions")  # type=o fallback
                            or []
                        )
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
                # Strip <mark> tags from the snippet for table display
                snippet = (
                    hit.get("snippet")
                    or hit.get("short_description")
                    or ""
                )
                snippet = re.sub(r"</?mark>", "", snippet)
                # The 'description' / 'document_type' fields tell a GC
                # what kind of filing this actually is — Voluntary
                # Petition, Schedules of Assets and Liabilities, Motion
                # to Assume Executory Contract, Adversary Complaint,
                # etc. Surface them directly and use them to drive the
                # classifier.
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
                # type=rd hits sometimes lack docket-level fields; try
                # additional fallbacks before showing 'None None None'.
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
                kind = _classify_filing(
                    description, doc_type, snippet, case_value or "",
                )
                cl_rows.append({
                    "kind": kind,
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
                    "more_docs": bool(hit.get("more_docs")),
                    "snippet": snippet[:400] if snippet else None,
                    "courtlistener": cl_link,
                    "pdf": pdf_link,
                    "recap_doc_id": recap_doc_id,
                })
            cl_df = pd.DataFrame(cl_rows)

            # GC-triage filter: surface only contracts, only lawsuits,
            # or hide everything that looks like procedural noise.
            n_contracts = int((cl_df["kind"] == "contract").sum())
            n_lawsuits = int((cl_df["kind"] == "lawsuit").sum())
            n_other = len(cl_df) - n_contracts - n_lawsuits
            filter_choice = st.radio(
                "Triage filter",
                [
                    f"All ({len(cl_df)})",
                    f"Likely contracts ({n_contracts})",
                    f"Likely lawsuits ({n_lawsuits})",
                    f"Other / unclear ({n_other})",
                ],
                horizontal=True,
                key="cl_kind_filter",
                help=(
                    "Classifier looks at the filing's description / type "
                    "field for contract-signal terms (agreement, lease, "
                    "executory contract, schedule G, asset purchase, "
                    "assumption / rejection, indenture, …) and lawsuit-"
                    "signal terms (complaint, adversary proceeding, "
                    "summary judgment, motion to dismiss, jury demand, …). "
                    "Procedural noise lands in 'Other / unclear'."
                ),
            )
            kind_filter_map = {
                "All": None,
                "Likely contracts": "contract",
                "Likely lawsuits": "lawsuit",
                "Other / unclear": "",
            }
            # The radio labels include the count; strip it back to the key
            filter_label = filter_choice.split(" (")[0]
            kind_filter = kind_filter_map.get(filter_label)
            if kind_filter is not None:
                visible_cl_df = cl_df[cl_df["kind"] == kind_filter].reset_index(drop=True)
            else:
                visible_cl_df = cl_df

            cl_editor_df = visible_cl_df.copy()
            cl_editor_df.insert(0, "Select", False)
            cl_locked = [c for c in cl_editor_df.columns if c != "Select"]
            cl_edited = st.data_editor(
                cl_editor_df,
                hide_index=True,
                use_container_width=True,
                num_rows="fixed",
                disabled=cl_locked,
                column_config={
                    "Select": st.column_config.CheckboxColumn(
                        label="✓",
                        default=False,
                        help=(
                            "Tick rows whose RECAP PDFs you want staged "
                            "(downloaded into _staging/ and added to the "
                            "staging picker above for commit / GC AI upload)."
                        ),
                    ),
                    "kind": st.column_config.TextColumn(
                        "kind",
                        help=(
                            "GC-triage classification: 'contract' = the "
                            "filing's description matches contract-signal "
                            "terms; 'lawsuit' = matches litigation terms; "
                            "blank = neither (procedural / metadata)."
                        ),
                    ),
                    "description": st.column_config.TextColumn(
                        "description",
                        help=(
                            "What kind of filing this actually is "
                            "(Voluntary Petition, Schedules of Assets and "
                            "Liabilities, Motion to Assume Executory "
                            "Contract, Adversary Complaint, ...). Most "
                            "decision-relevant column for triage."
                        ),
                    ),
                    "doc_type": st.column_config.TextColumn(
                        "doc_type",
                        help="RECAP document_type if present.",
                    ),
                    "courtlistener": st.column_config.LinkColumn(
                        "Open on CourtListener"
                    ),
                    "pdf": st.column_config.LinkColumn(
                        "PDF",
                        help=(
                            "Direct link to the RECAP-archived PDF. "
                            "Empty if the filing isn't in RECAP yet."
                        ),
                    ),
                    "more_docs": st.column_config.CheckboxColumn(
                        "more docs?",
                        help=(
                            "Type=r returns up to 3 nested matching "
                            "documents per docket; this row had more — "
                            "switch to 'Documents' granularity above to "
                            "see them all."
                        ),
                    ),
                    "recap_doc_id": None,  # hide from display, used by Pray button
                },
                key="cl_results_editor",
            )
            st.caption(
                "Empty PDF cells = the filing isn't in the RECAP archive "
                "(no one has paid for it yet). Open the case on "
                "CourtListener to request it via Pray-and-Pay or fetch "
                "from PACER directly."
            )

            cl_selected_rows = cl_edited[cl_edited["Select"] == True]  # noqa: E712
            cl_with_pdf = cl_selected_rows[
                cl_selected_rows["pdf"].notna()
                & (cl_selected_rows["pdf"].astype(str).str.strip() != "")
            ]
            # Prayer-eligible: NOT in RECAP yet AND we know the doc id
            cl_no_pdf = cl_selected_rows[
                cl_selected_rows["pdf"].isna()
                | (cl_selected_rows["pdf"].astype(str).str.strip() == "")
            ]
            cl_prayable = cl_no_pdf[
                cl_no_pdf["recap_doc_id"].notna()
                & (cl_no_pdf["recap_doc_id"].astype(str).str.strip() != "")
            ]
            cl_n_stageable = len(cl_with_pdf)
            cl_n_prayable = len(cl_prayable)

            cl_nav_a, cl_nav_b, cl_nav_c, cl_nav_d = st.columns(4)
            stage_label = f"Stage {cl_n_stageable} PDF(s)"
            stage_clicked = cl_nav_a.button(
                stage_label,
                type="primary",
                disabled=cl_n_stageable == 0,
                use_container_width=True,
                key="cl_stage",
                help=(
                    "Downloads each selected RECAP PDF into the local "
                    "staging dir and adds a row to the staging picker "
                    "above. From there, commit selected rows locally "
                    "and/or upload them to GC AI like SEC contracts."
                ),
            )
            pray_clicked = cl_nav_b.button(
                f"Pray for {cl_n_prayable} doc(s)",
                disabled=cl_n_prayable == 0,
                use_container_width=True,
                key="cl_pray",
                help=(
                    "For selected rows whose PDF isn't in RECAP yet: "
                    "create a free Pray-and-Pay request. CourtListener "
                    "will email you (and grant the prayer) when someone "
                    "else fetches the document. Subject to your daily "
                    "prayer quota."
                ),
            )
            cl_next_disabled = not cl_result.next_url
            next_clicked = cl_nav_c.button(
                "Next page",
                disabled=cl_next_disabled,
                use_container_width=True,
                key="cl_next_page",
            )
            clear_clicked = cl_nav_d.button(
                "Clear results", use_container_width=True, key="cl_clear",
            )

            if stage_clicked:
                cl_client = CourtListenerClient(
                    st.session_state["cl_token_for_pagination"]
                )
                staging = out_dir / STAGING_DIRNAME
                staging.mkdir(parents=True, exist_ok=True)
                staging_manifest_path = staging / "manifest.jsonl"

                staged = 0
                stage_failed: list[str] = []
                with staging_manifest_path.open(
                    "a", encoding="utf-8"
                ) as staging_manifest, st.spinner(
                    f"Downloading {cl_n_stageable} PDF(s) from RECAP…"
                ):
                    for _, row in cl_with_pdf.iterrows():
                        pdf_url = str(row["pdf"]).strip()
                        # The pdf column was already absolute-ified upstream,
                        # so download_pdf will pass it through unchanged.
                        # Strip query string / fragment so the saved
                        # filename doesn't end up "doc.pdf?download=1".
                        basename = pdf_url.rsplit("/", 1)[-1]
                        basename = basename.split("?", 1)[0].split("#", 1)[0]
                        original_name = basename or "recap.pdf"
                        try:
                            pdf_bytes = cl_client.download_pdf(pdf_url)
                        except CourtListenerError as exc:
                            stage_failed.append(f"{original_name} ({exc})")
                            continue
                        except Exception as exc:
                            stage_failed.append(
                                f"{original_name} (network: {exc})"
                            )
                            continue

                        docket_safe = (
                            str(row.get("docket") or "")
                            .replace(":", "_")
                            .replace("/", "_")
                        )
                        target_name = safe_filename(
                            str(row.get("filed") or "undated"),
                            (str(row.get("court") or "RECAP")).upper(),
                            docket_safe or "no-docket",
                            original_name,
                        )
                        if not target_name.lower().endswith(".pdf"):
                            target_name += ".pdf"
                        target_path = staging / target_name
                        target_path.write_bytes(pdf_bytes)

                        record = {
                            "source": "recap",
                            "accession": (
                                str(row.get("docket") or original_name)
                            ),
                            "cik": "",
                            "form": "RECAP",
                            "filed": row.get("filed"),
                            "company": row.get("case"),
                            "document": original_name,
                            "exhibit": "recap",
                            "url": pdf_url,
                            "saved_as": target_name,
                            "court": row.get("court"),
                        }
                        # Sanitize NaN/None before serialization (pandas can
                        # leave NaNs in the row's optional columns).
                        clean = {
                            k: v for k, v in record.items()
                            if v is not None
                            and not (
                                isinstance(v, float)
                                and pd.isna(v)
                            )
                        }
                        _append_manifest_line(
                            staging_manifest,
                            json.dumps(clean) + "\n",
                        )
                        staged += 1

                if staged:
                    st.success(
                        f"Staged {staged} PDF(s) in {staging}. Scroll up "
                        "to **Staged candidates** to commit them locally "
                        "and/or upload to GC AI."
                    )
                if stage_failed:
                    st.warning(
                        f"{len(stage_failed)} download failure(s): "
                        + "; ".join(stage_failed[:5])
                        + ("…" if len(stage_failed) > 5 else "")
                    )
                st.rerun()

            if pray_clicked:
                cl_client = CourtListenerClient(
                    st.session_state["cl_token_for_pagination"]
                )
                created = 0
                already_prayed = 0
                pray_failed: list[str] = []
                with st.spinner(f"Submitting {cl_n_prayable} prayer(s)…"):
                    for _, row in cl_prayable.iterrows():
                        rdid = str(row["recap_doc_id"]).strip()
                        try:
                            resp = cl_client.create_prayer(rdid)
                            if resp.get("status") == 1:
                                created += 1
                            else:
                                # CL sometimes returns the existing prayer
                                # if you ask for a doc you've already
                                # prayed for — count it separately.
                                already_prayed += 1
                        except CourtListenerError as exc:
                            label = (
                                str(row.get("docket") or rdid).strip()
                                or rdid
                            )
                            pray_failed.append(f"{label} ({exc})")
                        except Exception as exc:
                            pray_failed.append(
                                f"{rdid} (network: {exc})"
                            )

                msg_parts: list[str] = []
                if created:
                    msg_parts.append(f"created {created} prayer(s)")
                if already_prayed:
                    msg_parts.append(
                        f"{already_prayed} already prayed for"
                    )
                if msg_parts:
                    st.success(
                        "Pray-and-Pay: " + ", ".join(msg_parts)
                        + ". CourtListener will email you when a "
                        "document becomes available."
                    )
                if pray_failed:
                    st.warning(
                        f"{len(pray_failed)} prayer(s) rejected (likely "
                        "daily quota): "
                        + "; ".join(pray_failed[:3])
                        + ("…" if len(pray_failed) > 3 else "")
                    )

            if next_clicked:
                try:
                    cl_client = CourtListenerClient(
                        st.session_state["cl_token_for_pagination"]
                    )
                    with st.spinner("Fetching next page…"):
                        st.session_state["cl_last_result"] = cl_client.next_page(
                            cl_result
                        )
                    st.rerun()
                except CourtListenerError as exc:
                    st.error(f"CourtListener error ({exc.status_code}): {exc}")

            if clear_clicked:
                st.session_state.pop("cl_last_result", None)
                st.rerun()


# ---- CourtListener: citation lookup / verification ---------------------
st.divider()
with st.expander("Citation lookup & verification", expanded=False):
    st.write(
        "Paste contract text (or any prose) to extract every legal "
        "citation in it via Eyecite, and verify each one against "
        "CourtListener's case-law database. Useful for catching "
        "hallucinated cites or stale references in agreements. Requires "
        "the CourtListener API token in the sidebar. Throttle: 60 valid "
        "citations / minute, 250 per request, 64 KB text."
    )
    cit_text = st.text_area(
        "Text to scan",
        height=180,
        placeholder=(
            "Paste a contract clause, a brief, or any text containing legal "
            "citations to verify."
        ),
    )
    cit_run = st.button(
        "Look up citations", type="primary", use_container_width=True,
        disabled=not (cl_token_active and cit_text.strip()),
    )
    if cit_run:
        cl_client = CourtListenerClient(cl_token_active)
        try:
            with st.spinner("Calling CourtListener…"):
                cit_results = cl_client.lookup_citations(text=cit_text.strip())
        except CourtListenerError as exc:
            st.error(f"CourtListener error ({exc.status_code}): {exc}")
        except Exception as exc:
            st.error(f"Citation lookup error: {exc}")
        else:
            if not cit_results:
                st.info("No citations found in the text.")
            else:
                # Status code → human label
                status_labels = {
                    200: "✓ found",
                    404: "✗ not in CL",
                    300: "? ambiguous",
                    400: "✗ unknown reporter",
                    429: "⏸ throttled (>250)",
                }
                cit_rows = []
                for c in cit_results:
                    clusters = c.get("clusters") or []
                    cluster_names = ", ".join(
                        cl.get("case_name") or "" for cl in clusters[:3]
                    )
                    try:
                        status_code = int(c.get("status") or 0)
                    except (TypeError, ValueError):
                        status_code = 0
                    cit_rows.append({
                        "citation": c.get("citation"),
                        "normalized": ", ".join(
                            c.get("normalized_citations") or []
                        ),
                        "status": status_labels.get(
                            status_code, str(c.get("status"))
                        ),
                        "matched_case": cluster_names or None,
                        "error": c.get("error_message") or None,
                    })
                st.success(
                    f"Found {len(cit_results)} citation(s). "
                    f"{sum(1 for c in cit_results if c.get('status') == 200)} verified, "
                    f"{sum(1 for c in cit_results if c.get('status') == 404)} not in CL, "
                    f"{sum(1 for c in cit_results if c.get('status') == 300)} ambiguous."
                )
                cit_df = pd.DataFrame(cit_rows)
                st.dataframe(cit_df, hide_index=True, use_container_width=True)


st.divider()
with st.expander("Save profile / Schedule recurring runs", expanded=False):
    st.write(
        "Save the current form values as a JSON profile, then use launchd "
        "(macOS) to run them on a schedule. The API key is **not** written to "
        "the profile — it stays in `GC_AI_API_KEY` and is injected by launchd."
    )
    profile_name = st.text_input(
        "Profile name", value="demo-entity-001",
        help="Saved as profiles/<name>.json next to app.py.",
    )

    project_dir = Path(__file__).resolve().parent
    profiles_dir = project_dir / "profiles"
    safe_name = (
        re.sub(r"[^A-Za-z0-9._-]+", "-", profile_name.strip()).strip("-")
        or "unnamed"
    )
    profile_path = profiles_dir / f"{safe_name}.json"

    save_col, schedule_col = st.columns(2)

    if save_col.button("Save profile", use_container_width=True):
        if not user_agent or "@" not in user_agent:
            save_col.error("Set the SEC User-Agent first.")
        elif not profile_name.strip():
            save_col.error("Profile name is required.")
        else:
            profile_data = {
                "name": profile_name.strip(),
                "user_agent": user_agent.strip(),
                "query": query.strip(),
                "forms": forms or DEFAULT_FORMS,
                "no_exhibits": not include_exhibits,
                "start": start.strip() or None,
                "end": end.strip() or None,
                "max_filings": int(max_filings) if max_filings > 0 else None,
                "output": str(Path(output_dir.strip() or "./contracts").resolve()),
                "gc_ai_base_url": gc_ai_base_url.strip() or GC_AI_DEFAULT_BASE_URL,
                "gc_ai_folder_id": (
                    gc_ai_folder_id.strip()
                    if folder_mode == "Use existing folder ID"
                    else None
                ),
                "gc_ai_folder_name": (
                    gc_ai_folder_name.strip()
                    if folder_mode == "Create new folder"
                    else None
                ),
                "gc_ai_folder_description": gc_ai_folder_description.strip() or None,
            }
            profiles_dir.mkdir(exist_ok=True)
            profile_path.write_text(
                json.dumps(profile_data, indent=2), encoding="utf-8"
            )
            save_col.success(f"Saved {profile_path}")

    show_plist = schedule_col.button(
        "Show launchd plist", use_container_width=True,
        help="Generates a macOS launchd schedule snippet you can install.",
    )

    if show_plist:
        if not profile_path.exists():
            st.warning(
                f"No profile yet at {profile_path}. Click 'Save profile' first."
            )
        else:
            python_path = project_dir / ".venv" / "bin" / "python"
            script_path = project_dir / "sec_contract_scanner.py"
            log_path = (
                Path.home() / "Library" / "Logs"
                / f"contractreview-{safe_name}.log"
            )
            label = f"com.contractreview.{safe_name}"
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
        <string>{profile_path}</string>
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
            st.write(
                f"**Save this as** `{plist_path}` "
                "(adjust day/hour to taste — the example runs Monday 8am)."
            )
            st.code(plist, language="xml")
            st.write("**Then install with:**")
            st.code(
                f"chmod 600 {plist_path}\n"
                f"launchctl bootstrap gui/$(id -u) {plist_path}",
                language="bash",
            )
            st.write("**To remove:**")
            st.code(
                f"launchctl bootout gui/$(id -u) {plist_path}\n"
                f"rm {plist_path}",
                language="bash",
            )
            st.warning(
                "The displayed plist always contains a placeholder so a live "
                "key cannot leak into copied output or screenshots. Replace "
                "`PASTE_YOUR_USER_SCOPED_KEY_HERE` locally before saving it."
            )
            st.caption(
                "The plist holds your API key in plaintext. `chmod 600` makes "
                "it readable only by your user account."
            )
