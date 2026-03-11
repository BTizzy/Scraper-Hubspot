"""find_active_hiring.py

Detect active hiring signals using company careers pages and job-board search.

Input: companies_rebrands.csv with `company_name` and optional `website`/`domain`.
Output: companies_hiring.csv with columns: hiring_flag, hiring_reason, hiring_sample,
        hiring_query_status, hiring_http_status, hiring_error
"""
import argparse
import csv
import time
from urllib.parse import urljoin, urlparse

import requests

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
SEARCH_PATHS = ['/careers', '/jobs', '/join-us', '/join-our-team', '/work-with-us']
JOB_KEYWORDS = ['open positions', 'we are hiring', 'join our team', 'careers', 'job openings', 'apply now']


def normalize_domain(website):
    if not website:
        return ''
    if website.startswith('http'):
        parsed = urlparse(website)
        return f"{parsed.scheme}://{parsed.netloc}"
    return f"https://{website}"


def fetch_with_http_fallback(url):
    try:
        response = requests.get(url, timeout=8, headers=HEADERS)
        return response, url, ''
    except requests.exceptions.SSLError as exc:
        if url.startswith('https://'):
            fallback_url = 'http://' + url[len('https://'):]
            response = requests.get(fallback_url, timeout=8, headers=HEADERS)
            return response, fallback_url, f'https failed: {exc}'
        raise


def scan_careers_pages(domain):
    if not domain:
        return {
            'found': None,
            'sample': '',
            'status': 'no_domain',
            'http_status': '',
            'error': 'no domain available',
        }
    try:
        for path in SEARCH_PATHS:
            url = urljoin(domain, path)
            r, resolved_url, fallback_note = fetch_with_http_fallback(url)
            if r.status_code != 200:
                continue
            text = r.text.lower()
            for kw in JOB_KEYWORDS:
                if kw in text:
                    idx = text.find(kw)
                    snippet = text[max(0, idx - 80):idx + 120]
                    return {
                        'found': True,
                        'sample': f'{resolved_url} | {snippet}',
                        'status': 'match',
                        'http_status': r.status_code,
                        'error': fallback_note,
                    }
        return {
            'found': False,
            'sample': '',
            'status': 'ok_no_match',
            'http_status': 200,
            'error': '',
        }
    except requests.exceptions.Timeout:
        return {
            'found': None,
            'sample': '',
            'status': 'timeout',
            'http_status': '',
            'error': 'request timeout',
        }
    except requests.exceptions.RequestException as e:
        return {
            'found': None,
            'sample': '',
            'status': 'request_error',
            'http_status': '',
            'error': str(e),
        }
    except Exception as e:
        return {
            'found': None,
            'sample': '',
            'status': 'unknown_error',
            'http_status': '',
            'error': str(e),
        }


def find(input_csv, output_csv):
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = list(reader.fieldnames)
        for col in [
            'hiring_flag', 'hiring_reason', 'hiring_sample',
            'hiring_query_status', 'hiring_http_status', 'hiring_error',
        ]:
            if col not in fieldnames:
                fieldnames.append(col)
        if 'signal_tag' not in fieldnames:
            fieldnames.append('signal_tag')
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        detected = 0
        for row in reader:
            website = row.get('website') or row.get('domain') or ''
            domain = normalize_domain(website)
            result = scan_careers_pages(domain)

            row['hiring_flag'] = 'TRUE' if result.get('found') else 'FALSE'
            row['hiring_reason'] = 'careers page/job posting' if result.get('found') else ''
            row['hiring_sample'] = result.get('sample', '')
            row['hiring_query_status'] = result.get('status', '')
            row['hiring_http_status'] = str(result.get('http_status', '')) if result.get('http_status', '') != '' else ''
            row['hiring_error'] = result.get('error', '')

            existing_signals = [s.strip() for s in (row.get('signal_tag', '') or '').split(';') if s.strip()]
            if result.get('found') and 'active_hiring' not in existing_signals:
                existing_signals.append('active_hiring')
                detected += 1
                print(f"  🔔 {row.get('company_name')}: active hiring detected")
            row['signal_tag'] = ';'.join(existing_signals)
            writer.writerow(row)
            time.sleep(0.25)

        print(f"\n  Active hiring detected: {detected} companies")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', '-i', required=True)
    parser.add_argument('--output', '-o', required=True)
    args = parser.parse_args()
    find(args.input, args.output)


if __name__ == '__main__':
    main()