"""Scan SEC EDGAR filings for contracts containing a target phrase and download them.

Default target phrase: "DEMO_ENTITY_001" (synthetic token).

Usage:
    python sec_contract_scanner.py --user-agent "DEMO_OPERATOR contact@example.invalid"
    python sec_contract_scanner.py -q "DEMO_ENTITY_001" -o ./contracts \\
        --start 2098-01-01 --end 2099-12-31 --user-agent "DEMO_OPERATOR contact@example.invalid"

The SEC requires a descriptive User-Agent (name + contact email) for all
programmatic access. See https://www.sec.gov/os/accessing-edgar-data.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import fcntl  # POSIX only; Windows falls back to no-op
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_ARCHIVES = "https://www.sec.gov/Archives/"

EXHIBIT_PREFIX_RE = re.compile(r"^d?ex[-_]?(?=\d)", re.IGNORECASE)
EXHIBIT_DIGITS_RE = re.compile(r"^(\d+)")
EXHIBIT_EXT_RE = re.compile(r"\.(htm|html|txt)$", re.IGNORECASE)
EX99_AGREEMENT_RE = re.compile(
    r"\b(AGREEMENT|CONTRACT|INDENTURE|GUARANT(?:Y|EE)|LEASE|"
    r"PROMISSORY NOTE|PLEDGE|SECURITY AGREEMENT|MERGER|"
    r"SUPPLEMENT(?:AL)? INDENTURE|AMENDMENT)\b"
)

# SEC's standard top-level exhibit numbers. Used to disambiguate filenames
# like 'dex991.htm' (Ex 99-1) vs 'ex21.htm' (Ex 21, Subsidiaries).
KNOWN_EXHIBITS = frozenset(
    {"2", "3", "4", "5", "7", "8", "9", "10", "11", "12", "13", "14", "15",
     "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27",
     "28", "29", "30", "31", "32", "33", "34", "95", "96", "97", "98", "99"}
)

# Exhibits that never contain real agreements: subsidiary lists, auditor
# consents, Sarbanes-Oxley certifications, mine safety disclosures, etc.
BOILERPLATE_EXHIBITS = frozenset(
    {"21", "22", "23", "24", "25", "31", "32", "95"}
)

# Exhibits whose category alone tells us they're agreements — Ex 2 (plans of
# acquisition / M&A), Ex 4 (debt instruments), Ex 9 (voting trust agreements),
# Ex 10 (material contracts). For these, skip the heuristic agreement-keyword
# filter so a contract whose title isn't in the first 3KB still gets captured.
KNOWN_CONTRACT_EXHIBITS = frozenset({"2", "4", "9", "10"})

# Default = empty (broad). When the user doesn't narrow exhibits, every
# non-boilerplate exhibit that contains the phrase AND looks like an
# agreement is captured.
DEFAULT_EXHIBITS: list[str] = []

REQUEST_DELAY_SECONDS = 0.15

GC_AI_DEFAULT_BASE_URL = "https://app.gc.ai/api/external/v1"

# Manifest 'exhibit' value used when the row is the filing body itself.
PRIMARY_DOC_LABEL = "primary"


@dataclass
class Filing:
    accession: str
    cik: str
    form: str
    filed: str
    company: str

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")

    @property
    def index_url(self) -> str:
        # SEC archive paths use the CIK with leading zeros stripped.
        cik = self.cik.lstrip("0") or self.cik
        return f"{EDGAR_ARCHIVES}edgar/data/{cik}/{self.accession_nodash}/"


class EdgarClient:
    def __init__(self, user_agent: str) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "SEC requires a User-Agent with a contact email, e.g. "
                "'DEMO_OPERATOR contact@example.invalid' (replace before use)."
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )

    def _get(self, url: str, **kwargs) -> requests.Response:
        time.sleep(REQUEST_DELAY_SECONDS)
        last_exc: requests.HTTPError | None = None
        for attempt in range(4):
            response = self.session.get(url, timeout=30, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            last_exc = requests.HTTPError(
                f"{response.status_code} {response.reason} for url: {response.url}",
                response=response,
            )
            backoff = 2 ** attempt
            print(
                f"  ! EDGAR returned {response.status_code}; "
                f"retrying in {backoff}s (attempt {attempt + 1}/4)",
                file=sys.stderr,
            )
            time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    def search(
        self,
        query: str,
        forms: Iterable[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        ciks: Iterable[str] | str | None = None,
    ) -> Iterator[Filing]:
        # Strip embedded double quotes; we wrap the phrase in our own quotes
        # for EDGAR phrase search, and a literal " in the user's query would
        # break that wrapping (e.g. DEMO_ENTITY_001).
        sanitized = query.replace('"', "").strip()
        from_index = 0
        page_size = 10
        announced_total = False
        # EDGAR's full-text search returns hits at the *document* level, so
        # the same filing can appear multiple times if more than one of its
        # documents matches the phrase. Dedupe so the caller sees each
        # filing once.
        seen_accessions: set[str] = set()
        while True:
            params = {
                "q": f'"{sanitized}"',
                "from": from_index,
                "size": page_size,
            }
            if forms:
                # Pass each form as a separate forms= parameter rather than a
                # comma-separated string. EDGAR's full-text endpoint accepts
                # both, but the multi-value form is more reliable when the
                # list is long — a comma-separated list with 20+ values has
                # been observed to silently return a subset of hits.
                params["forms"] = list(forms)
            if ciks:
                # Restrict the search to filings BY a specific issuer (or
                # set of issuers). Without this filter, EDGAR's full-text
                # search returns every filing in the corpus that contains
                # the phrase — including third-party filings that name the
                # issuer (e.g. a competitor's S-4 mentioning the company).
                # For Disclosure Integrity Engine workspace builds, scoping
                # to the issuer's own filings is what you want.
                if isinstance(ciks, str):
                    params["ciks"] = ciks
                else:
                    params["ciks"] = list(ciks)
            if start:
                params["dateRange"] = "custom"
                params["startdt"] = start
            if end:
                params["dateRange"] = "custom"
                params["enddt"] = end

            response = self._get(EDGAR_SEARCH_URL, params=params)
            payload = response.json()
            hits = payload.get("hits", {}).get("hits", [])
            total = payload.get("hits", {}).get("total", {}).get("value", 0)
            if not announced_total:
                print(f"EDGAR full-text search: {total} matching filing(s).")
                # Print the resolved URL on every first search so users
                # can verify in the log exactly which filters EDGAR
                # received (e.g. confirm the ciks= param made it).
                print(f"  Request URL: {response.url}")
                if total == 0:
                    print(
                        "  If EDGAR's web UI shows hits for this URL but the "
                        "scanner doesn't, the response was likely served "
                        "post-retry from a degraded cache. Re-run the scan; "
                        "if it persists, try removing the form filter or "
                        "narrowing the phrase."
                    )
                announced_total = True
            if not hits:
                return

            for hit in hits:
                source = hit.get("_source", {})
                accession_with_doc = hit.get("_id", "")
                accession = accession_with_doc.split(":", 1)[0]
                ciks = source.get("ciks") or []
                companies = source.get("display_names") or []
                if not accession or not ciks:
                    # Some EDGAR responses occasionally omit either the
                    # accession or the CIK. Without both we can't build the
                    # archive URL, so skip rather than crash the run.
                    continue
                if accession in seen_accessions:
                    # Same filing matched on a different document; we'll walk
                    # all of its documents ourselves, no need to revisit.
                    continue
                seen_accessions.add(accession)
                yield Filing(
                    accession=accession,
                    cik=ciks[0],
                    form=source.get("form", ""),
                    filed=source.get("file_date", ""),
                    company=companies[0] if companies else "",
                )

            from_index += page_size
            if from_index >= total:
                return

    def list_filing_documents(self, filing: Filing) -> list[dict]:
        """List the documents in a filing with their SEC document types.

        Multiple indexes exist for every EDGAR filing; only some carry
        proper SEC submission types. We try them in order of reliability
        and log which one answered so users can spot a misclassification
        cause from the run log.
        """
        accession_dashed = filing.accession

        # 1) Filing-level XML.
        xml_url = urljoin(
            filing.index_url, f"{accession_dashed}-index.xml"
        )
        try:
            response = self._get(xml_url)
            items = _parse_filing_index_xml(response.content)
            if items:
                print(f"    [index=xml] {len(items)} doc(s)")
                return items
        except (requests.HTTPError, requests.RequestException):
            pass

        # 2) Filing-level JSON.
        json_url = urljoin(
            filing.index_url, f"{accession_dashed}-index.json"
        )
        try:
            response = self._get(json_url)
            items = response.json().get("directory", {}).get("item", [])
            # Accept this path only if the items have non-icon-class
            # types (i.e. anything other than the icon-gif filenames).
            if items and any(
                (it.get("type") or "").upper() not in
                ("", "TEXT.GIF", "HTML.GIF", "XML.GIF", "FOLDER.GIF")
                for it in items
            ):
                print(f"    [index=json] {len(items)} doc(s)")
                return items
        except (requests.HTTPError, requests.RequestException):
            pass

        # 3) Filing-level HTML (always exists; parsed from EDGAR web UI).
        htm_url = urljoin(
            filing.index_url, f"{accession_dashed}-index.htm"
        )
        try:
            response = self._get(htm_url)
            items = _parse_filing_index_html(response.content)
            if items:
                print(f"    [index=htm] {len(items)} doc(s)")
                return items
        except (requests.HTTPError, requests.RequestException):
            pass

        # 4) Directory listing (last resort, types are icon classes).
        directory_index_url = urljoin(filing.index_url, "index.json")
        response = self._get(directory_index_url)
        items = response.json().get("directory", {}).get("item", [])
        print(f"    [index=dir-fallback] {len(items)} doc(s) — types are icon classes, not SEC types")
        return items

    def download(self, url: str) -> bytes:
        return self._get(url).content

    def get_recent_filings(
        self,
        cik: str,
        forms: Iterable[str] | None = None,
        *,
        limit: int = 1,
    ) -> list[Filing]:
        """Look up an issuer's most recent filings of a given type.

        Uses the SEC's ``/submissions/CIK<10-digit>.json`` feed, which
        is the canonical per-issuer filing history endpoint (no
        full-text search, no phrase). Returns up to ``limit`` filings,
        newest first, optionally filtered to specific form types.

        Used for the Disclosure Integrity Engine baseline pull: given
        a CIK, fetch the latest 10-K / 20-F to extract Risk Factors
        + MD&A + Financial Statements from.
        """
        cik_clean = (cik or "").lstrip("0").strip()
        if not cik_clean.isdigit():
            raise ValueError(f"CIK must be all-digits; got {cik!r}")
        cik_padded = cik_clean.zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        response = self._get(url)
        payload = response.json()
        company = payload.get("name", "")
        recent = payload.get("filings", {}).get("recent", {}) or {}
        accessions = recent.get("accessionNumber") or []
        forms_list = recent.get("form") or []
        dates = recent.get("filingDate") or []
        # `is not None` so an explicit empty list filters everything
        # out (returns []). `if forms:` would treat [] as "no filter"
        # and silently return every form — divergent from the caller's
        # intent.
        wanted = (
            {f.upper() for f in forms} if forms is not None else None
        )

        out: list[Filing] = []
        for accession, form, filed in zip(accessions, forms_list, dates):
            if wanted is not None and form.upper() not in wanted:
                continue
            out.append(
                Filing(
                    accession=accession,
                    cik=cik_clean,
                    form=form,
                    filed=filed,
                    company=company,
                )
            )
            if len(out) >= limit:
                break
        return out


class GCAIClient:
    """Minimal client for the GC AI External API (folders + files)."""

    def __init__(self, api_key: str, base_url: str | None = GC_AI_DEFAULT_BASE_URL) -> None:
        if not api_key:
            raise ValueError("A GC AI API key is required.")
        # Callers (GUIs) often pass `gc_base or None` when the field is
        # blank; treat None / "" as "use the default".
        self.base_url = (base_url or GC_AI_DEFAULT_BASE_URL).rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = api_key

    def _request_with_retry(
        self,
        request_fn: Callable[[], requests.Response],
        label: str,
    ) -> requests.Response:
        """Call request_fn() up to 4 times, retrying with exponential backoff
        on 5xx and 429 responses. The callable is re-invoked from scratch on
        each attempt so callers that need to re-open a file (multipart upload)
        can do so inside the function body."""
        last_exc: requests.HTTPError | None = None
        for attempt in range(4):
            response = request_fn()
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            last_exc = requests.HTTPError(
                f"{response.status_code} {response.reason} for url: {response.url}",
                response=response,
            )
            backoff = 2 ** attempt
            print(
                f"  ! GC AI {label} returned {response.status_code}; "
                f"retrying in {backoff}s (attempt {attempt + 1}/4)",
                file=sys.stderr,
            )
            time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    def create_folder(
        self,
        name: str,
        description: str | None = None,
        parent_folder_id: str | None = None,
    ) -> dict:
        body: dict = {"name": name}
        if description:
            body["description"] = description
        if parent_folder_id:
            body["parent_folder_id"] = parent_folder_id
        response = self._request_with_retry(
            lambda: self.session.post(
                f"{self.base_url}/folders", json=body, timeout=30
            ),
            label="create_folder",
        )
        return response.json()

    def upload_file(self, path: Path, folder_id: str | None = None) -> dict:
        def do_upload() -> requests.Response:
            with path.open("rb") as fh:
                files = {"file": (path.name, fh, _guess_content_type(path))}
                data = {"folder_id": folder_id} if folder_id else {}
                return self.session.post(
                    f"{self.base_url}/files",
                    files=files,
                    data=data,
                    timeout=300,
                )
        response = self._request_with_retry(do_upload, label=f"upload {path.name}")
        return response.json()


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".htm": "text/html",
        ".html": "text/html",
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    }.get(suffix, "application/octet-stream")


def match_exhibit(
    filename: str,
    allowed: Iterable[str] | None,
) -> str | None:
    """Return the SEC exhibit number if `filename` is a candidate exhibit,
    else None.

    - If `allowed` is None or empty, accepts any non-boilerplate exhibit
      (subsidiary lists, certifications, auditor consents, etc. are always
      skipped).
    - If `allowed` is a list/set, only those numbers are accepted.

    Handles both separated names ('ex-10.1.htm') and concatenated ones
    ('dex991.htm' -> Ex 99-1) by longest-prefix-matching against the set of
    known SEC top-level exhibit numbers.
    """
    base = filename.lower()
    stem = EXHIBIT_EXT_RE.sub("", base)
    if stem == base:
        return None  # filename had no .htm/.html/.txt extension
    prefix = EXHIBIT_PREFIX_RE.match(stem)
    if not prefix:
        return None
    digits_match = EXHIBIT_DIGITS_RE.match(stem[prefix.end():])
    if not digits_match:
        return None
    digits = digits_match.group(1)
    matched: str | None = None
    for n in range(len(digits), 0, -1):
        candidate = digits[:n]
        if candidate in KNOWN_EXHIBITS:
            matched = candidate
            break
    if matched is None:
        return None
    if matched in BOILERPLATE_EXHIBITS:
        return None
    allowed_set = set(allowed) if allowed else None
    if allowed_set is not None and matched not in allowed_set:
        return None
    return matched


_SEC_HEADER_RE = re.compile(
    r"<SEC-HEADER>(.*?)</SEC-HEADER>", re.DOTALL | re.IGNORECASE
)
_DOCUMENT_BLOCK_RE = re.compile(
    r"<DOCUMENT>(.*?)</DOCUMENT>", re.DOTALL | re.IGNORECASE
)
_DOC_TYPE_RE = re.compile(r"<TYPE>([^\s<]+)", re.IGNORECASE)
_DOC_FILENAME_RE = re.compile(r"<FILENAME>([^\s<]+)", re.IGNORECASE)


def _describe_txt_match(content: bytes, phrase: str) -> str | None:
    """For an SEC submission .txt file, identify whether a phrase appears
    in SEC-HEADER metadata or inside a specific <DOCUMENT> section. Return
    a short human-readable description, or None when the phrase is absent
    from the parseable structure.

    A header-only match is bibliographic metadata, not contract content.
    Surfacing that distinction prevents a metadata hit from being mistaken
    for a responsive document.
    """
    try:
        text = content.decode("utf-8", errors="ignore")
    except Exception:
        return None
    phrase_lower = phrase.lower()
    if phrase_lower not in text.lower():
        return None

    header_match = _SEC_HEADER_RE.search(text)
    if header_match and phrase_lower in header_match.group(1).lower():
        for line in header_match.group(1).splitlines():
            if phrase_lower in line.lower():
                snippet = line.strip()
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."
                return f"SEC-HEADER metadata: {snippet!r}"
        return "SEC-HEADER metadata"

    for doc_match in _DOCUMENT_BLOCK_RE.finditer(text):
        block = doc_match.group(1)
        if phrase_lower not in block.lower():
            continue
        parts = []
        type_m = _DOC_TYPE_RE.search(block)
        if type_m:
            parts.append(f"type={type_m.group(1)}")
        fn_m = _DOC_FILENAME_RE.search(block)
        if fn_m:
            parts.append(f"filename={fn_m.group(1)}")
        return f"<DOCUMENT> block ({', '.join(parts) or 'unknown'})"

    return "submission text outside <SEC-HEADER> and <DOCUMENT> blocks"


def _phrase_match(content: bytes, phrase: str) -> tuple[bool, bool]:
    """Look for `phrase` in a downloaded document.

    Returns (matched, via_raw_bytes_only):
      * (True, False)  -> phrase found in BeautifulSoup-extracted text
                          (the normal, clean path).
      * (True, True)   -> phrase missing from extracted text but present in
                          the raw bytes. Common when the phrase lives inside
                          inline XBRL (<ix:*> elements html.parser doesn't
                          extract cleanly), in HTML attributes, or in
                          <script>/<style> blobs. Save anyway and flag so
                          the user can inspect.
      * (False, False) -> not in this document at all.
    """
    phrase_lower = phrase.lower()
    text = extract_text(content)
    if phrase_lower in text.lower():
        return True, False
    if phrase_lower.encode("utf-8") in content.lower():
        return True, True
    return False, False


def looks_like_agreement(text: str) -> bool:
    """Heuristic: True if the first ~3KB of decoded text contains an
    agreement-style keyword (AGREEMENT, CONTRACT, INDENTURE, ...).

    Used in broad-mode scans to keep press releases and earnings exhibits
    out of the results, and always for Exhibit 99 even in narrow mode.
    """
    head = text[:3000].upper()
    return bool(EX99_AGREEMENT_RE.search(head))


def extract_text(content: bytes) -> str:
    """Decode `content` and return whitespace-normalized plain text.

    BeautifulSoup runs UnicodeDammit on the raw bytes, so this handles
    UTF-8, Windows-1252, ISO-8859-1, and the other encodings older SEC
    filings actually use — important because phrases containing em-dashes,
    smart quotes, or accented characters would otherwise silently fail to
    match after a naive utf-8 decode."""
    if not content:
        return ""
    try:
        soup = BeautifulSoup(content, "html.parser")
        text = soup.get_text(" ")
    except Exception:
        text = content.decode("utf-8", errors="ignore")
    return re.sub(r"\s+", " ", text).strip()


_FILENAME_MAX_LEN = 200  # leave headroom under the 255-byte filesystem cap


def _parse_filing_index_html(content: bytes) -> list[dict]:
    """Parse EDGAR's filing-level HTML index page (<accession>-index.htm)
    into the same dict shape as index.json.

    The page renders one or two ``<table class="tableFile">`` blocks
    listing each document with a Type column. Reliable across vintages
    going back many years.
    """
    soup = BeautifulSoup(content, "html.parser")
    out: list[dict] = []
    seen_names: set[str] = set()
    # SEC tags both the primary "Document Format Files" table and the
    # ancillary "Data Files" table with class="tableFile". We process
    # both — different filings split material across them.
    for table in soup.find_all("table", class_="tableFile"):
        # Build a header → column-index map (some tables omit columns).
        headers = []
        for th in table.find_all("th"):
            headers.append(th.get_text(strip=True).lower())
        if not headers:
            continue
        try:
            doc_idx = headers.index("document")
        except ValueError:
            continue
        type_idx = headers.index("type") if "type" in headers else None
        size_idx = headers.index("size") if "size" in headers else None
        desc_idx = (
            headers.index("description") if "description" in headers else None
        )
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < len(headers):
                continue
            link = cells[doc_idx].find("a")
            name = (link.get_text(strip=True) if link else "") or ""
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            record: dict = {"name": name}
            if type_idx is not None:
                record["type"] = cells[type_idx].get_text(strip=True)
            if size_idx is not None:
                record["size"] = cells[size_idx].get_text(strip=True)
            if desc_idx is not None:
                record["description"] = cells[desc_idx].get_text(strip=True)
            out.append(record)
    return out


def _parse_filing_index_xml(content: bytes) -> list[dict]:
    """Parse an EDGAR filing's <accession>-index.xml into the same dict
    shape as index.json's directory.item entries.

    Returns one dict per <document> element, with at least ``name``
    and ``type`` populated. ``type`` will hold the SEC submission type
    (e.g. ``EX-4.1``) — the field downstream classifiers actually need.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    out: list[dict] = []
    # The XML namespaces vary across vintages — search by local-name.
    for doc in root.iter():
        if doc.tag.rsplit("}", 1)[-1] != "document":
            continue
        record: dict = {}
        for child in doc:
            local = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if local == "filename":
                record["name"] = text
            elif local == "type":
                record["type"] = text
            elif local == "size":
                record["size"] = text
            elif local == "description":
                record["description"] = text
            elif local == "sequence":
                record["sequence"] = text
        if record.get("name"):
            out.append(record)
    return out


def safe_filename(*parts: str) -> str:
    joined = "_".join(parts)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", joined).strip("_")
    if len(cleaned) <= _FILENAME_MAX_LEN:
        return cleaned
    suffix = ""
    dot = cleaned.rfind(".")
    if dot >= 0 and dot >= len(cleaned) - 8:
        suffix = cleaned[dot:]
        cleaned = cleaned[:dot]
    keep = _FILENAME_MAX_LEN - len(suffix)
    return cleaned[:keep] + suffix


_RENDERED_XBRL_RE = re.compile(r"^r\d+\.htm$", re.IGNORECASE)


_NON_BODY_TYPES = frozenset({"GRAPHIC", "COVER", "ZIP"})

# Filename substrings (case-insensitive) that signal boilerplate exhibits
# regardless of the SEC `type` field. Some filers leave `type` blank for
# attachments named descriptively, so we can't rely on type alone — e.g.
# an issuer's '20-F Ex 8' may appear as 'sample-subsidiaries.htm' or another
# descriptive filename. Also catches 10-K Ex 21 / Ex 23 / Ex 95
# variants whose filenames don't follow the 'ex-' convention.
_BOILERPLATE_FILENAME_PATTERNS = (
    "subsidiar",          # subsidiaries / subsidiary lists (10-K Ex 21, 20-F Ex 8)
    "listofsubsid",
    "consentof",          # auditor / counsel consents (Ex 23 / Ex 24)
    "auditorconsent",
    "minesafety",         # mine safety disclosures (Ex 95)
    "302cert",            # SOX 302 certifications (Ex 31)
    "906cert",            # SOX 906 certifications (Ex 32)
    "ceocertification",
    "cfocertification",
)


def _looks_like_boilerplate_filename(filename_lower: str) -> bool:
    return any(p in filename_lower for p in _BOILERPLATE_FILENAME_PATTERNS)


def find_primary_documents(items: list[dict], form: str = "") -> list[str]:
    """Return all body-candidate HTML files for this filing, in priority order.

    Filters out anything tagged as an exhibit (`type` starting 'EX-')
    even if the filename doesn't follow the `ex-` convention — some
    filers use descriptive names like 'sample-subsidiaries.htm', and without the
    type check we'd misclassify them as filing bodies.

    Matches by the `type` field SEC publishes per item:
      1. Exact match on the filing's form (e.g. type='10-K' for a 10-K).
      2. Match on the base form when the filing is an amendment (form
         '10-K/A' falls back to type='10-K').
      3. Any other plausible HTML file (not an exhibit, not rendered XBRL,
         not a TOC index, not metadata) is included as a fallback so
         multi-page filing bodies are still scanned.
    """
    form_upper = form.upper().strip()
    base_form = form_upper.split("/A", 1)[0] if "/A" in form_upper else ""

    exact: list[str] = []
    base: list[str] = []
    fallback: list[str] = []

    for item in items:
        name = item.get("name", "")
        lower = name.lower()
        if not lower.endswith((".htm", ".html")):
            continue
        if EXHIBIT_PREFIX_RE.match(lower):
            continue
        if _RENDERED_XBRL_RE.match(lower):
            continue
        if "metalinks" in lower or "financialreport" in lower:
            continue
        if (
            lower.endswith("-index.htm")
            or lower.endswith("-index.html")
            or lower.endswith("-index-headers.html")
            or lower.endswith("-index-headers.htm")
            or lower.endswith("filingsummary.xml")
        ):
            continue
        if _looks_like_boilerplate_filename(lower):
            continue

        item_type = item.get("type", "").upper()
        # Reject anything SEC tagged as an exhibit or non-body asset,
        # regardless of filename.
        if item_type.startswith("EX-") or item_type in _NON_BODY_TYPES:
            continue

        if form_upper and item_type == form_upper:
            exact.append(name)
        elif base_form and item_type == base_form:
            base.append(name)
        else:
            fallback.append(name)

    return exact + base + fallback


_EXHIBIT_TYPE_RE = re.compile(r"^EX-(\d+)", re.IGNORECASE)


def match_exhibit_type(item_type: str, allowed: Iterable[str] | None) -> str | None:
    """Match an SEC `type` field like 'EX-10.1' or 'EX-21' to an exhibit
    number, applying the same boilerplate / allowed filtering as
    match_exhibit. Used to recognize exhibits whose filenames don't
    follow the 'ex-' convention (e.g. 'demo-contract.htm'
    that SEC tags as type='EX-4.1').

    SEC document exhibits run from EX-1 through EX-99. EX-100+ types
    are machine-readable structured-data exhibits — EX-101.INS /
    EX-101.SCH / EX-101.DEF / EX-101.LAB / EX-101.PRE are inline XBRL
    components, EX-104 is the Cover Page Interactive Data File. None
    of those are material contracts; reject them outright before the
    longest-prefix matching that would otherwise misclassify
    'EX-101.INS' as Ex 10.
    """
    if not item_type:
        return None
    m = _EXHIBIT_TYPE_RE.match(item_type)
    if not m:
        return None
    digits = m.group(1)
    # 3+ digit runs in the type field are XBRL / structured-data
    # exhibits (EX-101 family, EX-102, EX-104). Reject before any
    # prefix matching could collapse 101 → 10.
    if len(digits) >= 3:
        return None
    matched: str | None = None
    for n in range(len(digits), 0, -1):
        candidate = digits[:n]
        if candidate in KNOWN_EXHIBITS:
            matched = candidate
            break
    if matched is None or matched in BOILERPLATE_EXHIBITS:
        return None
    allowed_set = set(allowed) if allowed else None
    if allowed_set is not None and matched not in allowed_set:
        return None
    return matched


def find_primary_document(items: list[dict], form: str = "") -> str | None:
    """Backwards-compatible single-result wrapper."""
    docs = find_primary_documents(items, form=form)
    return docs[0] if docs else None


STAGING_DIRNAME = "_staging"


def scan_and_download(
    client: EdgarClient,
    phrase: str,
    output_dir: Path,
    forms: list[str],
    start: str | None,
    end: str | None,
    max_filings: int | None,
    gc_ai: GCAIClient | None = None,
    gc_ai_folder_id: str | None = None,
    exhibits: list[str] | None = None,
    include_exhibits: bool = True,
    staging_only: bool = False,
    ciks: Iterable[str] | str | None = None,
) -> None:
    """When staging_only is True the scan writes into <output_dir>/_staging and
    skips GC AI uploads, so the GUI can present matched contracts as a
    selection list before anything is committed to the final output dir or
    sent to GC AI. Use commit_selection() to apply the user's picks."""
    main_manifest_path: Path | None = None
    if staging_only:
        # Stash the main manifest path before overriding output_dir so we
        # can also seed the dedup set from contracts already committed.
        main_manifest_path = output_dir / "manifest.jsonl"
        output_dir = output_dir / STAGING_DIRNAME
        gc_ai = None
        gc_ai_folder_id = None
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.jsonl"
    seen = _load_seen(manifest_path)
    already_committed = 0
    if main_manifest_path is not None and main_manifest_path.exists():
        # Skip contracts the user already committed in a previous run.
        committed = _load_seen(main_manifest_path)
        already_committed = len(committed - seen)
        seen |= committed

    allowed_exhibits = list(exhibits) if exhibits else []
    broad_mode = not allowed_exhibits

    print(f"Searching EDGAR for filings containing: {phrase!r}")
    if forms:
        print(f"  Forms: {', '.join(forms)}")
    if start or end:
        print(f"  Date range: {start or '...'} to {end or '...'}")
    if ciks:
        ciks_label = ciks if isinstance(ciks, str) else ", ".join(ciks)
        print(f"  Restricted to CIK(s): {ciks_label}")
    if not include_exhibits:
        print("Document mode: primary filing body only (exhibits skipped).")
    elif broad_mode:
        print(
            "Document mode: primary filing body + every non-boilerplate "
            "exhibit. Ex 2/4/9/10 captured as-is; ambiguous exhibits and "
            "Ex 99 must contain an agreement-style header to pass."
        )
    else:
        print(
            f"Document mode: primary filing body + exhibits "
            f"{', '.join(allowed_exhibits)}."
        )
        if "99" in allowed_exhibits:
            print("  (Ex 99 is filtered to documents whose header looks like an agreement)")
    if gc_ai and gc_ai_folder_id:
        print(f"GC AI uploads enabled -> folder {gc_ai_folder_id}")
    if already_committed:
        print(
            f"  Skipping {already_committed} contract(s) already in the main "
            "manifest from previous runs."
        )
    count = 0
    downloaded = 0
    uploaded = 0
    with manifest_path.open("a", encoding="utf-8") as manifest:
        for filing in client.search(
            phrase, forms=forms, start=start, end=end, ciks=ciks,
        ):
            if max_filings is not None and count >= max_filings:
                break
            count += 1
            print(
                f"[{count}] {filing.filed} {filing.form:<8} "
                f"{filing.company} (acc {filing.accession})"
            )
            try:
                items = client.list_filing_documents(filing)
            except requests.HTTPError as exc:
                print(f"  ! could not list documents: {exc}", file=sys.stderr)
                continue

            candidates: list[tuple[str, str | None]] = []
            body_docs = find_primary_documents(items, form=filing.form)
            primary_set = set(body_docs)
            for body in body_docs:
                candidates.append((body, None))
            if not body_docs and not include_exhibits:
                print("  - no primary filing document found", file=sys.stderr)
            if include_exhibits:
                allowed = allowed_exhibits if not broad_mode else None
                for item in items:
                    name = item.get("name", "")
                    if name in primary_set:
                        continue
                    lower = name.lower()
                    if not lower.endswith((".htm", ".html", ".txt")):
                        continue
                    if _looks_like_boilerplate_filename(lower):
                        # Subsidiary lists, consents, SOX certifications,
                        # and mine-safety disclosures regardless of how
                        # the filer numbered them.
                        continue
                    # Try filename-based exhibit match first (most reliable
                    # when filers follow the 'ex-' / 'dex' convention),
                    # then fall back to the SEC `type` field for filers
                    # who name exhibits descriptively.
                    exhibit_num = (
                        match_exhibit(name, allowed)
                        or match_exhibit_type(item.get("type", ""), allowed)
                    )
                    if exhibit_num:
                        candidates.append((name, exhibit_num))

            saved_for_filing = 0

            for name, exhibit_num in candidates:
                doc_url = urljoin(filing.index_url, name)
                key = f"{filing.accession}:{name}"
                if key in seen:
                    continue
                try:
                    content = client.download(doc_url)
                except requests.HTTPError as exc:
                    print(f"  ! download failed for {name}: {exc}", file=sys.stderr)
                    continue
                matched, via_raw = _phrase_match(content, phrase)
                if not matched:
                    continue
                doc_text = extract_text(content)
                if via_raw:
                    print(
                        f"  ! {phrase!r} matched on raw bytes only in {name} "
                        "(text extraction missed it — likely inline XBRL or "
                        "HTML attributes; saving anyway)"
                    )
                # Body docs (exhibit_num is None) are always taken as long as
                # they contain the phrase. Exhibits in the explicit-contract
                # categories (Ex 2/4/9/10) are likewise trusted by category.
                # Everything else gets the agreement-keyword check, and Ex 99
                # always does even when listed explicitly. The keyword check
                # uses extracted text only — a raw-bytes-only match wouldn't
                # have a meaningful 'first 3KB of body' to inspect, so we
                # skip the filter in that case and trust the category.
                apply_agreement_filter = (
                    exhibit_num is not None
                    and exhibit_num not in KNOWN_CONTRACT_EXHIBITS
                    and (broad_mode or exhibit_num == "99")
                    and not via_raw
                )
                if apply_agreement_filter and not looks_like_agreement(doc_text):
                    print(
                        f"  - skipped {name} "
                        f"(Ex {exhibit_num} without agreement-like header)"
                    )
                    continue

                target = output_dir / safe_filename(
                    filing.filed, filing.company or filing.cik, filing.accession, name
                )
                target.write_bytes(content)
                downloaded += 1
                record = {
                    "source": "sec",
                    "accession": filing.accession,
                    "cik": filing.cik,
                    "form": filing.form,
                    "filed": filing.filed,
                    "company": filing.company,
                    "document": name,
                    "exhibit": exhibit_num if exhibit_num else PRIMARY_DOC_LABEL,
                    "url": doc_url,
                    "saved_as": str(target.relative_to(output_dir)),
                }
                if via_raw:
                    record["match_via_raw_bytes"] = True
                if gc_ai and gc_ai_folder_id:
                    try:
                        uploaded_file = gc_ai.upload_file(target, folder_id=gc_ai_folder_id)
                        record["gc_ai_file_id"] = uploaded_file.get("id")
                        record["gc_ai_folder_id"] = gc_ai_folder_id
                        record["gc_ai_status"] = uploaded_file.get("status")
                        uploaded += 1
                        print(
                            f"  -> uploaded to GC AI ({uploaded_file.get('id')}, "
                            f"status={uploaded_file.get('status')})"
                        )
                    except requests.HTTPError as exc:
                        record["gc_ai_error"] = f"{exc.response.status_code} {exc.response.text[:200]}"
                        print(f"  ! GC AI upload failed: {exc}", file=sys.stderr)
                    except requests.RequestException as exc:
                        record["gc_ai_error"] = str(exc)
                        print(f"  ! GC AI upload failed: {exc}", file=sys.stderr)
                _append_manifest_line(manifest, json.dumps(record) + "\n")
                seen.add(key)
                saved_for_filing += 1
                print(f"  -> saved {target.name}")

            if saved_for_filing == 0 and candidates:
                # The phrase didn't match in any body or exhibit candidate.
                # That usually means EDGAR found the phrase in a document
                # we filtered out — a boilerplate exhibit (Ex 21 / Ex 23 /
                # SOX certs), an XBRL render, or the filing-headers blob.
                # Run a one-pass locator across the rest of the filing's
                # documents and tell the user where the phrase actually is.
                print(
                    f"  - no candidate document contained {phrase!r} "
                    f"({len(candidates)} doc(s) checked: "
                    f"{', '.join(name for name, _ in candidates[:5])}"
                    f"{', ...' if len(candidates) > 5 else ''})"
                )
                checked_set = {name for name, _ in candidates}
                located_in: str | None = None
                located_content: bytes | None = None
                for item in items:
                    name = item.get("name", "")
                    if name in checked_set:
                        continue
                    if not name.lower().endswith(
                        (".htm", ".html", ".txt", ".xml")
                    ):
                        continue
                    locator_url = urljoin(filing.index_url, name)
                    try:
                        body = client.download(locator_url)
                    except requests.HTTPError:
                        continue
                    matched, _ = _phrase_match(body, phrase)
                    if matched:
                        located_in = name
                        located_content = body
                        break
                if located_in:
                    description = ""
                    if located_in.lower().endswith(".txt") and located_content:
                        description = _describe_txt_match(
                            located_content, phrase
                        ) or ""
                    if description.startswith("SEC-HEADER"):
                        # A bibliographic metadata match is not contract
                        # content, so report the distinction plainly.
                        print(
                            f"  - {phrase!r} appears only in the filing's "
                            f"SEC-HEADER metadata. {description}. No actual "
                            "document content matches; nothing to save."
                        )
                    elif description:
                        print(
                            f"  - {phrase!r} located in {located_in}: "
                            f"{description}. Filtered as boilerplate / XBRL "
                            "/ metadata; not saved."
                        )
                    else:
                        print(
                            f"  - {phrase!r} located in {located_in} "
                            "(filtered as boilerplate / XBRL / metadata; "
                            "not saved). Adjust filters if you want to "
                            "capture it."
                        )

    print(f"\nDone. Scanned {count} filing(s); downloaded {downloaded} contract(s).")
    if gc_ai and gc_ai_folder_id:
        print(f"Uploaded {uploaded} contract(s) to GC AI folder {gc_ai_folder_id}.")
    print(f"Manifest: {manifest_path}")


def fetch_and_save_disclosure_sections(
    client: "EdgarClient",
    cik: str,
    output_dir: Path,
    *,
    forms: Iterable[str] | None = None,
    section_keys: Iterable[str] | None = None,
    gc_ai: "GCAIClient | None" = None,
    gc_ai_folder_id: str | None = None,
) -> list[dict]:
    """Pull a company's latest annual report and extract the Skill's
    REQUIRED BASELINE sections (Risk Factors, MD&A, Financial Statements).

    For each section successfully extracted:
      * Save as plain text to ``output_dir`` with a Skill-friendly name
        ``<safe-company>_<form>_<filed>_<SectionKey>.txt``.
      * Append a manifest row with ``source=sec-disclosure`` and
        ``section=<key>`` so the Skill / GUI can identify the document
        without re-parsing the filename.
      * Optionally upload to a GC AI folder.

    Returns the list of saved manifest records (one per section).
    """
    # Default to the canonical annual reports for US issuers + FPIs.
    forms_list = list(forms) if forms else ["10-K", "20-F"]
    filings = client.get_recent_filings(cik, forms=forms_list, limit=1)
    if not filings:
        print(
            f"No recent filings found for CIK {cik} in forms {forms_list}. "
            "Try a different form (e.g. '10-Q') or verify the CIK."
        )
        return []
    filing = filings[0]
    print(
        f"Latest {filing.form} for {filing.company or cik}: "
        f"accession={filing.accession}, filed={filing.filed}"
    )

    # Locate the primary HTML body of the filing — same logic as
    # scan_and_download uses, so we slice the same document a human
    # would read on EDGAR.
    items = client.list_filing_documents(filing)
    primary = find_primary_document(items, form=filing.form)
    if not primary:
        print(f"  Could not locate primary HTML body for {filing.accession}")
        return []
    primary_url = urljoin(filing.index_url, primary)
    print(f"  Fetching primary body: {primary}")
    html = client.download(primary_url).decode("utf-8", errors="replace")

    # Extract the sections.
    from disclosure_sections import (
        extract_sections, SECTIONS_10K, SECTIONS_20F,
    )
    sections = extract_sections(html, filing.form, sections=section_keys)
    if not sections:
        print(
            "  No target sections matched the canonical Item headers in this "
            "filing. The issuer may use non-standard headings; you'll need "
            "to upload the relevant section manually."
        )
        return []

    # Save + (optionally) upload each.
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.jsonl"
    saved_records: list[dict] = []

    safe_company = (
        re.sub(r"[^A-Za-z0-9._-]+", "-", filing.company or cik).strip("-")
        or cik
    )
    safe_form = re.sub(r"[^A-Za-z0-9_-]+", "-", filing.form)

    with manifest_path.open("a", encoding="utf-8") as manifest:
        for key, extracted in sections.items():
            target_name = (
                f"{safe_company}_{safe_form}_{filing.filed}_{key}.txt"
            )
            target = output_dir / target_name
            target.write_text(extracted.text, encoding="utf-8")
            print(
                f"  -> saved {key} ({extracted.char_count:,} chars) to "
                f"{target_name}"
            )
            record: dict = {
                "source": "sec-disclosure",
                "section": key,
                "section_label": extracted.spec.label,
                "accession": filing.accession,
                "cik": filing.cik,
                "form": filing.form,
                "filed": filing.filed,
                "company": filing.company,
                # Suffix the source document with the section key so
                # disclosure rows have a distinct (accession, document)
                # dedup key from a contract row that might have saved
                # the same primary HTML body. Three slices of the same
                # 10-K body get three distinct keys.
                "document": f"{primary}#{key}",
                "exhibit": "disclosure",
                "url": primary_url,
                "saved_as": target_name,
                "char_count": extracted.char_count,
            }
            if gc_ai is not None and gc_ai_folder_id:
                try:
                    upload_resp = gc_ai.upload_file(
                        target, folder_id=gc_ai_folder_id
                    )
                    record["gc_ai_file_id"] = upload_resp.get("id")
                    record["gc_ai_folder_id"] = gc_ai_folder_id
                    record["gc_ai_status"] = upload_resp.get("status")
                    print(
                        f"     uploaded to GC AI ({upload_resp.get('id')})"
                    )
                except requests.HTTPError as exc:
                    record["gc_ai_error"] = (
                        f"{exc.response.status_code} "
                        f"{exc.response.text[:200]}"
                    )
                    print(f"     ! GC AI upload failed: {exc}", file=sys.stderr)
                except requests.RequestException as exc:
                    record["gc_ai_error"] = str(exc)
                    print(f"     ! GC AI upload failed: {exc}", file=sys.stderr)
            _append_manifest_line(manifest, json.dumps(record) + "\n")
            saved_records.append(record)

    missing = [
        s.key for s in (
            SECTIONS_20F if filing.form.upper().startswith("20-F")
            else SECTIONS_10K
        ) if s.key not in sections
    ]
    if missing:
        print(
            f"  (note: {', '.join(missing)} not located by canonical headers; "
            "upload manually if needed)"
        )
    return saved_records


# Default form types pulled by fetch_issuer_material_exhibits.
# Covers both US issuers (10-K/Q/8-K, S-1/4) and Foreign Private
# Issuers (20-F, 6-K, F-1/3/4). Order doesn't matter; EDGAR returns
# all matching forms.
ISSUER_DEFAULT_FORMS = (
    "10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A",
    "20-F", "20-F/A", "6-K",
    "S-1", "S-1/A", "S-4", "S-4/A",
    "F-1", "F-1/A", "F-3", "F-3/A", "F-4", "F-4/A",
)


def fetch_issuer_material_exhibits(
    client: "EdgarClient",
    cik: str,
    output_dir: Path,
    *,
    forms: Iterable[str] | None = None,
    max_filings: int = 10,
    gc_ai: "GCAIClient | None" = None,
    gc_ai_folder_id: str | None = None,
    include_primary_body: bool = False,
) -> int:
    """Pull every material exhibit (Ex 2 / 4 / 9 / 10) attached to an
    issuer's recent filings — no phrase search.

    For Disclosure Integrity Engine workspace builds, this is the
    correct contract-collection path: enumerate the issuer's own
    filings by CIK and grab the material-contract exhibits directly.
    Phrase search via ``scan_and_download`` is kept for ad-hoc
    cross-issuer queries ("which issuers mention DEMO_ENTITY_001?")
    where the phrase is the actual filter.

    The function reuses the same exhibit classifier, manifest schema,
    and dedup mechanism as ``scan_and_download`` so its output mixes
    cleanly with phrase-scan output in a shared output directory.

    Returns the number of exhibits saved (excluding deduped ones).
    """
    forms_list = list(forms) if forms else list(ISSUER_DEFAULT_FORMS)
    filings = client.get_recent_filings(
        cik, forms=forms_list, limit=max_filings,
    )
    if not filings:
        print(
            f"No filings found for CIK {cik} in forms "
            f"{', '.join(forms_list)}"
        )
        return 0

    print(
        f"Found {len(filings)} recent filing(s) for "
        f"{filings[0].company or cik}; pulling material exhibits…"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.jsonl"
    seen = _load_seen(manifest_path)

    saved = 0
    with manifest_path.open("a", encoding="utf-8") as manifest:
        for filing in filings:
            print(f"  {filing.form} {filing.filed} (acc {filing.accession})")
            try:
                items = client.list_filing_documents(filing)
            except requests.RequestException as exc:
                print(f"    ! could not list documents: {exc}")
                continue

            # Decide what to pull: every Ex 2 / 4 / 9 / 10 attachment,
            # plus optionally the primary filing body. We don't apply
            # the agreement-keyword filter here because we're already
            # restricted to material-contract exhibit categories — for
            # Ex 2/4/9/10 the category alone is sufficient signal.
            # Diagnostic accounting: how many of the filing's items were
            # HTML candidates, classified as material, deduped against
            # the existing manifest, and freshly saved. Helps users
            # debug "why didn't the 20-F yield Ex 4?" quickly.
            html_candidates = 0
            classified_material = 0
            filing_dedup = 0
            filing_saved = 0

            for item in items:
                name = item.get("name", "")
                item_type = item.get("type", "")
                if not name.lower().endswith((".htm", ".html")):
                    continue
                html_candidates += 1
                # Try the SEC <type> field first (e.g. "EX-4.1") since
                # it survives non-standard filenames like
                # "demo-material-document.htm".
                exhibit_num = match_exhibit_type(
                    item_type, allowed=KNOWN_CONTRACT_EXHIBITS,
                )
                if exhibit_num is None:
                    # match_exhibit takes (filename, allowed) — operates on
                    # the filename pattern (e.g. ex-10.1.htm). Distinct from
                    # match_exhibit_type above, which takes the SEC <type>
                    # value from the filing index.
                    exhibit_num = match_exhibit(
                        name, allowed=KNOWN_CONTRACT_EXHIBITS,
                    )
                is_primary = (
                    include_primary_body
                    and find_primary_document(items, form=filing.form) == name
                )
                if exhibit_num is None and not is_primary:
                    continue
                classified_material += 1
                key = f"{filing.accession}:{name}"
                if key in seen:
                    filing_dedup += 1
                    continue
                seen.add(key)

                doc_url = urljoin(filing.index_url, name)
                try:
                    content = client.download(doc_url)
                except requests.RequestException as exc:
                    print(f"    ! download failed for {name}: {exc}")
                    continue

                target = output_dir / safe_filename(
                    filing.filed,
                    filing.company or filing.cik,
                    filing.accession,
                    name,
                )
                target.write_bytes(content)
                exhibit_label = exhibit_num if exhibit_num else PRIMARY_DOC_LABEL
                record: dict = {
                    "source": "sec",
                    "accession": filing.accession,
                    "cik": filing.cik,
                    "form": filing.form,
                    "filed": filing.filed,
                    "company": filing.company,
                    "document": name,
                    "exhibit": exhibit_label,
                    "url": doc_url,
                    "saved_as": str(target.relative_to(output_dir)),
                }
                if gc_ai is not None and gc_ai_folder_id:
                    try:
                        upload_resp = gc_ai.upload_file(
                            target, folder_id=gc_ai_folder_id,
                        )
                        record["gc_ai_file_id"] = upload_resp.get("id")
                        record["gc_ai_folder_id"] = gc_ai_folder_id
                        record["gc_ai_status"] = upload_resp.get("status")
                        print(
                            f"    -> {target.name} (Ex {exhibit_label}); "
                            f"uploaded to GC AI"
                        )
                    except requests.RequestException as exc:
                        record["gc_ai_error"] = str(exc)
                        print(
                            f"    -> {target.name} (Ex {exhibit_label}); "
                            f"GC AI upload failed: {exc}"
                        )
                else:
                    print(
                        f"    -> {target.name} (Ex {exhibit_label})"
                    )
                _append_manifest_line(manifest, json.dumps(record) + "\n")
                saved += 1
                filing_saved += 1

            # Per-filing diagnostic. We always emit a one-line summary
            # when the filing had HTML candidates so the user can see
            # the (classified, saved, deduped) breakdown without
            # guessing why a particular filing yielded zero new docs.
            if html_candidates > 0:
                if classified_material == 0:
                    types_seen = sorted(
                        {
                            (item.get("type") or "").strip() or "(no-type)"
                            for item in items
                            if item.get("name", "").lower().endswith(
                                (".htm", ".html")
                            )
                        }
                    )
                    print(
                        f"    (no Ex 2/4/9/10 among {html_candidates} "
                        f"HTML doc(s); types: {', '.join(types_seen[:8])}"
                        f"{'...' if len(types_seen) > 8 else ''})"
                    )
                elif filing_saved == 0 and filing_dedup > 0:
                    print(
                        f"    ({filing_dedup} material exhibit(s) "
                        f"already saved in earlier run — skipped)"
                    )
                elif filing_saved == 0:
                    print(
                        f"    ({classified_material} classified as material "
                        f"but none saved — check log for download errors)"
                    )

    print(f"Done. Pulled {saved} material exhibit(s) for CIK {cik}.")
    return saved


def _append_manifest_line(manifest, line: str) -> None:
    """Append a single JSONL record to the manifest, taking an exclusive
    flock so that concurrent scanner processes against the same output
    directory don't interleave bytes mid-line."""
    if fcntl is not None:
        fcntl.flock(manifest.fileno(), fcntl.LOCK_EX)
        try:
            manifest.write(line)
            manifest.flush()
        finally:
            fcntl.flock(manifest.fileno(), fcntl.LOCK_UN)
    else:
        manifest.write(line)
        manifest.flush()


def _sanitize_record(record: dict) -> dict:
    """Drop None and NaN values from a record before JSON-serialization.

    Pandas turns missing cells into NaN when the column is float-typed (e.g.
    a gc_ai_error column where most rows are missing). NaN survives
    json.dumps with the default allow_nan=True as the literal token 'NaN',
    which isn't standard JSON. Filter those out so the main manifest stays
    portable.
    """
    out: dict = {}
    for k, v in record.items():
        if v is None:
            continue
        if isinstance(v, float) and math.isnan(v):
            continue
        out[k] = v
    return out


def _record_key(record: dict) -> str | None:
    """Build the (accession, document) dedup key, or None if either piece
    isn't a non-empty string. Pandas-derived records can have NaN in these
    positions when columns are float-typed; treat that as 'unusable'."""
    accession = record.get("accession")
    document = record.get("document")
    if not isinstance(accession, str) or not accession:
        return None
    if not isinstance(document, str) or not document:
        return None
    return f"{accession}:{document}"


def commit_selection(
    staging_dir: Path,
    output_dir: Path,
    selected_records: list[dict],
    gc_ai: GCAIClient | None = None,
    gc_ai_folder_id: str | None = None,
) -> dict:
    """Apply the user's pick from the staging manifest.

    For each selected record:
      * Move the file from staging_dir -> output_dir.
      * If gc_ai + folder_id provided, upload it.
      * Append the record (with any GC AI fields) to the main manifest.
      * Remove the row from the staging manifest.

    Returns a dict with counts: {moved, uploaded, failed: list[str]}.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    main_manifest_path = output_dir / "manifest.jsonl"
    staging_manifest_path = staging_dir / "manifest.jsonl"

    moved = 0
    uploaded = 0
    failed: list[str] = []

    committed_keys: set[str] = set()
    main_seen = _load_seen(main_manifest_path)

    with main_manifest_path.open("a", encoding="utf-8") as main_manifest:
        for record in selected_records:
            saved_as = record.get("saved_as")
            if not saved_as:
                failed.append("(missing saved_as)")
                continue
            staged_path = staging_dir / saved_as
            if not staged_path.exists():
                failed.append(f"{saved_as} (not in staging dir)")
                continue
            target = output_dir / staged_path.name
            try:
                staged_path.replace(target)
            except OSError as exc:
                failed.append(f"{saved_as} (move failed: {exc})")
                continue
            moved += 1

            new_record = {k: v for k, v in record.items() if k != "Select"}
            new_record["saved_as"] = target.name
            new_record = _sanitize_record(new_record)
            key = _record_key(record)
            if key is None:
                failed.append(f"{saved_as} (record missing accession/document)")
                continue
            committed_keys.add(key)

            if gc_ai is not None and gc_ai_folder_id:
                try:
                    upload_resp = gc_ai.upload_file(target, folder_id=gc_ai_folder_id)
                    new_record["gc_ai_file_id"] = upload_resp.get("id")
                    new_record["gc_ai_folder_id"] = gc_ai_folder_id
                    new_record["gc_ai_status"] = upload_resp.get("status")
                    uploaded += 1
                except requests.RequestException as exc:
                    new_record["gc_ai_error"] = str(exc)
                    failed.append(f"{saved_as} (upload failed: {exc})")

            if key not in main_seen:
                _append_manifest_line(
                    main_manifest, json.dumps(new_record) + "\n"
                )
                main_seen.add(key)

    # Rewrite the staging manifest without the committed rows.
    if staging_manifest_path.exists() and committed_keys:
        kept_lines: list[str] = []
        with staging_manifest_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    kept_lines.append(line)
                    continue
                key = _record_key(rec)
                if key is not None and key in committed_keys:
                    continue
                kept_lines.append(line if line.endswith("\n") else line + "\n")
        staging_manifest_path.write_text("".join(kept_lines), encoding="utf-8")

    return {"moved": moved, "uploaded": uploaded, "failed": failed}


def _load_seen(manifest_path: Path) -> set[str]:
    if not manifest_path.exists():
        return set()
    seen: set[str] = set()
    with manifest_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen.add(f"{record.get('accession')}:{record.get('document')}")
    return seen


# Default form list used when the user doesn't override. Kept narrow on
# purpose: EDGAR's full-text endpoint silently returns zero hits if any
# form value in the comma-separated list is one it doesn't recognize, and
# the /A amendments are matched by the base form anyway. Users can add
# more (DEF 14A, S-3, F-1, etc.) via the GUI multiselect.
PROFILE_DEFAULT_FORMS = ["10-K", "10-Q", "8-K", "S-1", "S-4", "20-F"]


def _load_profile(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Profile {path} is not a JSON object.")
    return raw


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--profile", type=Path)
    pre_args, _ = pre.parse_known_args(argv)
    profile: dict = _load_profile(pre_args.profile) if pre_args.profile else {}

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", type=Path,
        help="Load default values from a JSON profile (CLI flags still override).",
    )
    parser.add_argument(
        "-q", "--query",
        default=profile.get("query", "DEMO_ENTITY_001"),
        help="Phrase to search for.",
    )
    parser.add_argument(
        "-o", "--output", type=Path,
        default=Path(profile.get("output", "./contracts")),
        help="Directory to save downloaded contracts.",
    )
    parser.add_argument(
        "--user-agent",
        default=profile.get("user_agent"),
        help=(
            "SEC-required User-Agent; the placeholder "
            "'DEMO_OPERATOR contact@example.invalid' must be replaced."
        ),
    )
    parser.add_argument(
        "--forms", nargs="+",
        default=profile.get("forms", PROFILE_DEFAULT_FORMS),
        help="Form types to search.",
    )
    parser.add_argument(
        "--start", default=profile.get("start"),
        help="Start date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end", default=profile.get("end"),
        help="End date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--max-filings", type=int,
        default=profile.get("max_filings"),
        help="Stop after scanning this many filings.",
    )
    parser.add_argument(
        "--exhibits", nargs="*",
        default=profile.get("exhibits", DEFAULT_EXHIBITS),
        help=(
            "Exhibit numbers to capture (e.g. '2 4 10'). Leave empty (the "
            "default) to capture every non-boilerplate exhibit whose first "
            "~3KB looks like an agreement — robust to filer naming "
            "differences. Pick numbers to narrow."
        ),
    )
    parser.add_argument(
        "--include-ex99", action="store_true",
        default=bool(profile.get("include_ex99", False)),
        help=(
            "(Legacy.) Add Ex 99 to the explicit exhibits list. "
            "When --exhibits is empty, Ex 99 is captured automatically "
            "subject to the agreement-keyword filter."
        ),
    )
    parser.add_argument(
        "--no-exhibits", action="store_true",
        default=bool(profile.get("no_exhibits", False)),
        help=(
            "Skip exhibits entirely; download only the primary filing body "
            "(e.g. the 10-K text itself, not its attached agreements)."
        ),
    )

    gc = parser.add_argument_group("GC AI integration")
    gc.add_argument(
        "--gc-ai-key",
        default=os.environ.get("GC_AI_API_KEY"),
        help=(
            "GC AI API key (user-scoped, 'u:gcai_...'). "
            "Defaults to env var GC_AI_API_KEY. Required for GC AI upload."
        ),
    )
    gc.add_argument(
        "--gc-ai-base-url",
        default=profile.get("gc_ai_base_url", GC_AI_DEFAULT_BASE_URL),
        help=f"GC AI API base URL (default: {GC_AI_DEFAULT_BASE_URL}).",
    )
    gc.add_argument(
        "--gc-ai-folder-id",
        default=profile.get("gc_ai_folder_id"),
        help="Upload into this existing GC AI folder ID (UUID).",
    )
    gc.add_argument(
        "--gc-ai-folder-name",
        default=profile.get("gc_ai_folder_name"),
        help=(
            "Create a new GC AI folder with this name and upload into it. "
            "Mutually exclusive with --gc-ai-folder-id."
        ),
    )
    gc.add_argument(
        "--gc-ai-folder-description",
        default=profile.get("gc_ai_folder_description"),
        help="Optional description used when creating the folder.",
    )
    gc.add_argument(
        "--gc-ai-parent-folder-id",
        default=profile.get("gc_ai_parent_folder_id"),
        help="Optional parent folder ID when creating a new folder.",
    )

    args = parser.parse_args(argv)
    if not args.user_agent:
        parser.error("--user-agent is required (or set 'user_agent' in --profile).")
    # Treat --max-filings 0 the same as the GUI: "no limit". A literal cap of
    # zero would cause an immediate break and is never what users mean.
    if args.max_filings is not None and args.max_filings <= 0:
        args.max_filings = None
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.gc_ai_folder_id and args.gc_ai_folder_name:
        print(
            "error: --gc-ai-folder-id and --gc-ai-folder-name are mutually exclusive.",
            file=sys.stderr,
        )
        return 2

    gc_ai_client: GCAIClient | None = None
    gc_ai_folder_id: str | None = None
    if args.gc_ai_folder_id or args.gc_ai_folder_name:
        if not args.gc_ai_key:
            print(
                "error: GC AI upload requested but no API key provided "
                "(use --gc-ai-key or set GC_AI_API_KEY).",
                file=sys.stderr,
            )
            return 2
        gc_ai_client = GCAIClient(args.gc_ai_key, base_url=args.gc_ai_base_url)
        if args.gc_ai_folder_id:
            gc_ai_folder_id = args.gc_ai_folder_id
        else:
            description = args.gc_ai_folder_description or (
                f"Auto-created by sec_contract_scanner for query {args.query!r}."
            )
            folder = gc_ai_client.create_folder(
                name=args.gc_ai_folder_name,
                description=description,
                parent_folder_id=args.gc_ai_parent_folder_id,
            )
            gc_ai_folder_id = folder["id"]
            print(
                f"Created GC AI folder {folder['path']} "
                f"(id={gc_ai_folder_id})"
            )

    exhibits = list(args.exhibits)
    # --include-ex99 is a legacy convenience for the narrow-list path.
    # In broad mode (empty exhibits), Ex 99 is captured automatically by the
    # agreement-keyword filter, so the flag is a no-op there — *don't* let it
    # silently switch the run from broad to ['99']-only.
    if args.include_ex99 and exhibits and "99" not in exhibits:
        exhibits.append("99")

    client = EdgarClient(args.user_agent)
    scan_and_download(
        client=client,
        phrase=args.query,
        output_dir=args.output,
        forms=args.forms,
        start=args.start,
        end=args.end,
        max_filings=args.max_filings,
        gc_ai=gc_ai_client,
        gc_ai_folder_id=gc_ai_folder_id,
        exhibits=exhibits,
        include_exhibits=not args.no_exhibits,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
