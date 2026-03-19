"""collect_from_web.py — Master collector for public web sources.

Calls all source modules, deduplicates by company_name, merges signal_tags
when the same company is found in multiple sources, and outputs a single CSV
compatible with the pipeline's --sos input format.

Usage:
  python collect_from_web.py --output companies_input.csv --days 90 --state WA
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from sources import sos_scraper, opencorporates_source, courtlistener_source, web_discovery


OUTPUT_FIELDS = [
    "company_name",
    "registered_date",
    "ubi_number",
    "entity_type",
    "registered_agent",
    "governors",
    "status",
    "principal_office",
    "state",
    "source",
    "signal_tag",
    "collected_date",
]

LEGAL_SUFFIX = re.compile(
    r"\b(llc|inc\.?|corp\.?|corporation|co\.?|company|ltd\.?|pllc|lp|llp|group|services|holdings)\b",
    re.IGNORECASE,
)


def normalize_company_name(name: str) -> str:
    """Normalize a company name for deduplication."""
    if not name:
        return ""
    cleaned = LEGAL_SUFFIX.sub("", name.lower())
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def merge_signal_tags(existing: str, new: str) -> str:
    """Merge two semicolon-separated signal tag strings."""
    tags = set()
    for tag_str in (existing, new):
        for tag in (tag_str or "").split(";"):
            tag = tag.strip()
            if tag:
                tags.add(tag)
    return ";".join(sorted(tags))


def merge_sources(existing: str, new: str) -> str:
    """Merge two semicolon-separated source strings."""
    sources = set()
    for src_str in (existing, new):
        for src in (src_str or "").split(";"):
            src = src.strip()
            if src:
                sources.add(src)
    return ";".join(sorted(sources))


def deduplicate(companies: list[dict]) -> list[dict]:
    """Deduplicate companies by normalized name, merging signal_tags and sources."""
    seen: dict[str, dict] = {}

    for company in companies:
        name = company.get("company_name", "")
        key = normalize_company_name(name)
        if not key:
            continue

        if key in seen:
            existing = seen[key]
            existing["signal_tag"] = merge_signal_tags(
                existing.get("signal_tag", ""),
                company.get("signal_tag", ""),
            )
            existing["source"] = merge_sources(
                existing.get("source", ""),
                company.get("source", ""),
            )
            # Keep richer data if existing is empty
            for field in ("registered_date", "ubi_number", "entity_type",
                          "registered_agent", "governors", "principal_office"):
                if not existing.get(field) and company.get(field):
                    existing[field] = company[field]
        else:
            seen[key] = dict(company)

    return list(seen.values())


def collect_all(config: dict) -> list[dict]:
    """Run all source collectors and return merged results."""
    all_companies = []

    # Source 1: SOS Business Registrations (Oregon SODA API + WA CCFS fallback)
    print("\n  [1/4] Secretary of State business registrations...")
    try:
        sos_results = sos_scraper.collect(config)
        all_companies.extend(sos_results)
    except Exception as e:
        print(f"    Error: {e}")

    # Source 2: OpenCorporates
    print("\n  [2/4] OpenCorporates API...")
    try:
        oc_results = opencorporates_source.collect(config)
        all_companies.extend(oc_results)
    except Exception as e:
        print(f"    Error: {e}")

    # Source 3: CourtListener (needs company names from other sources)
    print("\n  [3/4] CourtListener lawsuit search...")
    try:
        company_names = list({c["company_name"] for c in all_companies if c.get("company_name")})
        if company_names:
            cl_config = {**config, "company_names": company_names[:50]}  # Cap at 50
            cl_results = courtlistener_source.collect(cl_config)
            all_companies.extend(cl_results)
        else:
            print("    Skipped: no company names to search")
    except Exception as e:
        print(f"    Error: {e}")

    # Source 4: Web discovery (DuckDuckGo)
    print("\n  [4/4] Web discovery (DuckDuckGo)...")
    try:
        web_results = web_discovery.collect(config)
        all_companies.extend(web_results)
    except Exception as e:
        print(f"    Error: {e}")

    return all_companies


def write_csv(companies: list[dict], output_path: str) -> None:
    """Write companies to CSV file."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for company in companies:
            writer.writerow(company)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect companies from public web sources"
    )
    parser.add_argument("--output", "-o", required=True, help="Output CSV path")
    parser.add_argument("--days", type=int, default=90, help="Lookback window in days (default: 90)")
    parser.add_argument("--state", default="WA", help="Target state (default: WA)")
    args = parser.parse_args()

    print(f"  Collecting companies from web sources (days={args.days}, state={args.state})")

    config = {
        "days": args.days,
        "state": args.state,
    }

    all_companies = collect_all(config)

    # Add collected_date
    today = datetime.now().strftime("%Y-%m-%d")
    for company in all_companies:
        company["collected_date"] = today

    # Deduplicate
    before_count = len(all_companies)
    companies = deduplicate(all_companies)
    dedup_count = before_count - len(companies)

    print(f"\n  Summary:")
    print(f"    Total collected: {before_count}")
    print(f"    Deduplicated: {dedup_count}")
    print(f"    Unique companies: {len(companies)}")

    # Count by signal
    signal_counts: dict[str, int] = {}
    for c in companies:
        for tag in (c.get("signal_tag", "") or "").split(";"):
            tag = tag.strip()
            if tag:
                signal_counts[tag] = signal_counts.get(tag, 0) + 1
    print(f"    Signal distribution: {signal_counts}")

    # Count by source
    source_counts: dict[str, int] = {}
    for c in companies:
        for src in (c.get("source", "") or "").split(";"):
            src = src.strip()
            if src:
                source_counts[src] = source_counts.get(src, 0) + 1
    print(f"    Source distribution: {source_counts}")

    write_csv(companies, args.output)
    print(f"\n  Output: {args.output} ({len(companies)} companies)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
