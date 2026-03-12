"""courtlistener_source.py — Search CourtListener for business lawsuits.

API: https://www.courtlistener.com/api/rest/v4/search/?type=d&q=<company_name>
Free, no key needed for search (key needed for full RECAP access).

Output: list of dicts with company_name, lawsuit info, signal_tag="active_lawsuit"
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import requests

CL_SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def collect(config: dict | None = None) -> list[dict]:
    """Search CourtListener for recent business lawsuits.

    Args:
        config: Optional dict with keys:
            - company_names: list of company names to search
            - days: int, lookback window (default 365)

    Returns:
        List of company dicts with lawsuit signals.
    """
    config = config or {}
    company_names = config.get("company_names", [])
    days = int(config.get("days", 365))

    if not company_names:
        return []

    since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    results = []

    token = os.environ.get("COURTLISTENER_API_KEY", "").strip()

    for name in company_names:
        try:
            matches = _search_company(name, since_date, token)
            for match in matches:
                results.append(match)
        except Exception as e:
            print(f"  CourtListener: error searching '{name}': {e}")

        time.sleep(1)  # Respect rate limits

    print(f"  CourtListener: found {len(results)} lawsuit matches across {len(company_names)} companies")
    return results


def _search_company(name: str, since_date: str, token: str) -> list[dict]:
    """Search CourtListener for a single company name."""
    params = {
        "type": "d",
        "q": f'"{name}"',
        "filed_after": since_date,
    }

    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Token {token}"

    try:
        r = requests.get(CL_SEARCH, params=params, headers=headers, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"  CourtListener: request failed for '{name}': {e}")
        return []

    if r.status_code != 200:
        return []

    try:
        data = r.json()
    except ValueError:
        return []

    raw_results = data.get("results", [])
    matches = []

    for item in raw_results:
        case_name = item.get("caseName", "")
        if not case_name:
            continue

        matches.append({
            "company_name": name,
            "registered_date": "",
            "ubi_number": "",
            "entity_type": "",
            "registered_agent": "",
            "governors": "[]",
            "status": "",
            "principal_office": "",
            "state": "",
            "source": "courtlistener",
            "signal_tag": "active_lawsuit",
            "lawsuit_case": case_name,
            "lawsuit_court": item.get("court_id", ""),
            "lawsuit_date": item.get("dateFiled", ""),
        })

    return matches
