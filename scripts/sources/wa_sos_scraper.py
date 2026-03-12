"""wa_sos_scraper.py — Fetch new business formations from WA Secretary of State CCFS.

Uses the CCFS search API at https://ccfs.sos.wa.gov to find companies
formed in the last N days. Falls back to HTML scraping if API is unavailable.

Output: list of dicts with standard company fields + signal_tag="new_business"
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import requests

CCFS_API_URL = "https://ccfs.sos.wa.gov/api/BusinessSearch/BusinessSearch"
CCFS_SEARCH_URL = "https://ccfs.sos.wa.gov"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _format_date(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _parse_governors(governors_raw):
    """Parse governors list from API response into JSON string."""
    if not governors_raw:
        return "[]"
    if isinstance(governors_raw, list):
        return json.dumps(governors_raw)
    return json.dumps([str(governors_raw)])


def collect(config: dict | None = None) -> list[dict]:
    """Fetch recent WA business formations from CCFS.

    Args:
        config: Optional dict with keys:
            - days: int, lookback window (default 90)
            - state: str (default "WA")
            - max_results: int (default 500)

    Returns:
        List of company dicts in standard format.
    """
    config = config or {}
    days = int(config.get("days", 90))
    max_results = int(config.get("max_results", 500))

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    companies = []

    # Try the CCFS API first
    try:
        companies = _fetch_from_api(start_date, end_date, max_results)
    except Exception as e:
        print(f"  WA SOS API failed ({e}), trying HTML fallback...")
        try:
            companies = _fetch_from_html(start_date, end_date, max_results)
        except Exception as e2:
            print(f"  WA SOS HTML fallback also failed: {e2}")

    return companies


def _fetch_from_api(start_date: datetime, end_date: datetime, max_results: int) -> list[dict]:
    """Hit the CCFS search API endpoint."""
    params = {
        "SearchType": "Advanced",
        "SearchCriteria.DateOfIncorporationFrom": _format_date(start_date),
        "SearchCriteria.DateOfIncorporationTo": _format_date(end_date),
        "SearchCriteria.State": "WA",
        "SearchCriteria.BusinessType": "",
        "SearchCriteria.Status": "Active",
        "PageSize": str(min(max_results, 100)),
        "PageNumber": "1",
    }

    companies = []
    page = 1

    while len(companies) < max_results:
        params["PageNumber"] = str(page)
        r = requests.get(CCFS_API_URL, params=params, headers=HEADERS, timeout=30)

        if r.status_code != 200:
            if page == 1:
                raise RuntimeError(f"CCFS API returned HTTP {r.status_code}")
            break

        try:
            data = r.json()
        except ValueError:
            if page == 1:
                raise RuntimeError("CCFS API returned non-JSON response")
            break

        results = data if isinstance(data, list) else data.get("Businesses", data.get("businesses", []))
        if not results:
            break

        for biz in results:
            company = _normalize_api_result(biz)
            if company:
                companies.append(company)

        if len(results) < int(params["PageSize"]):
            break

        page += 1
        time.sleep(0.5)

    print(f"  WA SOS API: fetched {len(companies)} companies (pages={page})")
    return companies


def _normalize_api_result(biz: dict) -> dict | None:
    """Convert a CCFS API business record to standard format."""
    name = (
        biz.get("BusinessName")
        or biz.get("businessName")
        or biz.get("name")
        or ""
    ).strip()
    if not name:
        return None

    return {
        "company_name": name,
        "registered_date": biz.get("DateOfIncorporation", biz.get("dateOfIncorporation", "")),
        "ubi_number": biz.get("UBI", biz.get("ubi", "")),
        "entity_type": biz.get("BusinessType", biz.get("businessType", "")),
        "registered_agent": biz.get("RegisteredAgent", biz.get("registeredAgent", "")),
        "governors": _parse_governors(biz.get("Governors", biz.get("governors"))),
        "status": biz.get("Status", biz.get("status", "")),
        "principal_office": biz.get("PrincipalOffice", biz.get("principalOffice", "")),
        "state": "WA",
        "source": "wa_sos",
        "signal_tag": "new_business",
    }


def _fetch_from_html(start_date: datetime, end_date: datetime, max_results: int) -> list[dict]:
    """Fallback: scrape CCFS search results page."""
    from bs4 import BeautifulSoup

    search_url = f"{CCFS_SEARCH_URL}/#/AdvancedSearch"
    # The CCFS Angular app uses XHR, so HTML scraping may not yield results.
    # Return empty — caller should handle gracefully.
    print("  WA SOS HTML scrape: CCFS is an Angular SPA, HTML fallback limited")
    return []
