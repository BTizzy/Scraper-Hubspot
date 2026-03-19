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
    r"\b(LLC|Inc\.?|Corp\.?|Corporation|Co\.|Company|Ltd\.?|PLLC|LP|LLP)\b",
    re.IGNORECASE,
)

LAWSUIT_TERMS = re.compile(
    r"\b(lawsuit|litigation|defendant|plaintiff|sued|complaint|docket|v\.\s)\b",
    re.IGNORECASE,
)

CHANGE_TERMS = re.compile(
    r"\b(sold|acquired|under new management|merged|transfer|new ownership|business sale|rebranded?)\b",
    re.IGNORECASE,
)

# Blacklist common false-positive "company names" from form-filling sites, ads, how-to articles
FALSE_POSITIVE_NAMES = {
    "form your llc", "start your llc", "file your llc", "register your llc",
    "your llc", "the llc", "my llc", "an llc", "new llc", "best llc",
    "form an llc", "start an llc", "create an llc", "open an llc",
    "how to form", "how to start", "how to file", "how to register",
    "llc formation", "llc filing", "llc registration", "llc service",
    "business formation", "legal zoom", "legalzoom", "incfile",
    "northwest registered agent", "zenbusiness", "swyft filings",
    "rocket lawyer", "bizfilings", "harbor compliance",
    "registered agent", "statutory agent", "formation service",
    "your business", "small business", "new business",
    "example llc", "sample llc", "test llc", "demo llc",
    "any company", "some company", "this company",
}

# Domains to skip (form-filling services, not actual companies)
SKIP_DOMAINS = {
    "legalzoom.com", "incfile.com", "zenbusiness.com", "nolo.com",
    "swyftfilings.com", "rocketlawyer.com", "northwest.com",
    "bizfilings.com", "corpnet.com", "mycorporation.com",
    "harborcompliance.com", "incorporations.com",
    "score.org", "sba.gov", "irs.gov", "nolo.com",
    "wikihow.com", "wikipedia.org", "investopedia.com",
    "forbes.com", "entrepreneur.com", "thebalancemoney.com",
    "indeed.com", "glassdoor.com", "yelp.com",
}


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

    # Deduplicate by normalized name
    seen = set()
    unique = []
    for c in companies:
        key = c["company_name"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(c)

    print(f"  Web discovery: found {len(unique)} companies from {len(queries)} queries")
    return unique


def _build_queries(state: str, year: int) -> list[tuple[str, str]]:
    """Build search queries for each signal type."""
    state_names = {
        "WA": "Washington state",
        "CA": "California",
        "NY": "New York",
        "OR": "Oregon",
        "TX": "Texas",
    }
    state_name = state_names.get(state, state)

    return [
        # New business formations — target SOS filings
        (f'new LLC formed {state_name} {year} site:sos.{state.lower()}.gov', "new_business"),
        (f'new business registration {state_name} {year} LLC Corp', "new_business"),
        # Lawsuits — target small business litigation
        (f'small business lawsuit employment {state_name} {year}', "active_lawsuit"),
        (f'company sued employee dispute {state_name} {year}', "active_lawsuit"),
        # Business changes — acquisitions, sales, rebrands
        (f'small business sold acquired new owner {state_name} {year}', "business_change"),
        (f'business transfer sale rebrand {state_name} {year}', "business_change"),
    ]


def _search_ddg(query: str) -> list[dict]:
    """Execute a DuckDuckGo HTML search and return results."""
    try:
        r = requests.post(DDG_HTML, data={"q": query, "b": ""}, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        results = []

        for result in soup.select(".result"):
            title_el = result.select_one("a.result__a")
            snippet_el = result.select_one(".result__snippet")

            if not title_el:
                continue

            url = title_el.get("href", "")

            # Skip results from known form-filling / how-to sites
            if _is_skip_domain(url):
                continue

            results.append({
                "title": title_el.get_text(strip=True),
                "url": url,
                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
            })

        return results[:10]  # Cap at 10 per query
    except Exception:
        return []


def _is_skip_domain(url: str) -> bool:
    """Check if URL is from a domain we should skip."""
    url_lower = url.lower()
    for domain in SKIP_DOMAINS:
        if domain in url_lower:
            return True
    return False


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

    # Extract the company name
    company_name = _extract_company_name(text)
    if not company_name:
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
    # Require at least 2 characters before the legal suffix
    matches = re.findall(
        r"([A-Z][A-Za-z0-9\s&'\-\.]{2,50}(?:LLC|Inc\.?|Corp\.?|Corporation|Co\.|Company|Ltd\.?|PLLC|LP|LLP))",
        text,
    )

    for raw_match in matches:
        name = raw_match.strip()
        # Trim leading common words that aren't part of company names
        name = re.sub(
            r"^(?:The|A|An|About|How|Why|What|When|To|For|Your|My|Our|Their|This|That|From|With|Form|File|Start|Create|Open|Register)\s+",
            "",
            name,
            flags=re.IGNORECASE,
        )
        name = name.strip()

        # Check minimum length (must be a real name, not just "LLC")
        name_without_suffix = COMPANY_SUFFIX.sub("", name).strip()
        if len(name_without_suffix) < 3:
            continue

        # Check against false positive blacklist
        if name.lower().strip() in FALSE_POSITIVE_NAMES:
            continue

        # Must have at least one word that looks like a proper noun before suffix
        name_core = COMPANY_SUFFIX.sub("", name).strip()
        words = name_core.split()
        if not words:
            continue

        # At least one word should start with uppercase (proper noun check)
        has_proper_noun = any(w[0].isupper() for w in words if w)
        if not has_proper_noun:
            continue

        return name

    return ""
