"""build_input_pool.py

Build a normalized, deduplicated company input pool from multiple public sources.

Primary use:
  1) Ingest one or more local CSV sources (SOS/manual/company lists)
  2) Enrich/repair domains via existing discovery helpers
  3) Add extra domains from broad web/search and jobs/news discovery
  4) Filter against state (seen companies) and emit pool + JSON report

Output schema is compatible with run_pipeline.py / daily_contract_runner.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from collect_companies import discover_website, normalize_domain, search_opencorporates, scrape_opencorporates_html


HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

POOL_FIELDS = [
    "company_name",
    "registered_date",
    "website",
    "domain",
    "company_number",
    "jurisdiction_code",
    "current_status",
    "registered_address",
    "signal_tag",
    "source",
    "collected_date",
]

COMPANY_COL_CANDIDATES = [
    "company_name",
    "Company",
    "Company Name",
    "name",
    "organization",
    "business_name",
]

SKIP_DOMAINS = {
    "facebook.com",
    "linkedin.com",
    "yelp.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "wikipedia.org",
    "reddit.com",
    "medium.com",
    "indeed.com",
    "glassdoor.com",
    "opencorporates.com",
    "zoominfo.com",
    "crunchbase.com",
}


def normalize_company_key(row: dict) -> str:
    company = (row.get("company_name") or row.get("Company") or row.get("name") or "").strip().lower()
    domain = (row.get("domain") or row.get("website") or "").strip().lower()
    return f"{company}|{domain}" if company else ""


def read_csv_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def load_seen_companies(path: Path) -> set[str]:
    if not path.exists():
        return set()
    _, rows = read_csv_rows(path)
    seen = set()
    for row in rows:
        key = (row.get("company_key") or "").strip().lower()
        if key:
            seen.add(key)
    return seen


def find_company_col(fieldnames: list[str]) -> str:
    field_map = {f.lower(): f for f in fieldnames}
    for candidate in COMPANY_COL_CANDIDATES:
        if candidate.lower() in field_map:
            return field_map[candidate.lower()]
    raise ValueError(f"Missing company name column. Tried: {COMPANY_COL_CANDIDATES}")


def normalize_input_row(row: dict, source_name: str, company_col: str) -> dict:
    company_name = (row.get(company_col) or "").strip()
    website = (row.get("website") or row.get("Website") or "").strip()
    domain = (row.get("domain") or row.get("Domain") or normalize_domain(website)).strip().lower()
    if domain and not website:
        website = f"https://{domain}"

    normalized = {
        "company_name": company_name,
        "registered_date": (row.get("registered_date") or row.get("Registered Date") or "").strip(),
        "website": website,
        "domain": domain,
        "company_number": (row.get("company_number") or row.get("Company Number") or row.get("ubi_number") or "").strip(),
        "jurisdiction_code": (row.get("jurisdiction_code") or "us_wa").strip() or "us_wa",
        "current_status": (row.get("current_status") or row.get("status") or "").strip(),
        "registered_address": (row.get("registered_address") or row.get("principal_office") or "").strip(),
        "signal_tag": (row.get("signal_tag") or "").strip(),
        "source": source_name,
        "collected_date": datetime.now().strftime("%Y-%m-%d"),
    }
    return normalized


def maybe_enrich_domain_and_registry(row: dict, should_enrich: bool, deadline_ts: float) -> dict:
    if not should_enrich or time.time() >= deadline_ts:
        return row

    enriched = dict(row)
    company_name = (row.get("company_name") or "").strip()
    if not company_name:
        return enriched

    if not (enriched.get("domain") or "").strip():
        discovered = discover_website(company_name)
        if discovered:
            enriched["domain"] = discovered.lower()
            enriched["website"] = f"https://{discovered.lower()}"

    if time.time() >= deadline_ts:
        return enriched

    # Try API first; fallback HTML scrape for registry metadata.
    oc = search_opencorporates(company_name)
    if not oc:
        oc = scrape_opencorporates_html(company_name)

    if oc:
        if not enriched.get("company_number"):
            enriched["company_number"] = (oc.get("company_number") or "").strip()
        if not enriched.get("jurisdiction_code"):
            enriched["jurisdiction_code"] = (oc.get("jurisdiction_code") or "us_wa").strip() or "us_wa"
        if not enriched.get("current_status"):
            enriched["current_status"] = (oc.get("current_status") or "").strip()
        if not enriched.get("registered_address"):
            enriched["registered_address"] = (oc.get("registered_address") or "").strip()
        if not enriched.get("registered_date"):
            enriched["registered_date"] = (oc.get("incorporation_date") or "").strip()

    # Ensure new-business signal exists if missing.
    tags = {t.strip() for t in (enriched.get("signal_tag") or "").split(";") if t.strip()}
    if "new_business" not in tags:
        tags.add("new_business")
    enriched["signal_tag"] = ";".join(sorted(tags))
    return enriched


def _is_allowed_domain(domain: str) -> bool:
    if not domain:
        return False
    if any(domain == d or domain.endswith(f".{d}") for d in SKIP_DOMAINS):
        return False
    return "." in domain and len(domain) >= 5


def _domain_to_name(domain: str) -> str:
    left = domain.split(".")[0]
    tokens = [t for t in re.split(r"[^a-z0-9]+", left.lower()) if t]
    if not tokens:
        return ""
    return " ".join(t.capitalize() for t in tokens)


def extract_domains_from_ddg(query: str, max_results: int = 30) -> list[str]:
    def extract_target(href: str) -> str:
        parsed = urlparse(href)
        if parsed.scheme in ("http", "https") and parsed.netloc and "duckduckgo.com" not in parsed.netloc:
            return href
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg", [])
        if uddg:
            return unquote(uddg[0])
        return href

    domains = []
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=HEADERS,
            timeout=12,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a.result__a"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            target = extract_target(href)
            parsed = urlparse(target)
            domain = (parsed.netloc or "").lower().strip()
            if domain.startswith("www."):
                domain = domain[4:]
            if _is_allowed_domain(domain):
                domains.append(domain)
            if len(domains) >= max_results:
                break
    except Exception:
        return []
    # Keep insertion order unique.
    return list(dict.fromkeys(domains))


def search_discovery_rows(city: str, max_domains: int, deadline_ts: float) -> list[dict]:
    queries = [
        f"{city} \"LLC\" \"about us\"",
        f"{city} \"Inc\" \"contact\"",
        f"{city} \"small business\" \"our team\"",
    ]
    rows = []
    seen = set()
    per_query_cap = max(1, max_domains // max(1, len(queries)))

    for query in queries:
        if time.time() >= deadline_ts or len(rows) >= max_domains:
            break
        domains = extract_domains_from_ddg(query, max_results=per_query_cap)
        for domain in domains:
            if domain in seen:
                continue
            seen.add(domain)
            company_name = _domain_to_name(domain)
            if not company_name:
                continue
            rows.append({
                "company_name": company_name,
                "registered_date": "",
                "website": f"https://{domain}",
                "domain": domain,
                "company_number": "",
                "jurisdiction_code": "us_wa",
                "current_status": "",
                "registered_address": "",
                "signal_tag": "manual_review",
                "source": "search_discovery",
                "collected_date": datetime.now().strftime("%Y-%m-%d"),
            })
            if len(rows) >= max_domains:
                break
    return rows


def jobs_news_discovery_rows(city: str, max_domains: int, deadline_ts: float) -> list[dict]:
    queries = [
        f"{city} \"we are hiring\" \"human resources\"",
        f"{city} \"careers\" \"talent acquisition\"",
        f"{city} \"press release\" \"expanding team\"",
    ]
    rows = []
    seen = set()
    per_query_cap = max(1, max_domains // max(1, len(queries)))

    for query in queries:
        if time.time() >= deadline_ts or len(rows) >= max_domains:
            break
        domains = extract_domains_from_ddg(query, max_results=per_query_cap)
        for domain in domains:
            if domain in seen:
                continue
            seen.add(domain)
            company_name = _domain_to_name(domain)
            if not company_name:
                continue
            rows.append({
                "company_name": company_name,
                "registered_date": "",
                "website": f"https://{domain}",
                "domain": domain,
                "company_number": "",
                "jurisdiction_code": "us_wa",
                "current_status": "",
                "registered_address": "",
                "signal_tag": "active_hiring;manual_review",
                "source": "jobs_news_discovery",
                "collected_date": datetime.now().strftime("%Y-%m-%d"),
            })
            if len(rows) >= max_domains:
                break
    return rows


def li_safe_discovery_rows(city: str, max_domains: int, deadline_ts: float) -> list[dict]:
    """Discover company candidates via search-engine-visible LinkedIn company slugs.

    This does not authenticate to LinkedIn and only uses public search result snippets.
    """
    queries = [
        f'site:linkedin.com/company "{city}"',
        f'site:linkedin.com/company "{city}" "hiring"',
        f'site:linkedin.com/company "{city}" "human resources"',
    ]
    rows = []
    seen_names = set()
    per_query_cap = max(1, max_domains // max(1, len(queries)))

    for query in queries:
        if time.time() >= deadline_ts or len(rows) >= max_domains:
            break
        try:
            r = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=HEADERS,
                timeout=12,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            slugs = []
            for a in soup.select("a.result__a"):
                href = (a.get("href") or "").strip().lower()
                m = re.search(r"linkedin\.com/company/([a-z0-9\-]+)", href)
                if not m:
                    continue
                slugs.append(m.group(1))
                if len(slugs) >= per_query_cap:
                    break

            for slug in slugs:
                if time.time() >= deadline_ts or len(rows) >= max_domains:
                    break
                company_name = " ".join(part.capitalize() for part in slug.split("-") if part).strip()
                if not company_name:
                    continue
                lname = company_name.lower()
                if lname in seen_names:
                    continue
                seen_names.add(lname)

                # Resolve website via existing safe discovery helper.
                domain = discover_website(company_name)
                if not domain:
                    continue
                rows.append({
                    "company_name": company_name,
                    "registered_date": "",
                    "website": f"https://{domain}",
                    "domain": domain,
                    "company_number": "",
                    "jurisdiction_code": "us_wa",
                    "current_status": "",
                    "registered_address": "",
                    "signal_tag": "active_hiring;manual_review",
                    "source": "li_safe_discovery",
                    "collected_date": datetime.now().strftime("%Y-%m-%d"),
                })
        except Exception:
            continue

    return rows


def apply_budget_overrides(args: argparse.Namespace) -> argparse.Namespace:
    if not args.source_budget_json:
        return args
    try:
        raw = json.loads(args.source_budget_json)
    except Exception:
        return args

    if "search" in raw:
        args.max_search_domains = max(0, int(raw["search"]))
    if "jobs_news" in raw:
        args.max_jobs_news_domains = max(0, int(raw["jobs_news"]))
    if "li_safe" in raw:
        args.max_li_safe_domains = max(0, int(raw["li_safe"]))
    if "enrichment_rows" in raw:
        args.max_enrichment_rows = max(0, int(raw["enrichment_rows"]))
    return args


def merge_rows(rows: list[dict]) -> tuple[list[dict], int]:
    by_key: dict[str, dict] = {}
    deduped = 0

    for row in rows:
        key = normalize_company_key(row)
        if not key:
            # fallback key on company only
            company = (row.get("company_name") or "").strip().lower()
            if not company:
                continue
            key = f"{company}|"

        if key not in by_key:
            by_key[key] = dict(row)
            continue

        deduped += 1
        current = by_key[key]
        # Merge sparse fields.
        for field in [
            "registered_date",
            "website",
            "domain",
            "company_number",
            "jurisdiction_code",
            "current_status",
            "registered_address",
        ]:
            if not (current.get(field) or "").strip() and (row.get(field) or "").strip():
                current[field] = row[field]

        tags = {t.strip() for t in (current.get("signal_tag") or "").split(";") if t.strip()}
        tags.update({t.strip() for t in (row.get("signal_tag") or "").split(";") if t.strip()})
        current["signal_tag"] = ";".join(sorted(tags))

        sources = {s.strip() for s in (current.get("source") or "").split(";") if s.strip()}
        sources.update({s.strip() for s in (row.get("source") or "").split(";") if s.strip()})
        current["source"] = ";".join(sorted(sources))

    return list(by_key.values()), deduped


def main() -> int:
    parser = argparse.ArgumentParser(description="Build multi-source company input pool")
    parser.add_argument("--sos", action="append", default=[], help="SOS/company CSV input (repeatable)")
    parser.add_argument("--manual", action="append", default=[], help="Manual company CSV input (repeatable)")
    parser.add_argument("--output", required=True, help="Output pool CSV path")
    parser.add_argument("--report", default="", help="Optional output JSON report path")
    parser.add_argument("--exclude-state-dir", default="", help="Optional state dir with seen_companies.csv")
    parser.add_argument("--max-runtime-minutes", type=int, default=90, help="Time budget for discovery+enrichment")
    parser.add_argument("--city", default="Seattle WA", help="City phrase used in discovery queries")
    parser.add_argument("--max-search-domains", type=int, default=120, help="Max domains from search discovery")
    parser.add_argument("--max-jobs-news-domains", type=int, default=120, help="Max domains from jobs/news discovery")
    parser.add_argument("--enable-li-safe-discovery", action="store_true", help="Enable LI-safe discovery via public search snippets")
    parser.add_argument("--max-li-safe-domains", type=int, default=60, help="Max domains from LI-safe discovery")
    parser.add_argument("--max-enrichment-rows", type=int, default=0, help="Optional cap on input rows to enrich (0 means no cap)")
    parser.add_argument("--source-budget-json", default="", help="Optional JSON overrides: {\"search\":N,\"jobs_news\":N,\"li_safe\":N,\"enrichment_rows\":N}")
    parser.add_argument("--disable-domain-enrichment", action="store_true", help="Skip per-company domain enrichment")
    parser.add_argument("--disable-search-discovery", action="store_true", help="Disable broad search discovery")
    parser.add_argument("--disable-jobs-news-discovery", action="store_true", help="Disable jobs/news discovery")
    parser.add_argument("--allow-missing-domain", action="store_true", help="Allow rows without domain in output")
    args = parser.parse_args()

    args = apply_budget_overrides(args)

    if not args.sos and not args.manual:
        print("ERROR: provide at least one --sos or --manual input file")
        return 2

    start_ts = time.time()
    deadline_ts = start_ts + max(1, args.max_runtime_minutes) * 60

    input_rows: list[dict] = []
    source_input_counts = Counter()
    rejected = defaultdict(int)

    all_inputs = [(Path(p), "sos") for p in args.sos] + [(Path(p), "manual") for p in args.manual]
    for path, source_kind in all_inputs:
        if not path.exists():
            rejected["missing_input_file"] += 1
            continue
        fields, rows = read_csv_rows(path)
        if not fields:
            rejected["empty_input_file"] += 1
            continue
        try:
            company_col = find_company_col(fields)
        except ValueError:
            rejected["missing_company_column"] += 1
            continue

        source_name = f"{source_kind}:{path.stem}"
        for row in rows:
            normalized = normalize_input_row(row, source_name, company_col)
            if not normalized["company_name"]:
                rejected["missing_company_name"] += 1
                continue
            input_rows.append(normalized)
            source_input_counts[source_name] += 1

    enriched_rows = []
    for idx, row in enumerate(input_rows, start=1):
        if args.max_enrichment_rows > 0 and idx > args.max_enrichment_rows:
            rejected["enrichment_row_cap"] += 1
            break
        if time.time() >= deadline_ts:
            rejected["runtime_budget_reached"] += 1
            break
        enriched_rows.append(
            maybe_enrich_domain_and_registry(
                row,
                should_enrich=not args.disable_domain_enrichment,
                deadline_ts=deadline_ts,
            )
        )

    discovery_rows = []
    if not args.disable_search_discovery and time.time() < deadline_ts:
        discovery_rows.extend(
            search_discovery_rows(
                city=args.city,
                max_domains=max(0, args.max_search_domains),
                deadline_ts=deadline_ts,
            )
        )

    if not args.disable_jobs_news_discovery and time.time() < deadline_ts:
        discovery_rows.extend(
            jobs_news_discovery_rows(
                city=args.city,
                max_domains=max(0, args.max_jobs_news_domains),
                deadline_ts=deadline_ts,
            )
        )

    if args.enable_li_safe_discovery and time.time() < deadline_ts:
        discovery_rows.extend(
            li_safe_discovery_rows(
                city=args.city,
                max_domains=max(0, args.max_li_safe_domains),
                deadline_ts=deadline_ts,
            )
        )

    all_rows = enriched_rows + discovery_rows
    merged_rows, deduped_count = merge_rows(all_rows)

    seen_company_keys: set[str] = set()
    if args.exclude_state_dir:
        seen_path = Path(args.exclude_state_dir).resolve() / "seen_companies.csv"
        seen_company_keys = load_seen_companies(seen_path)

    final_rows = []
    source_output_counts = Counter()
    for row in merged_rows:
        key = normalize_company_key(row)
        if seen_company_keys and key and key in seen_company_keys:
            rejected["already_seen_company"] += 1
            continue

        domain = (row.get("domain") or "").strip().lower()
        website = (row.get("website") or "").strip()
        if not domain and website:
            domain = normalize_domain(website)
            row["domain"] = domain
        if domain and not website:
            row["website"] = f"https://{domain}"

        if not args.allow_missing_domain and not (row.get("domain") or "").strip():
            rejected["missing_domain"] += 1
            continue

        # Keep only canonical output fields.
        out = {field: (row.get(field) or "").strip() for field in POOL_FIELDS}
        if not out["collected_date"]:
            out["collected_date"] = datetime.now().strftime("%Y-%m-%d")
        final_rows.append(out)
        for src in [s for s in out["source"].split(";") if s]:
            source_output_counts[src] += 1

    output_path = Path(args.output).resolve()
    write_csv_rows(output_path, POOL_FIELDS, final_rows)

    report_path = Path(args.report).resolve() if args.report else output_path.with_name(output_path.stem + "_report.json")
    report = {
        "generated_at": datetime.now().isoformat(),
        "runtime_seconds": round(time.time() - start_ts, 2),
        "config": {
            "city": args.city,
            "max_runtime_minutes": args.max_runtime_minutes,
            "max_search_domains": args.max_search_domains,
            "max_jobs_news_domains": args.max_jobs_news_domains,
            "max_li_safe_domains": args.max_li_safe_domains,
            "max_enrichment_rows": args.max_enrichment_rows,
            "li_safe_discovery_enabled": args.enable_li_safe_discovery,
            "domain_required": not args.allow_missing_domain,
            "domain_enrichment_enabled": not args.disable_domain_enrichment,
            "search_discovery_enabled": not args.disable_search_discovery,
            "jobs_news_discovery_enabled": not args.disable_jobs_news_discovery,
        },
        "counts": {
            "input_rows": len(input_rows),
            "enriched_rows": len(enriched_rows),
            "discovery_rows": len(discovery_rows),
            "merged_rows": len(merged_rows),
            "deduped_rows": deduped_count,
            "final_rows": len(final_rows),
        },
        "by_source_input": dict(sorted(source_input_counts.items())),
        "by_source_output": dict(sorted(source_output_counts.items())),
        "rejections": dict(sorted(rejected.items())),
        "state_filter_enabled": bool(args.exclude_state_dir),
        "seen_company_keys_loaded": len(seen_company_keys),
        "output_csv": str(output_path),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)

    print(f"Pool build complete: rows={len(final_rows)} output={output_path}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
