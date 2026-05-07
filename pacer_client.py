"""Client for the PACER Authentication and Case Locator (PCL) APIs.

Auth API (PACER Authentication API User Guide v.3, May 2025):
    POST /services/cso-auth     - exchange credentials for a nextGenCSO
                                  authentication token.
    POST /services/cso-logout   - invalidate a previously issued token.

PCL API (PACER Case Locator API User Guide, Nov 2024):
    POST /pcl-public-api/rest/cases/find     - immediate case search
    POST /pcl-public-api/rest/parties/find   - immediate party search

The PCL endpoints expect the auth token in an X-NEXT-GEN-CSO request
header (different from the auth API's cookie convention). PACER may
rotate the token: a fresh X-NEXT-GEN-CSO in a search response should
replace the stored token for subsequent calls.

Document download is NOT part of this module. PCL returns a `caseLink`
URL pointing into the originating court's CM/ECF system; downloading
docket entries from CM/ECF requires per-court scraping (paid, no clean
REST API). Use the link or RECAP/CourtListener for free archived copies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

PACER_QA_AUTH_BASE = "https://qa-login.uscourts.gov/services"
PACER_PROD_AUTH_BASE = "https://pacer.login.uscourts.gov/services"
PACER_QA_PCL_BASE = "https://qa-pcl.uscourts.gov/pcl-public-api/rest"
PACER_PROD_PCL_BASE = "https://pcl.uscourts.gov/pcl-public-api/rest"


@dataclass
class PACERAuthResult:
    """Successful authentication outcome."""
    token: str  # nextGenCSO cookie value
    client_code: str | None
    warning: str | None = None  # PACER's errorDescription on partial-success
    environment: str = "qa"


class PACERAuthError(Exception):
    """Raised when PACER authentication fails.

    `login_result` carries PACER's numeric loginResult code from the
    response body so callers can branch on the cause:
      * "1"  = filer omitted redactFlag
      * "13" = invalid username, password, or OTP
      * other codes are documented case-by-case in the PACER guide.
    """

    def __init__(self, message: str, login_result: str = "") -> None:
        super().__init__(message)
        self.login_result = login_result


class PACERClient:
    """Stateless client over the PACER Authentication API.

    Construct once per environment; call .authenticate() to obtain a
    PACERAuthResult holding the nextGenCSO token. Hold the token in your
    own session state and call .logout(token) when done.
    """

    def __init__(
        self,
        *,
        environment: str = "qa",
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        if environment not in ("qa", "production"):
            raise ValueError("environment must be 'qa' or 'production'")
        self.environment = environment
        self.base_url = (
            PACER_PROD_AUTH_BASE if environment == "production"
            else PACER_QA_AUTH_BASE
        )
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )
        self.timeout = timeout

    def authenticate(
        self,
        username: str,
        password: str,
        *,
        client_code: str | None = None,
        otp_code: str | None = None,
        redact_flag: bool = False,
    ) -> PACERAuthResult:
        """Exchange credentials for a nextGenCSO token.

        Set `redact_flag=True` if the account is a filer (CM/ECF account
        with filing privileges) — PACER rejects login otherwise.

        Set `otp_code` to the 6-digit code from your authenticator app
        if the account is enrolled in MFA.

        Set `client_code` if your firm requires per-search billing
        attribution; PACER will accept the login but disable search
        privileges if a required client code is missing (returned as a
        warning, not an error).
        """
        if not username or not password:
            raise ValueError("username and password are required")

        body: dict = {"loginId": username, "password": password}
        if client_code:
            body["clientCode"] = client_code
        if otp_code:
            body["otpCode"] = otp_code
        if redact_flag:
            body["redactFlag"] = "1"

        response = self.session.post(
            f"{self.base_url}/cso-auth", json=body, timeout=self.timeout
        )
        response.raise_for_status()  # PACER returns 200 even on auth fail
        data = response.json()

        login_result = str(data.get("loginResult", ""))
        token = (data.get("nextGenCSO") or "").strip()
        error = (data.get("errorDescription") or "").strip()

        # PACER's contract: loginResult "0" means usable token. The error
        # description can still be set to convey a warning (e.g. "no search
        # privileges") even when the token itself is valid for filing.
        if login_result == "0" and token:
            return PACERAuthResult(
                token=token,
                client_code=client_code,
                warning=error or None,
                environment=self.environment,
            )

        raise PACERAuthError(
            error or f"PACER login failed (loginResult={login_result!r})",
            login_result=login_result,
        )

    def logout(self, token: str) -> bool:
        """Invalidate a previously issued token. Returns True on success."""
        if not token:
            return False
        response = self.session.post(
            f"{self.base_url}/cso-logout",
            json={"nextGenCSO": token},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("loginResult", "")) == "0"


# --------------------------------------------------------------------------
# PACER Case Locator (PCL) API
# --------------------------------------------------------------------------


@dataclass
class PCLSearchResult:
    """Parsed result of a /cases/find or /parties/find call.

    `content` carries the actual hits (case dicts for case search, party
    dicts for party search). `receipt` echoes PACER's billing record for
    this page (transactionDate, billablePages, searchFee, etc.). `pageInfo`
    drives pagination. `new_token` is set when PACER rotated the
    X-NEXT-GEN-CSO cookie; callers must store it for subsequent requests.
    """

    receipt: dict = field(default_factory=dict)
    page_info: dict = field(default_factory=dict)
    content: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    new_token: str | None = None

    @property
    def search_fee_dollars(self) -> float:
        """The dollar amount PACER billed for this page (0.0 in QA)."""
        try:
            return float(self.receipt.get("searchFee", 0) or 0)
        except (TypeError, ValueError):
            return 0.0


class PCLError(Exception):
    """A PCL search call failed.

    `status_code` carries the HTTP status returned by PCL:
      * 401 - token expired/invalid; re-authenticate
      * 406 - search criteria rejected (validation)
      * 429 - rate-limited
      * other 5xx / network errors propagate as the underlying exception
    """

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class PCLClient:
    """Stateful client over the PCL search endpoints.

    Holds the current X-NEXT-GEN-CSO token and rotates it transparently
    when PACER returns a refreshed value. Construct with the token from
    PACERClient.authenticate(); long-lived but not infinite — re-auth
    when a search returns 401.
    """

    def __init__(
        self,
        token: str,
        *,
        environment: str = "qa",
        client_code: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not token:
            raise ValueError("PCLClient requires a non-empty auth token")
        if environment not in ("qa", "production"):
            raise ValueError("environment must be 'qa' or 'production'")
        self.token = token
        self.environment = environment
        self.client_code = client_code
        self.base_url = (
            PACER_PROD_PCL_BASE if environment == "production"
            else PACER_QA_PCL_BASE
        )
        self.session = session or requests.Session()
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-NEXT-GEN-CSO": self.token,
        }
        if self.client_code:
            h["X-CLIENT-CODE"] = self.client_code
        return h

    def _post_search(
        self, path: str, criteria: dict, page: int = 0
    ) -> PCLSearchResult:
        url = f"{self.base_url}{path}?page={int(page)}"
        response = self.session.post(
            url,
            json={k: v for k, v in criteria.items() if v not in (None, "", [])},
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response.status_code == 401:
            raise PCLError(
                "PACER token expired or invalid (401). Re-authenticate.",
                status_code=401,
            )
        if response.status_code == 406:
            raise PCLError(
                "PCL rejected the search criteria (406). "
                f"Server said: {response.text[:300]}",
                status_code=406,
            )
        if response.status_code == 429:
            raise PCLError(
                "PACER is rate-limiting (429). Wait before retrying.",
                status_code=429,
            )
        response.raise_for_status()
        data = response.json() if response.content else {}

        rotated = response.headers.get("X-NEXT-GEN-CSO")
        if rotated and rotated != self.token:
            self.token = rotated
        else:
            rotated = None

        return PCLSearchResult(
            receipt=data.get("receipt") or {},
            page_info=data.get("pageInfo") or {},
            content=data.get("content") or [],
            raw=data,
            new_token=rotated,
        )

    def search_cases(self, criteria: dict, *, page: int = 0) -> PCLSearchResult:
        """POST /cases/find — page-by-page case search.

        Returns up to 54 cases per page. Set `criteria["dateFiledFrom"]` /
        `dateFiledTo` and `criteria["courtId"]` to narrow. See the PCL
        User Guide for the full searchable-fields list.
        """
        return self._post_search("/cases/find", criteria, page)

    def search_parties(self, criteria: dict, *, page: int = 0) -> PCLSearchResult:
        """POST /parties/find — page-by-page party search.

        For an entity (corporate party), set `criteria["lastName"]` to the
        entity name. Default match is 'starts with'; pass
        `exactNameMatch=True` to require an exact match. Nest case-level
        filters under `courtCase` (date range, court ID, jurisdiction).
        """
        return self._post_search("/parties/find", criteria, page)
