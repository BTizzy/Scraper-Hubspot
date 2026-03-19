"""sos_scraper.py — Fetch new business formations from Secretary of State data.

Primary source: Oregon SOS via data.oregon.gov SODA API (free, no key needed).
Secondary source: WA CCFS (unreliable — Cloudflare Turnstile; graceful fallback).

Output: list of dicts with standard company fields + signal_tag="new_business"
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import requests

# Oregon SOS via Socrata Open Data API (SODA)
OR_SODA_ENDPOINT = "https://data.oregon.gov/resource/esjy-u4fc.json"

# WA CCFS — kept as fallback but known to be unreliable
CCFS_API_URL = "https://ccfs.sos.wa.gov/api/BusinessSearch/BusinessSearch"

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
    """Fetch recent business formations from SOS data sources.

    Args:
        config: Optional dict with keys:
            - days: int, lookback window (default 90)
            - state: str (default "OR" — Oregon has reliable free API)
            - max_results: int (default 500)

    Returns:
        List of company dicts in standard format.
    """
    config = config or {}
    days = int(config.get("days", 90))
    state = config.get("state", "OR").upper()
    max_results = int(config.get("max_results", 500))

    companies = []

    # Primary: Oregon SOS via SODA API (always available, free, reliable)
    try:
        or_results = _fetch_from_oregon_soda(days, max_results)
        companies.extend(or_results)
    except Exception as e:
        print(f"  Oregon SOS SODA API failed: {e}")

    # Secondary: WA SOS CCFS (known to be unreliable due to Cloudflare Turnstile)
    if state == "WA" or not companies:
        try:
            wa_results = _fetch_from_wa_ccfs(days, min(max_results, 100))
            companies.extend(wa_results)
        except Exception as e:
            print(f"  WA SOS CCFS failed (expected — Cloudflare protected): {e}")

    if not companies:
        print("  SOS scraper: no companies from any source")

    return companies


def _fetch_from_oregon_soda(days: int, max_results: int) -> list[dict]:
    """Fetch new business registrations from Oregon SOS via data.oregon.gov SODA API."""
    since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")

    # We need to deduplicate by registry_number since each business has multiple rows
    # (one per associated_name_type). We prefer INDIVIDUAL WITH DIRECT KNOWLEDGE for person data.
    companies: dict[str, dict] = {}  # keyed by registry_number
    offset = 0
    page_size = 1000  # SODA max

    while len(companies) < max_results:
        params = {
            "$where": f"registry_date > '{since_date}'",
            "$limit": str(page_size),
            "$offset": str(offset),
            "$order": "registry_date DESC",
        }

        try:
            r = requests.get(OR_SODA_ENDPOINT, params=params, headers=HEADERS, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"  Oregon SODA: request error at offset {offset}: {e}")
            break

        if r.status_code != 200:
            if offset == 0:
                raise RuntimeError(f"Oregon SODA API returned HTTP {r.status_code}")
            print(f"  Oregon SODA: HTTP {r.status_code} at offset {offset}, stopping")
            break

        try:
            rows = r.json()
        except ValueError:
            break

        if not rows:
            break

        for row in rows:
            reg_num = (row.get("registry_number") or "").strip()
            if not reg_num:
                continue

            biz_name = (row.get("business_name") or "").strip()
            if not biz_name:
                continue

            name_type = (row.get("associated_name_type") or "").strip()

            if reg_num not in companies:
                companies[reg_num] = _normalize_oregon_result(row)
            elif name_type == "INDIVIDUAL WITH DIRECT KNOWLEDGE":
                # Update with person data from the preferred row
                existing = companies[reg_num]
                first = (row.get("first_name") or "").strip()
                last = (row.get("last_name") or "").strip()
                if first or last:
                    person = f"{first} {last}".strip()
                    existing["registered_agent"] = person
                    existing["governors"] = json.dumps([person])
            elif name_type == "PRINCIPAL PLACE OF BUSINESS":
                # Update address
                existing = companies[reg_num]
                addr_parts = [
                    (row.get("address_") or "").strip(),
                    (row.get("city") or "").strip(),
                    (row.get("state") or "").strip(),
                    (row.get("zip_code") or "").strip(),
                ]
                addr = ", ".join(p for p in addr_parts if p)
                if addr:
                    existing["principal_office"] = addr

            if len(companies) >= max_results:
                break

        if len(rows) < page_size:
            break

        offset += page_size
        time.sleep(0.3)  # Be polite

    result = list(companies.values())
    print(f"  Oregon SOS SODA: fetched {len(result)} unique companies (from {offset + len(rows if rows else [])} rows)")
    return result


def _normalize_oregon_result(row: dict) -> dict:
    """Convert an Oregon SODA row to standard pipeline format."""
    name = (row.get("business_name") or "").strip()
    reg_date = (row.get("registry_date") or "")
    if "T" in reg_date:
        reg_date = reg_date.split("T")[0]

    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    person = f"{first} {last}".strip()

    addr_parts = [
        (row.get("address_") or "").strip(),
        (row.get("city") or "").strip(),
        (row.get("state") or "").strip(),
        (row.get("zip_code") or "").strip(),
    ]
    address = ", ".join(p for p in addr_parts if p)

    return {
        "company_name": name,
        "registered_date": reg_date,
        "ubi_number": row.get("registry_number", ""),
        "entity_type": row.get("entity_type", ""),
        "registered_agent": person,
        "governors": json.dumps([person]) if person else "[]",
        "status": "Active",
        "principal_office": address,
        "state": "OR",
        "source": "oregon_sos",
        "signal_tag": "new_business",
    }


def _fetch_from_wa_ccfs(days: int, max_results: int) -> list[dict]:
    """Try to fetch from WA CCFS API. Known to be unreliable (Cloudflare Turnstile)."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    params = {
        "SearchType": "Advanced",
        "SearchCriteria.DateOfIncorporationFrom": _format_date(start_date),
        "SearchCriteria.DateOfIncorporationTo": _format_date(end_date),
        "SearchCriteria.State": "WA",
        "SearchCriteria.Status": "Active",
        "PageSize": str(min(max_results, 100)),
        "PageNumber": "1",
    }

    r = requests.get(CCFS_API_URL, params=params, headers=HEADERS, timeout=15)

    if r.status_code != 200:
        raise RuntimeError(f"WA CCFS API returned HTTP {r.status_code}")

    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("WA CCFS API returned non-JSON response")

    results = data if isinstance(data, list) else data.get("Businesses", data.get("businesses", []))
    companies = []

    for biz in (results or []):
        name = (
            biz.get("BusinessName")
            or biz.get("businessName")
            or biz.get("name")
            or ""
        ).strip()
        if not name:
            continue

        companies.append({
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
        })

    print(f"  WA SOS CCFS: fetched {len(companies)} companies")
    return companies
