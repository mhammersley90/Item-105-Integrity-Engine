"""Client for the CourtListener REST API v4.

CourtListener (Free Law Project) hosts the RECAP archive — ~half a
billion PACER items previously paid for by other users, free to
re-download. Use this as the cheap path for federal court documents
before falling back to paid PACER fetches.

Wraps the endpoints we actually use for a contract-review workflow:
    GET /api/rest/v4/search/             - federated search (type=r = RECAP)
    GET /api/rest/v4/dockets/            - docket list / detail
    GET /api/rest/v4/docket-entries/     - entries on a docket
    GET /api/rest/v4/recap-documents/    - the actual filings (incl. PDF URL)
    GET /api/rest/v4/parties/            - parties on dockets
    GET /api/rest/v4/recap-fetch/        - request a fresh PACER fetch (paid)

Auth: token via `Authorization: Token <key>` header. Get yours at
https://www.courtlistener.com/profile/api-token/. Free tier limits:
5 req/min, 50/hr, 125/day. We map 429 to a clear error; for higher
volume see https://free.law/membership/.

Document fetch: CourtListener serves RECAP-archived PDFs from its own
CDN. Each recap-document carries a `filepath_local` (relative URL); if
empty, the document is metadata-only — that filing's PDF hasn't been
contributed to RECAP yet. Use the Pray-and-Pay flow or request a
recap-fetch (billed via your PACER account) in that case.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

CL_BASE_URL = "https://www.courtlistener.com/api/rest/v4"
CL_HOST = "https://www.courtlistener.com"

# CourtListener court IDs match PACER subdomains except for these documented
# exceptions (PACER Data APIs page, Nov 2024). Without translating, queries
# against these courts silently return zero hits.
PACER_TO_CL_COURT_ID = {
    "azb": "arb",          # Arizona Bankruptcy
    "cofc": "uscfc",       # Court of Federal Claims
    "neb": "nebraskab",    # Nebraska Bankruptcy
    "nysb-mega": "nysb",   # CourtListener drops the 'mega' flag
}
CL_TO_PACER_COURT_ID = {v: k for k, v in PACER_TO_CL_COURT_ID.items()}

# Endpoints CourtListener restricts to "select users" — a normal token gets
# 403 (or sometimes 401) on these. The open paths are /search/, /dockets/,
# /docket-entries/, /courts/, and most metadata endpoints.
PERMISSION_RESTRICTED_PATHS = frozenset({
    "recap-documents/",
    "parties/",
    "attorneys/",
    "recap-query/",
})


def pacer_to_cl_court(court_id: str) -> str:
    """Translate a PACER court code to its CourtListener equivalent."""
    return PACER_TO_CL_COURT_ID.get(court_id, court_id)


def cl_to_pacer_court(court_id: str) -> str:
    """Translate a CourtListener court code to its PACER equivalent."""
    return CL_TO_PACER_COURT_ID.get(court_id, court_id)


@dataclass
class CourtListenerPage:
    """One page of results from a list / search endpoint."""

    count: int = 0
    next_url: str | None = None
    previous_url: str | None = None
    results: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class CourtListenerError(Exception):
    """A CourtListener API call failed.

    `status_code` carries the HTTP status — 401 means token issue,
    404 means resource doesn't exist, 429 means rate-limited.
    """

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class CourtListenerClient:
    """REST client over CourtListener v4. Token-authenticated; JSON only."""

    def __init__(
        self,
        api_token: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        base_url: str = CL_BASE_URL,
    ) -> None:
        if not api_token:
            raise ValueError("CourtListener API token is required")
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {api_token}",
                "Accept": "application/json",
            }
        )
        self.timeout = timeout

    # ---- low-level ------------------------------------------------------

    def _is_restricted_path(self, url: str) -> bool:
        return any(p in url for p in PERMISSION_RESTRICTED_PATHS)

    def _get(self, path_or_url: str, params: dict | None = None) -> dict:
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{self.base_url}/{path_or_url.lstrip('/')}"
        )
        response = self.session.get(url, params=params, timeout=self.timeout)
        if response.status_code in (401, 403):
            if self._is_restricted_path(url):
                raise CourtListenerError(
                    f"This CourtListener endpoint is restricted to select "
                    f"users (got {response.status_code}). The /search/?type=r "
                    "endpoint is open and its results include filepath_local "
                    "for PDF download. To request elevated access, contact "
                    "https://www.courtlistener.com/contact/?issue_type=partnerships.",
                    status_code=response.status_code,
                )
            if response.status_code == 401:
                raise CourtListenerError(
                    "CourtListener authentication failed (401). Verify your "
                    "token at https://www.courtlistener.com/profile/api-token/.",
                    status_code=401,
                )
            raise CourtListenerError(
                f"CourtListener access forbidden (403): {url}",
                status_code=403,
            )
        if response.status_code == 429:
            raise CourtListenerError(
                "CourtListener rate limit (429). Free tier allows 5 req/min, "
                "50/hr, 125/day. Wait, then retry — or join Free Law Project "
                "for higher limits.",
                status_code=429,
            )
        if response.status_code == 404:
            raise CourtListenerError(
                f"CourtListener resource not found (404): {url}",
                status_code=404,
            )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _list(
        self,
        path: str,
        *,
        fields: list[str] | None = None,
        omit: list[str] | None = None,
        filter_nested: bool = False,
        **params,
    ) -> CourtListenerPage:
        cleaned = {k: v for k, v in params.items() if v not in (None, "", [])}
        if fields:
            cleaned["fields"] = ",".join(fields)
        if omit:
            cleaned["omit"] = ",".join(omit)
        if filter_nested:
            cleaned["filter_nested_results"] = "true"
        data = self._get(path, params=cleaned or None)
        return CourtListenerPage(
            count=int(data.get("count") or 0),
            next_url=data.get("next"),
            previous_url=data.get("previous"),
            results=data.get("results") or [],
            raw=data,
        )

    # ---- high-level search ----------------------------------------------

    SEARCH_TYPE_OPINIONS = "o"
    SEARCH_TYPE_DOCKETS = "r"
    SEARCH_TYPE_DOCUMENTS = "rd"
    SEARCH_TYPE_DOCKETS_NO_DOCS = "d"
    SEARCH_TYPE_JUDGES = "p"
    SEARCH_TYPE_ORAL_ARGUMENTS = "oa"

    def search_recap(
        self,
        query: str,
        *,
        court_ids: list[str] | None = None,
        filed_after: str | None = None,
        filed_before: str | None = None,
        party_name: str | None = None,
        case_name: str | None = None,
        order_by: str | None = None,
        highlight: bool = True,
        search_type: str = SEARCH_TYPE_DOCKETS,
    ) -> CourtListenerPage:
        """Federated RECAP search wrapping GET /search/.

        Uses cursor-based pagination: do NOT pass page numbers — call
        ``next_page(previous_result)`` to fetch subsequent pages.

        - ``search_type`` defaults to 'r' (dockets with up to 3 nested
          matching documents; check the `more_docs` flag for >3). Use
          'rd' to query individual filings instead.
        - ``order_by`` accepts the same values as the website's drop-
          down: 'score desc' (relevance, default), 'dateFiled desc',
          'dateFiled asc', 'entry_date_filed desc', etc.
        - ``highlight=True`` (default) returns snippet fields with
          ``<mark>...</mark>`` around match terms — much better UX than
          a plain truncated snippet. Disable to shave latency.
        - ``court_ids`` are inlined into the q string as Lucene-style
          ``court_id:X OR court_id:Y`` so multi-court queries are
          portable.
        """
        params: dict = {"type": search_type, "q": query}
        if court_ids:
            joined = " OR ".join(f"court_id:{c}" for c in court_ids if c)
            params["q"] = f"({query}) AND ({joined})" if query else joined
        if filed_after:
            params["filed_after"] = filed_after
        if filed_before:
            params["filed_before"] = filed_before
        if party_name:
            params["party_name"] = party_name
        if case_name:
            params["case_name"] = case_name
        if order_by:
            params["order_by"] = order_by
        if highlight:
            params["highlight"] = "on"
        return self._list("search/", **params)

    def next_page(self, previous: CourtListenerPage) -> CourtListenerPage:
        """Fetch the next cursor-paginated page of a previous result.

        Returns an empty CourtListenerPage if there isn't one. Used for
        the search endpoint, which uses cursor-based pagination — page
        numbers are silently ignored there.
        """
        if not previous.next_url:
            return CourtListenerPage()
        data = self._get(previous.next_url)
        return CourtListenerPage(
            count=int(data.get("count") or 0),
            next_url=data.get("next"),
            previous_url=data.get("previous"),
            results=data.get("results") or [],
            raw=data,
        )

    # ---- direct list endpoints ------------------------------------------

    def list_dockets(
        self,
        *,
        case_name_contains: str | None = None,
        court: str | None = None,
        docket_number: str | None = None,
        docket_number_core: str | None = None,
        pacer_case_id: str | None = None,
        date_filed_after: str | None = None,
        date_filed_before: str | None = None,
        page: int = 1,
        translate_court_id: bool = True,
    ) -> CourtListenerPage:
        """List dockets matching the supplied filters.

        `docket_number_core` is the digits-only normalized form of a docket
        number. It is useful for cross-walking from PACER PCL, which provides
        the full case number that can be normalized before comparison.
        `pacer_case_id` is the upstream PACER case ID exposed on the docket
        record. `translate_court_id` (default on) maps PACER codes
        (e.g. azb / cofc / neb) to their CourtListener equivalents
        automatically.
        """
        if court and translate_court_id:
            court = pacer_to_cl_court(court)
        params: dict = {"page": page}
        if case_name_contains:
            params["case_name__icontains"] = case_name_contains
        if court:
            params["court"] = court
        if docket_number:
            params["docket_number"] = docket_number
        if docket_number_core:
            params["docket_number_core"] = docket_number_core
        if pacer_case_id:
            params["pacer_case_id"] = pacer_case_id
        if date_filed_after:
            params["date_filed__gte"] = date_filed_after
        if date_filed_before:
            params["date_filed__lte"] = date_filed_before
        return self._list("dockets/", **params)

    def get_docket(self, docket_id: int | str) -> dict:
        return self._get(f"dockets/{docket_id}/")

    def list_docket_entries(
        self,
        docket_id: int | str,
        *,
        page: int = 1,
    ) -> CourtListenerPage:
        return self._list("docket-entries/", docket=docket_id, page=page)

    def list_recap_documents(
        self,
        *,
        docket_entry: int | str | None = None,
        docket: int | str | None = None,
        omit_plain_text: bool = True,
        page: int = 1,
    ) -> CourtListenerPage:
        """List filings (recap-documents). RESTRICTED endpoint — most
        accounts will get 403. Use `search_recap()` for an open path
        that still returns filepath_local for PDF download.

        `omit_plain_text` (default True) drops the extracted-text field
        from the response — it can be enormous (full document text). Set
        to False when you actually want the text (e.g. for in-place
        keyword scanning without downloading the PDF).
        """
        return self._list(
            "recap-documents/",
            docket_entry=docket_entry,
            docket_entry__docket=docket,
            omit=["plain_text"] if omit_plain_text else None,
            page=page,
        )

    def list_parties(
        self,
        *,
        docket: int | str | None = None,
        name_contains: str | None = None,
        page: int = 1,
    ) -> CourtListenerPage:
        """List parties. RESTRICTED endpoint — most accounts get 403.

        When filtering by docket, `filter_nested_results=true` is sent
        automatically so the nested attorneys list is also scoped to
        that docket (otherwise it leaks attorneys from other cases).
        """
        return self._list(
            "parties/",
            docket=docket,
            name__icontains=name_contains,
            filter_nested=bool(docket),
            page=page,
        )

    def list_attorneys(
        self,
        *,
        docket: int | str | None = None,
        name_contains: str | None = None,
        page: int = 1,
    ) -> CourtListenerPage:
        """List attorneys. RESTRICTED endpoint — most accounts get 403.

        Same `filter_nested_results=true` treatment as list_parties when
        scoped to a docket — keeps the nested parties_represented list
        from leaking across cases.
        """
        return self._list(
            "attorneys/",
            docket=docket,
            name__icontains=name_contains,
            filter_nested=bool(docket),
            page=page,
        )

    def list_courts(self, *, page: int = 1) -> CourtListenerPage:
        """List CourtListener court records. Open endpoint, cacheable —
        the data changes rarely. Useful for translating court codes to
        full names in the UI.
        """
        return self._list("courts/", page=page)

    # ---- document fetch -------------------------------------------------

    def download_pdf(self, filepath_local: str) -> bytes:
        """Download a RECAP-archived PDF.

        Pass the `filepath_local` value from a recap-document object;
        relative paths are resolved against courtlistener.com. Returns
        the raw PDF bytes. Raises CourtListenerError(404) when the
        document hasn't been contributed to RECAP (filepath_local is
        empty in that case — caller should check first).
        """
        if not filepath_local:
            raise CourtListenerError(
                "No filepath_local — this filing isn't in RECAP yet. "
                "Use the Pray-and-Pay flow or fetch from PACER directly.",
                status_code=404,
            )
        url = (
            filepath_local
            if filepath_local.startswith("http")
            else f"{CL_HOST}{filepath_local if filepath_local.startswith('/') else '/' + filepath_local}"
        )
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 401:
            raise CourtListenerError(
                "CourtListener auth failed during PDF download (401).",
                status_code=401,
            )
        if response.status_code == 404:
            raise CourtListenerError(
                f"PDF not found at {url}", status_code=404
            )
        response.raise_for_status()
        return response.content

    # ---- Pray-and-Pay: request docs not yet in RECAP --------------------

    def create_prayer(self, recap_document_id: int | str) -> dict:
        """POST /prayers/ — ask CourtListener to notify you when this RECAP
        document becomes available (someone else buys it from PACER, the
        Fetch API is invoked, etc.).

        `recap_document_id` is the CourtListener internal ID of the
        RECAP document — for type=rd search hits this is `hit['id']`;
        for type=r hits it's the id of one of the nested recap_documents.

        Returns the prayer object {id, date_created, status, recap_document}.
        Status 1 = waiting (always for newly created prayers); 2 = granted.

        The free tier has a daily prayer quota; 4xx responses (typically
        400 / 429) carry the quota error in the body.
        """
        if not recap_document_id:
            raise ValueError("recap_document_id is required")
        url = f"{self.base_url}/prayers/"
        response = self.session.post(
            url,
            data={"recap_document": recap_document_id},
            timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            raise CourtListenerError(
                f"CourtListener rejected the prayer request "
                f"({response.status_code}). Check your token.",
                status_code=response.status_code,
            )
        if response.status_code in (400, 429):
            # Daily quota or other server-side validation issue
            raise CourtListenerError(
                f"Prayer request rejected ({response.status_code}): "
                f"{response.text[:300]}",
                status_code=response.status_code,
            )
        response.raise_for_status()
        return response.json() if response.content else {}

    def list_prayers(self, *, page: int = 1) -> CourtListenerPage:
        """GET /prayers/ — list your pending (status=1, WAITING) prayers."""
        return self._list("prayers/", page=page)

    def delete_prayer(self, prayer_id: int | str) -> bool:
        """DELETE /prayers/{id}/ — cancel a waiting prayer.

        Returns True on success. Granted prayers can't be deleted (PACER
        already fetched the document into RECAP).
        """
        if not prayer_id:
            return False
        url = f"{self.base_url}/prayers/{prayer_id}/"
        response = self.session.delete(url, timeout=self.timeout)
        if response.status_code == 404:
            return False
        if response.status_code in (401, 403):
            raise CourtListenerError(
                f"CourtListener rejected delete prayer ({response.status_code}).",
                status_code=response.status_code,
            )
        response.raise_for_status()
        return response.status_code in (200, 204)

    # ---- Citation lookup / verification ---------------------------------

    def lookup_citations(
        self,
        *,
        text: str | None = None,
        volume: str | int | None = None,
        reporter: str | None = None,
        page: str | int | None = None,
    ) -> list[dict]:
        """POST /citation-lookup/ — extract + verify citations.

        Provide either `text` (a block of prose to scan with Eyecite for
        all citations) OR a `volume`/`reporter`/`page` triad to look up
        a single citation by canonical form.

        Returns a list of citation result objects. Each object has:
          status: 200 found, 404 not found in CL, 300 ambiguous match,
                  400 reporter unrecognized, 429 over the 250-citation cap.
          citation: the citation text as parsed.
          normalized_citations: canonical forms (multiple if ambiguous).
          start_index / end_index: position in the source text.
          clusters: list of CL opinion-cluster matches.

        Throttle: 60 valid citations / minute, 250 per request, 64 KB text.
        """
        if not text and not (volume and reporter and page):
            raise ValueError(
                "lookup_citations requires either text= or "
                "volume= reporter= page="
            )
        url = f"{self.base_url}/citation-lookup/"
        if text:
            data = {"text": text}
        else:
            data = {
                "volume": str(volume),
                "reporter": str(reporter),
                "page": str(page),
            }
        response = self.session.post(url, data=data, timeout=self.timeout)
        if response.status_code in (401, 403):
            raise CourtListenerError(
                f"CourtListener rejected the citation lookup "
                f"({response.status_code}). Check your token.",
                status_code=response.status_code,
            )
        if response.status_code == 429:
            raise CourtListenerError(
                "Citation-lookup throttle hit (429). 60 valid citations "
                "per minute is the limit. Wait a moment and retry.",
                status_code=429,
            )
        response.raise_for_status()
        result = response.json() if response.content else []
        return result if isinstance(result, list) else []
