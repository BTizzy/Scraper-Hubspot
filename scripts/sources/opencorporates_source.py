"""opencorporates_source.py — Search OpenCorporates for recent incorporations.

API: https://api.opencorporates.com/v0.4/companies/search
Free tier: 200 searches/month, no key needed for basic search.
Also detects previous_names on companies → tags as business_change.

Output: list of dicts in standard company format.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import requests

OPENCORP_SEARCH = "https://api.opencorporates.com/v0.4/companies/search"
OPENCORP_HTML = "https://opencorporates.com/companies"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def collect(config: dict | None = None) -> list[dict]:
    """Search OpenCorporates for recently incorporated companies.

    Args:
        config: Optional dict with keys:
            - days: int, lookback window (default 90)
            - jurisdiction: str (default "us_wa")
            - max_pages: int (default 5)

    Returns:
        List of company dicts in standard format.
    """
    config = config or {}
    days = int(config.get("days", 90))
    jurisdiction = config.get("jurisdiction", "us_wa")
    max_pages = int(config.get("max_pages", 5))

    since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    companies = []

    try:
        companies = _fetch_from_api(since_date, jurisdiction, max_pages)
    except Exception as e:
        print(f"  OpenCorporates API failed ({e}), trying HTML fallback...")
        try:
            companies = _fetch_from_html(since_date, jurisdiction, max_pages)
        except Exception as e2:
            print(f"  OpenCorporates HTML fallback also failed: {e2}")

    return companies


def _fetch_from_api(since_date: str, jurisdiction: str, max_pages: int) -> list[dict]:
    """Use the OpenCorporates REST API."""
    companies = []

    for page in range(1, max_pages + 1):
        params = {
            "q": "*",
            "jurisdiction_code": jurisdiction,
            "incorporation_date": f"{since_date}:",
            "page": str(page),
            "per_page": "30",
        }

        r = requests.get(OPENCORP_SEARCH, params=params, headers=HEADERS, timeout=15)

        if r.status_code in (401, 403):
            if page == 1:
                raise RuntimeError(f"OpenCorporates API returned HTTP {r.status_code}")
            break

        if r.status_code == 429:
            print("  OpenCorporates: rate limited, stopping")
            break

        if r.status_code != 200:
            if page == 1:
                raise RuntimeError(f"OpenCorporates API returned HTTP {r.status_code}")
            break

        try:
            data = r.json()
        except ValueError:
            break

        results = data.get("results", {}).get("companies", [])
        if not results:
            break

        for item in results:
            company_data = item.get("company", {})
            company = _normalize_api_result(company_data)
            if company:
                companies.append(company)

        time.sleep(1)  # Respect rate limits

    print(f"  OpenCorporates API: fetched {len(companies)} companies")
    return companies


def _normalize_api_result(company_data: dict) -> dict | None:
    """Convert an OpenCorporates company record to standard format."""
    name = (company_data.get("name") or "").strip()
    if not name:
        return None

    previous_names = company_data.get("previous_names", []) or []
    has_name_change = len(previous_names) > 0

    signal_tag = "new_business"
    if has_name_change:
        signal_tag = "new_business;business_change"

    incorporation_date = company_data.get("incorporation_date", "")
    jurisdiction = company_data.get("jurisdiction_code", "")
    state = jurisdiction.replace("us_", "").upper() if jurisdiction.startswith("us_") else ""

    return {
        "company_name": name,
        "registered_date": incorporation_date or "",
        "ubi_number": company_data.get("company_number", ""),
        "entity_type": company_data.get("company_type", ""),
        "registered_agent": company_data.get("agent_name", ""),
        "governors": "[]",
        "status": company_data.get("current_status", ""),
        "principal_office": company_data.get("registered_address_in_full", ""),
        "state": state,
        "source": "opencorporates",
        "signal_tag": signal_tag,
    }


def _fetch_from_html(since_date: str, jurisdiction: str, max_pages: int) -> list[dict]:
    """Fallback: scrape OpenCorporates HTML search results."""
    from urllib.parse import quote_plus
    from bs4 import BeautifulSoup

    companies = []

    for page in range(1, min(max_pages, 3) + 1):
        url = (
            f"{OPENCORP_HTML}?"
            f"q=*&jurisdiction_code={jurisdiction}"
            f"&incorporation_date%5Bstart%5D={since_date}"
            f"&page={page}"
        )
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            for link in soup.select("a[href*='/companies/us_']"):
                name = link.get_text(strip=True)
                if name and len(name) > 2:
                    companies.append({
                        "company_name": name,
                        "registered_date": "",
                        "ubi_number": "",
                        "entity_type": "",
                        "registered_agent": "",
                        "governors": "[]",
                        "status": "",
                        "principal_office": "",
                        "state": "WA",
                        "source": "opencorporates",
                        "signal_tag": "new_business",
                    })
        except Exception:
            break

        time.sleep(1)

    print(f"  OpenCorporates HTML: fetched {len(companies)} companies")
    return companies
