"""web_discovery.py — DuckDuckGo web search for company signals.

Searches for:
  - New business formations from SOS sites
  - Business lawsuits and employment disputes
  - Business transfers, acquisitions, new management

Uses DuckDuckGo HTML search (no API key needed).
Conservative extraction — only tags companies where signal is clear.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

DDG_HTML = "https://html.duckduckgo.com/html/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# Patterns for extracting company names from search results
COMPANY_SUFFIX = re.compile(
    r"\b(LLC|Inc\.?|Corp\.?|Corporation|Co\.?|Company|Ltd\.?|PLLC|LP|LLP)\b",
    re.IGNORECASE,
)

LAWSUIT_TERMS = re.compile(
    r"\b(lawsuit|litigation|defendant|plaintiff|sued|complaint|docket)\b",
    re.IGNORECASE,
)

CHANGE_TERMS = re.compile(
    r"\b(sold|acquired|under new management|merged|transfer|new ownership|business sale|rebranded?)\b",
    re.IGNORECASE,
)


def collect(config: dict | None = None) -> list[dict]:
    """Search the web for company signals.

    Args:
        config: Optional dict with keys:
            - state: str (default "WA")
            - days: int, year hint for search queries
            - max_results_per_query: int (default 10)

    Returns:
        List of company dicts with signal tags.
    """
    config = config or {}
    state = config.get("state", "WA")
    year = datetime.now().year

    queries = _build_queries(state, year)
    companies = []

    for query, signal_type in queries:
        try:
            results = _search_ddg(query)
            for result in results:
                company = _extract_company(result, signal_type, state)
                if company:
                    companies.append(company)
        except Exception as e:
            print(f"  Web discovery: error for query '{query}': {e}")

        time.sleep(2)  # Be conservative with DDG

    print(f"  Web discovery: found {len(companies)} companies from {len(queries)} queries")
    return companies


def _build_queries(state: str, year: int) -> list[tuple[str, str]]:
    """Build search queries for each signal type."""
    state_names = {
        "WA": "Washington",
        "CA": "California",
        "NY": "New York",
        "OR": "Oregon",
    }
    state_name = state_names.get(state, state)

    return [
        (f'"new business" "LLC" site:sos.{state.lower()}.gov {year}', "new_business"),
        (f'"new business formation" {state_name} {year}', "new_business"),
        (f'"lawsuit" "employment" {state_name} company {year}', "active_lawsuit"),
        (f'"sold" OR "acquired" OR "new management" small business {state_name} {year}', "business_change"),
    ]


def _search_ddg(query: str) -> list[dict]:
    """Execute a DuckDuckGo HTML search and return results."""
    try:
        r = requests.get(DDG_HTML, params={"q": query}, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        results = []

        for result in soup.select(".result"):
            title_el = result.select_one("a.result__a")
            snippet_el = result.select_one(".result__snippet")

            if not title_el:
                continue

            results.append({
                "title": title_el.get_text(strip=True),
                "url": title_el.get("href", ""),
                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
            })

        return results[:10]  # Cap at 10 per query
    except Exception:
        return []


def _extract_company(result: dict, signal_type: str, state: str) -> dict | None:
    """Extract a company from a search result if signal is clear."""
    title = result.get("title", "")
    snippet = result.get("snippet", "")
    text = f"{title} {snippet}"

    # Must have a company suffix to be considered
    if not COMPANY_SUFFIX.search(text):
        return None

    # For lawsuit signals, verify lawsuit terms are present
    if signal_type == "active_lawsuit" and not LAWSUIT_TERMS.search(text):
        return None

    # For business change signals, verify change terms are present
    if signal_type == "business_change" and not CHANGE_TERMS.search(text):
        return None

    # Extract the company name (text before or containing the legal suffix)
    company_name = _extract_company_name(text)
    if not company_name or len(company_name) < 3:
        return None

    return {
        "company_name": company_name,
        "registered_date": "",
        "ubi_number": "",
        "entity_type": "",
        "registered_agent": "",
        "governors": "[]",
        "status": "",
        "principal_office": "",
        "state": state,
        "source": "web_discovery",
        "signal_tag": signal_type,
    }


def _extract_company_name(text: str) -> str:
    """Extract the most likely company name from text."""
    # Look for patterns like "Company Name LLC" or "Company Name, Inc."
    match = re.search(
        r"([A-Z][A-Za-z\s&'\-\.]+(?:LLC|Inc\.?|Corp\.?|Corporation|Co\.?|Company|Ltd\.?|PLLC|LP|LLP))",
        text,
    )
    if match:
        name = match.group(1).strip()
        # Trim leading common words
        name = re.sub(r"^(?:The|A|An)\s+", "", name, flags=re.IGNORECASE)
        if len(name) > 3:
            return name

    return ""
