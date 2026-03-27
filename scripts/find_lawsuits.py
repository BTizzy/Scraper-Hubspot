"""find_lawsuits.py

Detect active litigation for companies using a two-tier strategy:
  1. CourtListener v4 Dockets API (authenticated — requires free API token)
  2. DuckDuckGo search dork fallback (unauthenticated)

Input: companies_enriched.csv with `company_name` column.
Output: companies_lawsuits.csv with added columns: lawsuits_found (TRUE/FALSE), lawsuits_count, lawsuits_sample

CourtListener API docs: https://www.courtlistener.com/help/api/rest/
Free API token: https://www.courtlistener.com/sign-in/
"""
import argparse
import csv
import os
import re
import requests
from datetime import UTC, datetime, timedelta
import time
import urllib.parse

from bs4 import BeautifulSoup

from local_secrets import load_local_env

load_local_env()

# ── v4 Search API (authenticated) ──────────────────────────────────────────────
# CourtListener v4 search endpoint for finding dockets by company name.
# The /dockets/ endpoint is a detail endpoint (requires docket ID); use /search/ for name queries.
CL_SEARCH = 'https://www.courtlistener.com/api/rest/v4/search/'

# ── DuckDuckGo fallback ───────────────────────────────────────────────────────
DDG_HTML = 'https://html.duckduckgo.com/html/'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}

# Lawsuit-related terms for snippet evidence scoring
LAWSUIT_EVIDENCE_TERMS = re.compile(
    r'\b(lawsuit|litigation|defendant|plaintiff|sued|v\.\s|civil action|complaint|'
    r'court\s?order|settlement|injunction|docket)\b',
    re.IGNORECASE,
)

RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def get_with_retry(url, *, params=None, headers=None, timeout=12, retries=2, backoff_seconds=0.8):
    """Perform GET with exponential backoff for transient network/API failures."""
    last_error = ''
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code in RETRYABLE_HTTP_CODES and attempt < retries:
                time.sleep(backoff_seconds * (2 ** attempt))
                continue
            return resp, ''
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(backoff_seconds * (2 ** attempt))
                continue
            return None, last_error
    return None, last_error


def _get_api_token():
    """Read CourtListener API token from env or config."""
    return os.environ.get('COURTLISTENER_API_KEY', '').strip()


def _parse_iso_datetime(date_str):
    """Parse common ISO-like date strings from API payloads."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(str(date_str).replace('Z', '+00:00'))
    except Exception:
        return None


def query_courtlistener_v4(name, since_date):
    """Primary: CourtListener v4 Search API with token auth for company-name lawsuit lookup."""
    token = _get_api_token()
    if not token:
        return {
            'results': [],
            'status': 'no_api_key',
            'http_status': '',
            'error': 'COURTLISTENER_API_KEY not set — skipping v4 API',
        }

    # Use the v4 Search endpoint with exact-phrase company-name search.
    # The search endpoint returns opinions, clusters, and dockets matching the query.
    params = {
        'q': f'"{name}"',  # exact phrase search for company name
    }
    headers = {'Authorization': f'Token {token}'}
    try:
        r, req_error = get_with_retry(
            CL_SEARCH,
            params=params,
            headers=headers,
            timeout=15,
            retries=2,
            backoff_seconds=1.0,
        )
        if r is None:
            return {
                'results': [],
                'status': 'request_error',
                'http_status': '',
                'error': req_error or 'request failed after retries',
            }
        http_status = r.status_code
        if http_status == 429:
            return {
                'results': [],
                'status': 'rate_limited',
                'http_status': http_status,
                'error': 'HTTP 429 rate limited',
            }
        if http_status in (401, 403):
            return {
                'results': [],
                'status': 'auth_blocked',
                'http_status': http_status,
                'error': f'HTTP {http_status} auth/access blocked',
            }
        if http_status >= 400:
            # Capture response body for diagnostics
            try:
                error_detail = r.json().get('detail', r.text[:200])
            except Exception:
                error_detail = r.text[:200]
            return {
                'results': [],
                'status': 'http_error',
                'http_status': http_status,
                'error': f'HTTP {http_status}: {error_detail}',
            }

        data = r.json()
        results = data.get('results', [])
        # Post-filter results: keep only docket-related matches (caseName, docket_number present)
        # and respect the date filter
        filtered = []
        for item in results:
            # Check if this looks like a docket/case (not just a general document)
            case_name = item.get('caseName', '')
            docket_num = item.get('docketNumber', '')
            date_filed = item.get('dateFiled', '')
            
            # Include if it has a case name and was filed within the window
            if case_name and date_filed and date_filed >= since_date.strftime('%Y-%m-%d'):
                filtered.append({
                    'case_name': case_name,
                    'date_filed': date_filed,
                    'docket_number': docket_num or '',
                    'court_id': item.get('court_id', ''),
                    'absolute_url': item.get('docket_absolute_url', '') or item.get('absolute_url', ''),
                })
        
        return {
            'results': filtered,
            'status': 'ok' if filtered else 'ok_no_match',
            'http_status': http_status,
            'error': '',
        }
    except requests.exceptions.Timeout:
        return {
            'results': [],
            'status': 'timeout',
            'http_status': '',
            'error': 'request timeout',
        }
    except requests.exceptions.RequestException as e:
        return {
            'results': [],
            'status': 'request_error',
            'http_status': '',
            'error': str(e),
        }
    except ValueError as e:
        return {
            'results': [],
            'status': 'parse_error',
            'http_status': '',
            'error': str(e),
        }
    except Exception as e:
        return {
            'results': [],
            'status': 'unknown_error',
            'http_status': '',
            'error': str(e),
        }


def query_duckduckgo_lawsuits(name):
    """Fallback: DuckDuckGo dork for lawsuit evidence when API unavailable."""
    query = f'site:courtlistener.com "{name}"'
    try:
        r, req_error = get_with_retry(
            DDG_HTML,
            params={'q': query},
            headers=HEADERS,
            timeout=12,
            retries=2,
            backoff_seconds=0.8,
        )
        if r is None:
            return {
                'results': [],
                'status': 'request_error',
                'http_status': '',
                'error': req_error or 'DDG request failed after retries',
            }
        if r.status_code >= 400:
            return {
                'results': [],
                'status': 'http_error',
                'http_status': r.status_code,
                'error': f'DDG HTTP {r.status_code}',
            }

        soup = BeautifulSoup(r.text, 'html.parser')
        results = []
        for link in soup.select('a.result__a'):
            href = link.get('href', '')
            title = link.get_text(strip=True)
            if 'courtlistener.com' in href or LAWSUIT_EVIDENCE_TERMS.search(title):
                results.append({
                    'case_name': title,
                    'absolute_url': href,
                    'date_filed': '',
                    'docket_number': '',
                    'court_id': '',
                })
            if len(results) >= 5:
                break

        # Also scan broader results for lawsuit evidence
        if not results:
            for snippet in soup.select('.result__snippet'):
                text = snippet.get_text(' ', strip=True)
                if LAWSUIT_EVIDENCE_TERMS.search(text):
                    parent_link = snippet.find_parent('div')
                    title_el = parent_link.select_one('a.result__a') if parent_link else None
                    results.append({
                        'case_name': title_el.get_text(strip=True) if title_el else text[:100],
                        'absolute_url': title_el.get('href', '') if title_el else '',
                        'date_filed': '',
                        'docket_number': '',
                        'court_id': '',
                    })
                    if len(results) >= 5:
                        break

        status = 'ok' if results else 'ok_no_match'
        return {
            'results': results,
            'status': status,
            'http_status': r.status_code,
            'error': '',
        }
    except requests.exceptions.Timeout:
        return {
            'results': [],
            'status': 'timeout',
            'http_status': '',
            'error': 'DDG request timeout',
        }
    except requests.exceptions.RequestException as e:
        return {
            'results': [],
            'status': 'request_error',
            'http_status': '',
            'error': str(e),
        }
    except Exception as e:
        return {
            'results': [],
            'status': 'unknown_error',
            'http_status': '',
            'error': str(e),
        }


def query_courtlistener(name, since_date):
    """Two-tier lawsuit lookup: v4 API (if key set) → DuckDuckGo fallback."""
    # Tier 1: Authenticated v4 API
    result = query_courtlistener_v4(name, since_date)
    if result['status'] == 'ok':
        return result
    tier1_status = result['status']

    # Tier 2: DuckDuckGo dork fallback
    if tier1_status in ('no_api_key', 'auth_blocked', 'rate_limited'):
        ddg_result = query_duckduckgo_lawsuits(name)
        if ddg_result['results']:
            ddg_result['status'] = 'ok_ddg_fallback'
            return ddg_result
        # Preserve the DDG status but note the fallback was tried
        if ddg_result['status'] in ('ok_no_match',):
            ddg_result['error'] = f'v4={tier1_status}; DDG found no matches'
            ddg_result['status'] = 'ok_no_match'
            return ddg_result
        # DDG also failed — return the richer error
        ddg_result['error'] = f'v4={tier1_status}; DDG={ddg_result["status"]}: {ddg_result["error"]}'
        ddg_result['status'] = 'both_failed'
        return ddg_result

    # Tier 1 failed for non-auth reasons (timeout, parse error, etc.) — still try DDG
    ddg_result = query_duckduckgo_lawsuits(name)
    if ddg_result['results']:
        ddg_result['status'] = 'ok_ddg_fallback'
        ddg_result['error'] = f'v4={tier1_status}; fell back to DDG'
        return ddg_result

    # Both failed
    return result

def find(input_csv, output_csv):
    since = datetime.now(UTC) - timedelta(days=365)
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = list(reader.fieldnames)
        for col in ['lawsuits_found', 'lawsuits_count', 'lawsuits_sample',
                    'lawsuits_query_status', 'lawsuits_http_status', 'lawsuits_error']:
            if col not in fieldnames:
                fieldnames.append(col)
        # Ensure signal_tag column exists
        if 'signal_tag' not in fieldnames:
            fieldnames.append('signal_tag')
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        found_count = 0
        signal_appends = 0
        status_counts = {}
        api_error_rows = 0
        for row in reader:
            name = row.get('company_name') or row.get('Company') or row.get('name')
            if not name:
                row['lawsuits_found'] = 'FALSE'
                row['lawsuits_count'] = 0
                row['lawsuits_sample'] = ''
                row['lawsuits_query_status'] = 'missing_company_name'
                row['lawsuits_http_status'] = ''
                row['lawsuits_error'] = 'missing company name'
                writer.writerow(row)
                continue

            query_result = query_courtlistener(name, since)
            query_status = query_result.get('status', 'unknown_error')
            http_status = query_result.get('http_status', '')
            query_error = query_result.get('error', '')
            results = query_result.get('results', [])

            status_counts[query_status] = status_counts.get(query_status, 0) + 1
            if query_status != 'ok':
                api_error_rows += 1
                print(f"  ⚠ {name}: lawsuit lookup status={query_status} http={http_status or '-'} {query_error}")

            count = len(results)
            row['lawsuits_found'] = 'TRUE' if count > 0 else 'FALSE'
            row['lawsuits_count'] = count
            row['lawsuits_query_status'] = query_status
            row['lawsuits_http_status'] = str(http_status) if http_status != '' else ''
            row['lawsuits_error'] = query_error

            sample = ''
            if results:
                found_count += 1
                # store a short sample of titles/urls
                sample_items = []
                for item in results[:3]:
                    title = item.get('case_name') or item.get('title') or item.get('name') or ''
                    url = item.get('absolute_url') or item.get('url') or ''
                    sample_items.append(f"{title} | {url}")
                sample = ' || '.join(sample_items)
            row['lawsuits_sample'] = sample
            # ── Append signal tag ──
            existing_signals = [s.strip() for s in (row.get('signal_tag', '') or '').split(';') if s.strip()]
            if count > 0 and 'active_lawsuit' not in existing_signals:
                existing_signals.append('active_lawsuit')
                signal_appends += 1
                print(f"  🔔 {name}: {count} lawsuit(s) found")
            row['signal_tag'] = ';'.join(existing_signals)
            writer.writerow(row)
            time.sleep(0.5)
        print(f"\n  Lawsuits detected: {found_count} companies")
        print(f"  active_lawsuit appends: {signal_appends}")
        print(f"  lawsuit query statuses: {status_counts}")
        print(f"  lawsuit API/problem rows: {api_error_rows}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input','-i', required=True)
    parser.add_argument('--output','-o', required=True)
    args = parser.parse_args()
    find(args.input, args.output)

if __name__ == '__main__':
    main()
