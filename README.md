# Item 105 Integrity Engine - Public Portfolio Edition

A de-identified, runnable evidence-builder for an Item 105 disclosure-integrity workflow. It assembles SEC contracts and disclosure baselines, retrieves public litigation materials, extracts structured claim signals, preserves source metadata, and packages the resulting workspace for human-reviewed disclosure analysis.

The public tree contains the deterministic acquisition, normalization, and triage layer together with a sanitized copy of the Item 105 decision playbook. The included Skill definition is designed to run as a separate execution context inside GC AI. The application prepares and audits the evidence; the Skill structures the analysis, which remains subject to human legal review.

Core workflow: `source document -> preserved evidence -> exposure signal -> disclosure baseline -> gap-review workspace -> counsel action`.

## Included components

- `nicegui_app.py`: one-click workspace orchestration for an issuer.
- `sec_contract_scanner.py`: EDGAR acquisition, source metadata, manifesting, and optional GC AI handoff.
- `disclosure_sections.py`: deterministic extraction of Risk Factors, MD&A, and financial-statement baselines.
- `claim_extractor.py`: structured asserted-claim signals from public docket text.
- `pacer_client.py` and `courtlistener_client.py`: public-litigation discovery and retrieval.
- [`item_105_disclosure_integrity_skill.txt`](item_105_disclosure_integrity_skill.txt): sanitized public version of the Item 105 disclosure-integrity playbook delivered with the May 4, 2026 assessment.
- `WIREFRAME.md`: architecture, system boundary, and the complete handoff model.

> **Public portfolio note:** This is a sanitized current-state edition. It contains synthetic examples only and excludes client records, credentials, private branches, and private development history. The internal component name `ContractReview` remains in module and command names because that component performs evidence acquisition for the broader Item 105 workflow.

The tool searches three independent sources, all routing into the same
staging-and-pick → GC AI upload pipeline:

| Source | What it covers | Cost |
| --- | --- | --- |
| **SEC EDGAR** | Public-company filings + their attached exhibits | Free |
| **PACER Case Locator** | Federal court case index (every district + bankruptcy court) | $0.10 / page in Production; QA is free |
| **CourtListener / RECAP** | Federal court documents previously paid for via PACER | Free |

The default SEC search is **comprehensive** — any non-boilerplate exhibit
whose first ~3KB contains an agreement-style header (`AGREEMENT`,
`INDENTURE`, `GUARANTY`, etc.) is captured. Subsidiary lists, auditor
consents, SOX certifications, and mine-safety disclosures
(Exhibits 21–25, 31, 32, 95) are always skipped. The PACER and
CourtListener integrations let you cross-reference that haul with
litigation records and pull court-archived PDFs.

## Module structure

If you ever want to extract PACER or CourtListener to their own
package / branch, the modules are deliberately decoupled:

| File | Purpose | Depends on |
| --- | --- | --- |
| `sec_contract_scanner.py` | SEC EDGAR scanner + GC AI client + CLI | `requests`, `bs4` only |
| `pacer_client.py` | PACER auth + Case Locator search | `requests` only |
| `courtlistener_client.py` | CourtListener REST v4 client (search, dockets, prayers, citations) | `requests` only |
| `app.py` | Streamlit GUI tying all three together | All of the above |

Each client module is fully standalone — no cross-imports between
`sec_contract_scanner.py`, `pacer_client.py`, and `courtlistener_client.py`.
The GUI is the only place where they meet.

## Install

```bash
# Streamlit GUI (default)
pip install -e ".[gui]"

# OR — NiceGUI alternative (closer to gc.ai's chrome)
pip install -e ".[nicegui]"
```

Requires Python 3.10+. On macOS the system Python (3.9) won't work;
install 3.12 from python.org or via Homebrew. (`pip install -r
requirements.txt` also works for the Streamlit path but doesn't pull
in `pandas`, which the Streamlit GUI needs for table rendering — use
the `pyproject.toml` extras above.)

## GUI

Two equivalent front-ends — pick one based on which install extra you
chose. Both share the same backend modules and manifest schema; you
can switch freely between them.

```bash
# Streamlit
streamlit run app.py

# NiceGUI (Quasar-themed, hosted on FastAPI)
python nicegui_app.py
```

Streamlit prints a `Local URL` (default `http://localhost:8501`); the
NiceGUI app serves on `http://localhost:8080`.

The sidebar collects credentials. Each section is optional — sections
of the page hide themselves if you haven't configured the relevant
credentials, so you can use any subset of the three sources:

- **SEC User-Agent** (always required if you want to scan SEC):
  `DEMO_OPERATOR contact@example.invalid` is a non-routable placeholder;
  replace it with the descriptive name and real contact email the SEC
  requires on every request.
- **GC AI API key** (only if you want to upload contracts to a GC AI
  folder): user-scoped `u:gcai_...` key from
  `Settings → Organization → API Keys` in your GC AI workspace.
  Pre-fills from the `GC_AI_API_KEY` env var.
- **PACER login** (only if you want PACER PCL search): username,
  password, optional MFA OTP, optional client code. Choose QA (free
  test environment) or Production (billable).
- **CourtListener token** (only if you want RECAP search or citation
  lookup): free 40-char token from
  <https://www.courtlistener.com/profile/api-token/>. Pre-fills from
  the `COURTLISTENER_TOKEN` env var.

The main panel has, in order:

1. **SEC scan controls** + **Run scan** button
2. **Staged candidates** picker (appears after a Preview-mode scan or
   after staging RECAP PDFs) — pick rows, **Save selected locally**,
   or **Save & upload to GC AI**
3. **Saved contracts** table (the main `manifest.jsonl`)
4. **PACER Case Locator search** (gated on PACER login)
5. **CourtListener / RECAP search** (gated on CL token) — searches the
   open `/search/?type=r` endpoint, presents results with clickable
   PDFs, lets you stage selected PDFs into the same pipeline as SEC
   contracts, or send Pray-and-Pay requests for filings not yet in
   RECAP
6. **Citation lookup & verification** (gated on CL token) — paste
   contract text, get back every citation Eyecite finds with a
   verification status (`✓ found`, `✗ not in CL`, `? ambiguous`)
7. **Save profile / Schedule recurring runs** (macOS launchd)

## CLI (SEC scanner only)

The CLI exposes the SEC scanner only — PACER and CourtListener are
GUI-only. SEC requires a descriptive `User-Agent` (your name and a
contact email).

```bash
python sec_contract_scanner.py \
    --user-agent "DEMO_OPERATOR contact@example.invalid" \
    -q "DEMO_ENTITY_001" \
    -o ./contracts
```

Optional flags:

| Flag | Description |
| --- | --- |
| `-q`, `--query` | Phrase to search (default `DEMO_ENTITY_001`, an unmistakably synthetic token) |
| `-o`, `--output` | Output directory (default `./contracts`) |
| `--forms` | Form types to search (default `10-K 10-Q 8-K S-1 S-4 20-F`) |
| `--start` / `--end` | Date range as `YYYY-MM-DD` |
| `--max-filings` | Stop after N filings (`0` = no limit) |
| `--exhibits` | Exhibit numbers to narrow to (e.g. `10`). Empty = comprehensive default. |
| `--no-exhibits` | Skip exhibits entirely; download only the primary filing body. |
| `--profile` | Load all settings from a saved profile JSON. |

GC AI integration flags:

| Flag | Description |
| --- | --- |
| `--gc-ai-key` | API key (or set `GC_AI_API_KEY`). Must be user-scoped (`u:gcai_...`). |
| `--gc-ai-folder-name` | Create a new folder with this name and upload into it. |
| `--gc-ai-folder-id` | Upload into an existing folder by UUID. |
| `--gc-ai-folder-description` | Optional description on folder creation. |
| `--gc-ai-parent-folder-id` | Nest the new folder under this parent. |
| `--gc-ai-base-url` | Override the API base URL. |

```bash
export GC_AI_API_KEY="u:gcai_xxxxxxxxxxxxxxxxxxxx"

# First run: create a new folder and stream uploads into it.
python sec_contract_scanner.py \
    --user-agent "DEMO_OPERATOR contact@example.invalid" \
    -q "DEMO_ENTITY_001" \
    --gc-ai-folder-name "DEMO_ENTITY_001 — SYNTHETIC"

# Subsequent runs: reuse the existing folder.
python sec_contract_scanner.py \
    --user-agent "DEMO_OPERATOR contact@example.invalid" \
    -q "DEMO_ENTITY_001" \
    --gc-ai-folder-id "<folder-uuid-from-first-run>"
```

What gets recorded per upload (in `manifest.jsonl`):

- `source` — `sec` (SEC EDGAR scan) or `recap` (when staged from
  CourtListener)
- `accession`, `cik`, `form`, `filed`, `company`, `document`,
  `exhibit`, `url`, `saved_as`
- `gc_ai_file_id`, `gc_ai_folder_id`, `gc_ai_status` —
  if uploaded
- `gc_ai_error` — if the upload failed; local file still saved
- `match_via_raw_bytes` — `true` when the phrase was only found in
  the raw bytes (likely inline XBRL or HTML attributes)

## PACER PCL search (GUI)

Sign up for a PACER account at <https://pacer.uscourts.gov> (production)
or <https://qa-pacer.uscourts.gov> (test environment, free). Default to
**QA** in the GUI while you're learning — searches there are free.

The PACER section searches the federal court case index by **party /
entity name** (for example, search for the token `DEMO_ENTITY_001`) or
**case title / number**. Filter by jurisdiction (Bk / Civil
/ Crim / Appellate / MDL), court IDs (PCL Appendix A codes — e.g.
`nysdc` or `cacdc`), and date range.

In Production, an explicit per-search billing-confirmation checkbox
must be ticked before the Run button enables. Each search page is
$0.10. Results show filed date, court, case number, title, party,
role, plus a clickable **CM/ECF link** that opens the case in the
originating court's docket system. Token rotation is handled
automatically.

## CourtListener / RECAP search (GUI)

Get a free token at <https://www.courtlistener.com/profile/api-token/>.
Free-tier limits: 5 req/min, 50/hr, 125/day.

The CL section searches RECAP — federal court documents that have been
fetched from PACER by anyone, mirrored for free. Search by phrase
(Lucene-style query syntax supported), party name, court IDs (RECAP's
codes — `nysd`, `cacd`, etc.), date range. Pick **Dockets** granularity
(one row per case) or **Documents** (one row per filing); order by
relevance or date.

Each result row shows filed date, court, case, docket #, snippet (with
match terms highlighted via `<mark>` then stripped for display), and
two clickable links: **Open on CourtListener** and **PDF**.

Below the table, four action buttons:

- **Stage N PDF(s)** — downloads selected RECAP PDFs into `_staging/`
  and adds rows to the staging picker (further up the page) for
  commit / GC AI upload, exactly like SEC scanner contracts.
- **Pray for N doc(s)** — for selected rows whose PDF isn't in RECAP
  yet, creates free Pray-and-Pay requests. CourtListener emails you
  when a document becomes available. Subject to your daily prayer
  quota.
- **Next page** — cursor-based pagination.
- **Clear results** — drops the cached result set.

Court IDs differ slightly between PACER PCL and CourtListener (e.g.
PACER's `azb` is CL's `arb`); the client translates the four
documented exceptions automatically.

## Citation Lookup & Verification (GUI)

Paste contract text into the **Citation lookup & verification**
expander at the bottom of the page. Eyecite extracts every citation;
each is then verified against CourtListener's case-law database.
Status per citation:

- `✓ found` — citation parsed and matches a CourtListener case
- `✗ not in CL` — citation parsed but no match in the CL database
- `? ambiguous` — citation matches multiple cases (e.g. ambiguous
  reporter abbreviation)
- `✗ unknown reporter` — looks like a citation but the reporter
  abbreviation isn't valid
- `⏸ throttled` — past the 250-citation-per-request cap

Useful as a guardrail against hallucinated citations in AI-drafted
agreements or stale references in inherited templates.

Throttle: 60 valid citations / minute, 250 per request, 64 KB text.

## Scheduled re-runs (macOS, SEC scanner only)

A profile + launchd lets the SEC scanner run on a schedule (e.g.
Monday 8am) and pick up only new filings — the manifest dedupes
everything already downloaded and uploaded.

1. In the GUI, fill in all the SEC form fields the way you'd run them.
2. Open the **"Save profile / Schedule recurring runs"** expander at
   the bottom. Pick a profile name, click **Save profile**. A
   `profiles/<name>.json` is written. The API key is *not* stored in
   it.
3. Click **Show launchd plist**. The page renders an XML snippet with
   all absolute paths filled in, plus the install/remove commands. The
   API-key field is deliberately a placeholder, even if the environment
   already contains a live key.
4. Copy the XML into
   `~/Library/LaunchAgents/com.contractreview.<name>.plist`, replace the
   placeholder locally, and run the install commands shown.
5. Edit the `StartCalendarInterval` dict if you want a different
   cadence (omit `Weekday` to run daily, etc.). See `man
   launchd.plist`.

The plist injects `GC_AI_API_KEY` as an environment variable, so the
scheduled run can upload to GC AI without you being logged in. Treat
the plist file as a secret — `chmod 600` is included in the install
snippet.

To run the same profile manually (for testing the schedule):

```bash
.venv/bin/python sec_contract_scanner.py --profile profiles/<name>.json
```

PACER and CourtListener searches are GUI-only and don't have a
scheduled-run path.

## How the SEC scan works

1. Hits the EDGAR full-text search API
   (`https://efts.sec.gov/LATEST/search-index`) for the quoted phrase,
   paging through every hit (cursor-based).
2. For each filing, lists the documents in its archive directory.
3. **If exhibits are enabled** (default): each `.htm`/`.html`/`.txt`
   attachment is checked. The exhibit number is parsed by longest-
   prefix-matching against SEC's known top-level exhibits (so
   `dex991.htm` resolves to Ex 99 and `ex21.htm` to Ex 21), and
   boilerplate exhibits (Ex 21, 22, 23, 24, 25, 31, 32, 95) are
   always skipped. **If exhibits are disabled**: only the primary
   filing body (the 10-K text itself, the 8-K body, etc.) is
   considered.
4. Each candidate is downloaded and its text is extracted (BeautifulSoup
   with UnicodeDammit — handles UTF-8, Windows-1252, ISO-8859-1). The
   scanner keeps only documents whose rendered text contains the
   phrase, with a raw-bytes fallback so phrases hidden in inline XBRL
   tags or HTML attributes still match.
5. Surviving documents are written to disk and (if configured)
   uploaded to a GC AI folder.
6. A JSONL `manifest.jsonl` is appended so re-runs skip already-
   downloaded documents. Cross-manifest dedup means a preview-mode
   scan also skips contracts already committed in previous runs.

Throttled to stay under the SEC's 10 req/sec limit, with up to 4
exponential-backoff retries on transient 5xx and 429 responses.

## Privacy & security notes

- **GC AI API key** lives only in the env var `GC_AI_API_KEY` or the
  password field in the sidebar — never written to disk via this app.
  The launchd preview always renders a placeholder, so a live key cannot
  leak into copied output or screenshots. If you replace the placeholder
  in a local plist, the install snippet includes `chmod 600` to restrict
  file access.
- **PACER credentials** are kept in Streamlit session state for the
  duration of your browser session. The token is sent to PACER's
  authentication endpoint over HTTPS. The app never logs or persists
  the password.
- **CourtListener token** is stored in session state per user
  session; pre-fills from `COURTLISTENER_TOKEN` env var if set.
- **`profiles/` is gitignored** — saved scan profiles never end up on
  GitHub.
