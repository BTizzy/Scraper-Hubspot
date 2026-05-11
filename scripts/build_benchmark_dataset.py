"""build_benchmark_dataset.py

Curate a realistic benchmark dataset by keeping only company rows whose domains
produce direct first-party contact evidence in hosted-safe sources.

Usage:
    python build_benchmark_dataset.py --input candidates.csv --output test_data/sos_direct_evidence.csv
    python build_benchmark_dataset.py --query "seattle staffing agency" --output test_data/sos_realistic_direct.csv
"""
import argparse
import csv
import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import quote_plus, urlparse

import requests

from waterfall_enricher import (
    source_team_page,
    source_sitewide_scan,
    source_sitemap_recent,
    source_wayback_archive,
)


DIRECT_SOURCE_FUNCS = [
    ('team_page', source_team_page),
    ('site_scan', source_sitewide_scan),
    ('sitemap_recent', source_sitemap_recent),
    ('wayback_archive', source_wayback_archive),
]

DEFAULT_OUTPUT_FIELDS = [
    'company_name',
    'registered_date',
    'ubi_number',
    'entity_type',
    'registered_agent',
    'governor_1',
    'governor_1_title',
    'governor_2',
    'governor_2_title',
    'status',
    'principal_office',
    'website',
    'domain',
    'signal_tag',
    'collected_date',
]

EXCLUDED_DOMAIN_SUBSTRINGS = (
    'bing.com',
    'yelp.',
    'mapquest.',
    'facebook.com',
    'linkedin.com',
    'instagram.com',
    'youtube.com',
    'x.com',
    'twitter.com',
    'manta.com',
    'opencorporates.com',
    'expertise.com',
    'findlaw.com',
    'justia.com',
    'tripadvisor.com',
    'chamberofcommerce.com',
    'thumbtack.com',
    'clutch.co',
)

EXCLUDED_TITLE_SUBSTRINGS = (
    'best ',
    'top ',
    'near me',
    'directory',
    'reviews',
    'visit ',
    'things to do',
)


def normalize_domain(row: dict) -> str:
    domain = (row.get('website') or row.get('domain') or '').strip()
    if not domain:
        return ''
    if domain.startswith('http://') or domain.startswith('https://'):
        domain = urlparse(domain).netloc
    return domain.lower().lstrip('www.')


def slug_to_company_name(domain: str) -> str:
    root = domain.split('.')[0]
    tokens = [token for token in re.split(r'[^a-z0-9]+', root.lower()) if token]
    if not tokens:
        return domain
    return ' '.join(token.capitalize() for token in tokens)


def parse_bing_rss_candidates(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    candidates = []
    for item in root.findall('./channel/item'):
        link = (item.findtext('link') or '').strip()
        title = (item.findtext('title') or '').strip()
        domain = normalize_domain({'website': link})
        if not domain:
            continue
        if any(marker in domain for marker in EXCLUDED_DOMAIN_SUBSTRINGS):
            continue
        lowered_title = title.lower()
        if any(marker in lowered_title for marker in EXCLUDED_TITLE_SUBSTRINGS):
            continue
        candidates.append({
            'title': title,
            'website': f'https://{domain}',
            'domain': domain,
        })
    return candidates


def build_candidate_row(candidate: dict, query: str) -> dict:
    title = (candidate.get('title') or '').strip()
    company_name = re.split(r'\s+[\-|:]\s+', title, maxsplit=1)[0].strip() or slug_to_company_name(candidate['domain'])
    return {
        'company_name': company_name,
        'registered_date': '',
        'ubi_number': '',
        'entity_type': '',
        'registered_agent': '',
        'governor_1': '',
        'governor_1_title': '',
        'governor_2': '',
        'governor_2_title': '',
        'status': 'Active',
        'principal_office': '',
        'website': candidate['website'],
        'domain': candidate['domain'],
        'signal_tag': 'business_change',
        'collected_date': date.today().isoformat(),
        'candidate_query': query,
    }


def load_queries(args: argparse.Namespace) -> list[str]:
    queries = [query.strip() for query in (args.query or []) if query and query.strip()]
    if args.query_file:
        with open(args.query_file, encoding='utf-8') as fin:
            for line in fin:
                query = line.strip()
                if query and not query.startswith('#'):
                    queries.append(query)
    seen = set()
    deduped = []
    for query in queries:
        lowered = query.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(query)
    return deduped


def discover_candidate_rows(queries: list[str], max_candidates: int = 100, per_query: int = 10) -> list[dict]:
    rows = []
    seen_domains = set()
    headers = {'User-Agent': 'Mozilla/5.0'}

    for query in queries:
        if len(rows) >= max_candidates:
            break
        url = f'https://www.bing.com/search?format=rss&q={quote_plus(query)}'
        try:
            response = requests.get(url, timeout=20, headers=headers)
            response.raise_for_status()
        except requests.RequestException:
            continue

        kept_for_query = 0
        for candidate in parse_bing_rss_candidates(response.text):
            domain = candidate['domain']
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            rows.append(build_candidate_row(candidate, query))
            kept_for_query += 1
            if len(rows) >= max_candidates or kept_for_query >= max(1, per_query):
                break

    return rows


def load_input_rows(path: str) -> tuple[list[dict], list[str]]:
    with open(path, newline='', encoding='utf-8') as fin:
        reader = csv.DictReader(fin)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def collect_direct_evidence(domain: str) -> tuple[list[str], list[dict]]:
    sources = []
    contacts = []
    seen_emails = set()

    for source_name, source_func in DIRECT_SOURCE_FUNCS:
        try:
            found = source_func(domain)
        except Exception:
            found = []
        direct_found = []
        for contact in found:
            email = (contact.get('email') or '').strip().lower()
            if not email or email in seen_emails:
                continue
            seen_emails.add(email)
            direct_found.append(contact)
        if direct_found:
            sources.append(source_name)
            contacts.extend(direct_found)

    return sources, contacts


def curate_dataset(rows: list[dict], target_rows: int = 25) -> list[dict]:
    curated = []
    seen_domains = set()
    for row in rows:
        if len(curated) >= target_rows:
            break
        domain = normalize_domain(row)
        if not domain or domain in seen_domains:
            continue
        sources, contacts = collect_direct_evidence(domain)
        if not contacts:
            continue

        enriched = dict(row)
        enriched['website'] = row.get('website') or f'https://{domain}'
        enriched['domain'] = domain
        enriched['benchmark_direct_sources'] = ';'.join(sources)
        enriched['benchmark_direct_email_count'] = str(len(contacts))
        enriched['benchmark_sample_email'] = contacts[0].get('email', '')
        curated.append(enriched)
        seen_domains.add(domain)
        print(f"kept {domain}: {enriched['benchmark_direct_sources']} ({enriched['benchmark_direct_email_count']} emails)")
    return curated


def main() -> int:
    parser = argparse.ArgumentParser(description='Build a benchmark dataset from rows with direct-evidence domains')
    parser.add_argument('--input', help='Input CSV of company rows')
    parser.add_argument('--output', required=True, help='Output CSV path')
    parser.add_argument('--target-rows', type=int, default=25, help='Maximum benchmark rows to keep')
    parser.add_argument('--query', action='append', help='Bing RSS query used to discover candidate company websites')
    parser.add_argument('--query-file', help='File containing one Bing RSS discovery query per line')
    parser.add_argument('--max-candidates', type=int, default=150, help='Maximum candidate websites to gather before curation')
    parser.add_argument('--per-query', type=int, default=10, help='Maximum candidate websites to keep from each query')
    args = parser.parse_args()

    rows = []
    fieldnames = []
    if args.input:
        input_rows, fieldnames = load_input_rows(args.input)
        rows.extend(input_rows)

    queries = load_queries(args)
    if queries:
        rows.extend(discover_candidate_rows(queries, max_candidates=max(1, args.max_candidates), per_query=max(1, args.per_query)))

    if not rows:
        parser.error('provide --input and/or at least one --query/--query-file source')

    curated = curate_dataset(rows, target_rows=max(1, args.target_rows))
    extra_fields = ['benchmark_direct_sources', 'benchmark_direct_email_count', 'benchmark_sample_email']
    output_fields = list(DEFAULT_OUTPUT_FIELDS)
    for field in fieldnames:
        if field not in output_fields:
            output_fields.append(field)
    for field in ['candidate_query'] + extra_fields:
        if field not in output_fields:
            output_fields.append(field)

    with open(args.output, 'w', newline='', encoding='utf-8') as fout:
        writer = csv.DictWriter(fout, fieldnames=output_fields, extrasaction='ignore')
        writer.writeheader()
        for row in curated:
            writer.writerow(row)

    summary = {
        'input_rows': len(rows),
        'discovery_queries': len(queries),
        'curated_rows': len(curated),
        'output': args.output,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())