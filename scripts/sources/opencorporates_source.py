"""opencorporates_source.py — Search OpenCorporates for recent incorporations.

API: https://api.opencorporates.com/v0.4/companies/search
Requires API key (free tier discontinued for keyless access).
Set OPENCORPORATES_API_KEY env var if you have a key.

Falls back gracefully to 0 results if no key or API unavailable.

Output: list of dicts in standard company format.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import requests

OPENCORP_SEARCH = "https://api.opencorporates.com/v0.4/companies/search"
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

    api_key = os.environ.get("OPENCORPORATES_API_KEY", "").strip()

    since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    companies = []

    try:
        companies = _fetch_from_api(since_date, jurisdiction, max_pages, api_key)
    except Exception as e:
        print(f"  OpenCorporates API failed: {e}")
        if not api_key:
            print("  Tip: Set OPENCORPORATES_API_KEY env var for API access")
        print("  OpenCorporates: returning 0 results (non-fatal)")

    return companies


def _fetch_from_api(since_date: str, jurisdiction: str, max_pages: int, api_key: str) -> list[dict]:
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
        if api_key:
            params["api_token"] = api_key

        r = requests.get(OPENCORP_SEARCH, params=params, headers=HEADERS, timeout=15)

        if r.status_code in (401, 403):
            if page == 1:
                raise RuntimeError(
                    f"OpenCorporates API returned HTTP {r.status_code} — "
                    f"API key {'provided but invalid' if api_key else 'required (set OPENCORPORATES_API_KEY)'}"
                )
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
