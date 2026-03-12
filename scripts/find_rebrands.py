"""find_rebrands.py

Detect business changes (rebrands, transfers, sales, DBA filings) using a multi-tier strategy:
  1. Row-native alias/history columns from SOS or upstream registries
  2. Website keyword scan for business change evidence

Input: companies_enriched.csv (expects `company_name` and optionally `opencorp_url`)
Output: companies_rebrands.csv with columns: rebrand_flag, rebrand_reason, rebrand_sample
"""
import argparse
import csv
import re
import requests
from urllib.parse import urlparse, quote_plus
import time

from bs4 import BeautifulSoup

OPENCORP_SEARCH = "https://api.opencorporates.com/v0.4/companies/search?q={q}&jurisdiction_code=us_wa"

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}

KEYWORDS = [
    'formerly', 'formerly known as', 'rebrand', 'rebranded', 'now called',
    'previously known as', 'formerly called', 'dba', 'doing business as',
    'trade name', 'sold', 'acquired', 'under new management', 'merged',
    'transfer', 'new ownership', 'business sale',
]
ALIAS_COLUMN_PATTERN = re.compile(r'(previous|former|old|alternate|trade|dba).*(name)?', re.IGNORECASE)
LEGAL_SUFFIX_PATTERN = re.compile(r'\b(llc|inc|corp|corporation|co|company|ltd|limited|pllc|lp|llp|group|services|holdings)\b', re.IGNORECASE)


def _normalize_name(value):
    cleaned = LEGAL_SUFFIX_PATTERN.sub('', value or '')
    cleaned = re.sub(r'[^a-z0-9\s]', ' ', cleaned.lower())
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def extract_row_aliases(row, company_name):
    """Extract prior-name style aliases from any row columns that look like SOS/name-history fields."""
    aliases = []
    company_norm = _normalize_name(company_name)
    for key, value in row.items():
        if not value or not ALIAS_COLUMN_PATTERN.search(str(key)):
            continue
        raw_parts = re.split(r'[;|,/]', str(value))
        for part in raw_parts:
            alias = part.strip()
            if not alias:
                continue
            alias_norm = _normalize_name(alias)
            if alias_norm and alias_norm != company_norm and alias not in aliases:
                aliases.append(alias)
    return aliases


def fetch_url_with_http_fallback(url, timeout=8):
    """Try the given URL and fall back to http:// on TLS/SSL failures."""
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        return response, url, ''
    except requests.exceptions.SSLError as exc:
        if url.startswith('https://'):
            fallback_url = 'http://' + url[len('https://'):]
            try:
                response = requests.get(fallback_url, timeout=timeout, headers=HEADERS)
                return response, fallback_url, f'https failed: {exc}'
            except requests.exceptions.RequestException as fallback_exc:
                raise requests.exceptions.RequestException(f'https={exc}; http={fallback_exc}') from fallback_exc
        raise


def search_opencorp(name):
    try:
        q = requests.utils.requote_uri(name)
        url = OPENCORP_SEARCH.format(q=q)
        r = requests.get(url, timeout=10)
        http_status = r.status_code
        if http_status in (401, 403):
            return {
                'company': None,
                'status': 'auth_blocked',
                'http_status': http_status,
                'error': f'HTTP {http_status} auth/access blocked',
            }
        if http_status == 429:
            return {
                'company': None,
                'status': 'rate_limited',
                'http_status': http_status,
                'error': 'HTTP 429 rate limited',
            }
        if http_status >= 400:
            return {
                'company': None,
                'status': 'http_error',
                'http_status': http_status,
                'error': f'HTTP {http_status}',
            }

        data = r.json()
        results = data.get('results', {}).get('companies', [])
        if not results:
            return {
                'company': None,
                'status': 'ok_no_match',
                'http_status': http_status,
                'error': '',
            }
        return {
            'company': results[0].get('company'),
            'status': 'ok',
            'http_status': http_status,
            'error': '',
        }
    except requests.exceptions.Timeout:
        return {
            'company': None,
            'status': 'timeout',
            'http_status': '',
            'error': 'request timeout',
        }
    except requests.exceptions.RequestException as e:
        return {
            'company': None,
            'status': 'request_error',
            'http_status': '',
            'error': str(e),
        }
    except ValueError as e:
        return {
            'company': None,
            'status': 'parse_error',
            'http_status': '',
            'error': str(e),
        }
    except Exception as e:
        return {
            'company': None,
            'status': 'unknown_error',
            'http_status': '',
            'error': str(e),
        }


def scrape_opencorp_html(name):
    """Fallback: scrape OpenCorporates HTML search when API returns 401."""
    try:
        search_url = f"https://opencorporates.com/companies?q={quote_plus(name)}&jurisdiction_code=us_wa"
        r = requests.get(search_url, timeout=12, headers=HEADERS)
        if r.status_code != 200:
            return {
                'company': None,
                'status': 'html_http_error',
                'http_status': r.status_code,
                'error': f'HTML scrape HTTP {r.status_code}',
            }

        soup = BeautifulSoup(r.text, 'html.parser')
        # Find first result link to a WA company
        result_link = None
        for a in soup.find_all('a', href=True):
            if '/companies/us_wa/' in a['href']:
                result_link = a
                break
        if not result_link:
            return {
                'company': None,
                'status': 'html_no_match',
                'http_status': r.status_code,
                'error': '',
            }

        company_url = result_link['href']
        if not company_url.startswith('http'):
            company_url = 'https://opencorporates.com' + company_url

        company = {
            'name': result_link.get_text(strip=True),
            'opencorporates_url': company_url,
            'previous_names': [],
        }

        # Fetch company detail page to look for previous names
        try:
            dr = requests.get(company_url, timeout=12, headers=HEADERS)
            if dr.status_code == 200:
                detail_soup = BeautifulSoup(dr.text, 'html.parser')
                # Look for "Previous Names" or "Alternative Names" section
                for heading in detail_soup.find_all(['dt', 'h3', 'h4', 'th']):
                    text = heading.get_text(strip=True).lower()
                    if any(k in text for k in ['previous name', 'alternative name', 'former name']):
                        # Get the sibling dd/td or next element
                        sibling = heading.find_next_sibling(['dd', 'td', 'ul', 'div'])
                        if sibling:
                            for item in sibling.find_all(['li', 'span', 'a']):
                                pn = item.get_text(strip=True)
                                if pn and pn.lower() != company['name'].lower():
                                    company['previous_names'].append(pn)
                            if not company['previous_names']:
                                pn = sibling.get_text(strip=True)
                                if pn and pn.lower() != company['name'].lower():
                                    company['previous_names'].append(pn)
        except Exception:
            pass

        return {
            'company': company,
            'status': 'ok_html',
            'http_status': r.status_code,
            'error': '',
        }
    except requests.exceptions.Timeout:
        return {
            'company': None,
            'status': 'html_timeout',
            'http_status': '',
            'error': 'HTML scrape timeout',
        }
    except requests.exceptions.RequestException as e:
        return {
            'company': None,
            'status': 'html_request_error',
            'http_status': '',
            'error': str(e),
        }
    except Exception as e:
        return {
            'company': None,
            'status': 'html_unknown_error',
            'http_status': '',
            'error': str(e),
        }


def search_opencorp_with_fallback(name):
    """Two-tier OpenCorporates lookup: API → HTML scraper fallback."""
    result = search_opencorp(name)
    if result['status'] in ('ok', 'ok_no_match'):
        return result

    # API failed (401/403/rate_limited) — try HTML scrape
    tier1_status = result['status']
    if tier1_status in ('auth_blocked', 'rate_limited', 'http_error'):
        html_result = scrape_opencorp_html(name)
        if html_result['company']:
            html_result['error'] = f'API={tier1_status}; used HTML fallback'
            return html_result
        if html_result['status'] == 'html_no_match':
            return {
                'company': None,
                'status': 'ok_no_match',
                'http_status': html_result['http_status'],
                'error': f'API={tier1_status}; HTML found no match',
            }
        # HTML also failed
        return {
            'company': None,
            'status': 'both_failed',
            'http_status': '',
            'error': f'API={tier1_status}; HTML={html_result["status"]}: {html_result["error"]}',
        }

    return result


def scan_website_for_keywords(domain):
    if not domain:
        return {
            'found': None,
            'snippet': '',
            'status': 'no_domain',
            'http_status': '',
            'error': 'no domain available',
        }
    if not domain.startswith('http'):
        domain = 'https://' + domain
    try:
        r, resolved_url, fallback_note = fetch_url_with_http_fallback(domain, timeout=8)
        if r.status_code != 200:
            return {
                'found': None,
                'snippet': '',
                'status': 'http_error',
                'http_status': r.status_code,
                'error': f'HTTP {r.status_code}' + (f' | {fallback_note}' if fallback_note else ''),
            }
        text = r.text.lower()
        
        # Prioritize strong business change signals
        strong_signals = [
            'rebranded to', 'formerly known as', 'trading as',
            'acquired by', 'merged with', 'under new management',
            'new ownership', 'business sold', 'business sale',
        ]
        for signal in strong_signals:
            pattern = r'\b' + re.escape(signal) + r'\b'
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                idx = match.start()
                # Extract full sentence context
                start = text.rfind('.', max(0, idx - 200), idx)
                start = start + 2 if start >= 0 else max(0, idx - 100)
                end = text.find('.', idx + len(signal))
                end = end if end > 0 else min(len(text), idx + len(signal) + 150)
                snippet = text[start:end].strip()
                return {
                    'found': True,
                    'snippet': f'{resolved_url} | {snippet}',
                    'status': 'match_strong_signal',
                    'http_status': r.status_code,
                    'error': fallback_note,
                }
        
        # Weak signals: "doing business as", "dba" — require strong context to avoid legal boilerplate false positives
        weak_signals = ['doing business as', ' dba ']
        for kw in weak_signals:
            # Only match if not in footer/legal disclaimer context (heuristic: avoid text after "terms" or "legal")
            safe_text = text[:text.find('terms of service')] if 'terms of service' in text else text
            safe_text = safe_text[:safe_text.find('legal disclaimer')] if 'legal disclaimer' in safe_text else safe_text
            
            pattern = r'\b' + re.escape(kw.strip()) + r'\b'
            match = re.search(pattern, safe_text, re.IGNORECASE)
            if match and len(safe_text) > match.start():  # Verify we found it in safe_text, not excluded section
                idx = match.start()
                # Extract sentence context
                start = safe_text.rfind('.', max(0, idx - 200), idx)
                start = start + 2 if start >= 0 else max(0, idx - 100)
                end = safe_text.find('.', idx)
                end = end if end > 0 else min(len(safe_text), idx + 150)
                snippet = safe_text[start:end].strip()
                # If context includes "may be", "such as", "or", "including" → likely boilerplate
                if any(phrase in snippet.lower() for phrase in ['may be', 'such as', 'or ', 'including ', 'entities']):
                    continue  # Skip this match, too weak
                return {
                    'found': True,
                    'snippet': f'{resolved_url} | {snippet}',
                    'status': 'match_weak_signal',
                    'http_status': r.status_code,
                    'error': fallback_note,
                }
        
        return {
            'found': False,
            'snippet': '',
            'status': 'ok_no_keyword',
            'http_status': r.status_code,
            'error': fallback_note,
        }
    except requests.exceptions.Timeout:
        return {
            'found': None,
            'snippet': '',
            'status': 'timeout',
            'http_status': '',
            'error': 'request timeout',
        }
    except requests.exceptions.RequestException as e:
        return {
            'found': None,
            'snippet': '',
            'status': 'request_error',
            'http_status': '',
            'error': str(e),
        }
    except Exception as e:
        return {
            'found': None,
            'snippet': '',
            'status': 'unknown_error',
            'http_status': '',
            'error': str(e),
        }

def find(input_csv, output_csv):
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = list(reader.fieldnames)
        for col in ['rebrand_flag', 'rebrand_reason', 'rebrand_sample',
                    'rebrand_query_status', 'rebrand_http_status',
                    'rebrand_scan_status', 'rebrand_error']:
            if col not in fieldnames:
                fieldnames.append(col)
        # Ensure signal_tag column exists
        if 'signal_tag' not in fieldnames:
            fieldnames.append('signal_tag')
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        rebrand_count = 0
        signal_appends = 0
        query_status_counts = {}
        scan_status_counts = {}
        api_error_rows = 0
        for row in reader:
            name = row.get('company_name') or row.get('Company') or row.get('name')
            rebrand_flag = 'FALSE'
            reason = ''
            sample = ''
            row_errors = []

            # Primary source: row-native alias/history columns from SOS or upstream registries.
            row_aliases = extract_row_aliases(row, name)
            query_status = 'row_history_found' if row_aliases else 'no_row_history'
            query_http_status = ''
            query_error = ''
            query_status_counts[query_status] = query_status_counts.get(query_status, 0) + 1
            if row_aliases:
                rebrand_flag = 'TRUE'
                reason = 'row name-history fields'
                sample = '; '.join([str(x) for x in row_aliases[:3]])

            # Scan homepage for textual signals
            website = row.get('website') or row.get('domain') or ''
            domain = website
            if website and website.startswith('http'):
                p = urlparse(website)
                domain = f"{p.scheme}://{p.netloc}"
            scan_result = scan_website_for_keywords(domain)
            found = scan_result.get('found')
            snippet = scan_result.get('snippet', '')
            scan_status = scan_result.get('status', 'unknown_error')
            scan_http_status = scan_result.get('http_status', '')
            scan_error = scan_result.get('error', '')
            scan_status_counts[scan_status] = scan_status_counts.get(scan_status, 0) + 1

            if scan_status not in ('match', 'ok_no_keyword', 'no_domain'):
                row_errors.append(f"website:{scan_status}:{scan_error}")
                print(f"  ⚠ {name}: website scan status={scan_status} http={scan_http_status or '-'} {scan_error}")

            if found:
                rebrand_flag = 'TRUE'
                if reason:
                    reason += ' + website copy'
                else:
                    reason = 'website copy'
                sample = (sample + ' || ' + snippet) if sample else snippet

            row['rebrand_flag'] = rebrand_flag
            row['rebrand_reason'] = reason
            row['rebrand_sample'] = sample
            row['rebrand_query_status'] = query_status
            row['rebrand_http_status'] = str(query_http_status) if query_http_status != '' else ''
            row['rebrand_scan_status'] = scan_status
            row['rebrand_error'] = ' | '.join(row_errors) if row_errors else ''

            if row_errors:
                api_error_rows += 1

            # ── Append signal tag ──
            existing_signals = [s.strip() for s in (row.get('signal_tag', '') or '').split(';') if s.strip()]
            if rebrand_flag == 'TRUE' and 'business_change' not in existing_signals:
                existing_signals.append('business_change')
                rebrand_count += 1
                signal_appends += 1
                print(f"  🔔 {name}: rebrand detected — {reason}")
            row['signal_tag'] = ';'.join(existing_signals)

            writer.writerow(row)
            time.sleep(0.5)
        print(f"\n  Rebrands detected: {rebrand_count} companies")
        print(f"  rebrand appends: {signal_appends}")
        print(f"  rebrand query statuses: {query_status_counts}")
        print(f"  Website scan statuses: {scan_status_counts}")
        print(f"  rebrand API/problem rows: {api_error_rows}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input','-i', required=True)
    parser.add_argument('--output','-o', required=True)
    args = parser.parse_args()
    find(args.input, args.output)

if __name__ == '__main__':
    main()
