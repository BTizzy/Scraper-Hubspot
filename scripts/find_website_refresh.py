"""find_website_refresh.py

Detect website refresh signals using WHOIS update dates and HTTP Last-Modified.

Input: companies_hiring.csv with `domain` or `website`.
Output: companies_refresh.csv with columns: website_refresh_flag, website_refresh_reason,
        website_refresh_sample, website_refresh_status, website_refresh_error
"""
import argparse
import csv
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import requests
import whois

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
REFRESH_WINDOW_DAYS = 180


def normalize_domain(website):
    if not website:
        return ''
    if website.startswith('http'):
        return urlparse(website).netloc
    return website


def _pick_latest_date(value):
    if isinstance(value, list):
        values = [v for v in value if isinstance(v, datetime)]
        normalized = []
        for item in values:
            if item.tzinfo is not None:
                item = item.astimezone(UTC).replace(tzinfo=None)
            normalized.append(item)
        values = normalized
        return max(values) if values else None
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value if isinstance(value, datetime) else None


def detect_refresh(domain):
    if not domain:
        return {
            'found': None,
            'reason': '',
            'sample': '',
            'status': 'no_domain',
            'error': 'no domain available',
        }

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=REFRESH_WINDOW_DAYS)
    try:
        whois_data = whois.whois(domain)
        updated_date = _pick_latest_date(whois_data.updated_date)
        if updated_date and updated_date >= cutoff:
            return {
                'found': True,
                'reason': 'recent WHOIS update',
                'sample': updated_date.isoformat(),
                'status': 'match_whois',
                'error': '',
            }
    except Exception as e:
        whois_error = str(e)
    else:
        whois_error = ''

    try:
        response = requests.head(f'https://{domain}', timeout=8, headers=HEADERS, allow_redirects=True)
        last_modified = response.headers.get('Last-Modified', '')
        if last_modified:
            parsed = None
            for fmt in ('%a, %d %b %Y %H:%M:%S %Z', '%a, %d %b %Y %H:%M:%S GMT'):
                try:
                    parsed = datetime.strptime(last_modified, fmt)
                    if parsed.tzinfo is not None:
                        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
                    break
                except ValueError:
                    continue
            if parsed and parsed >= cutoff:
                return {
                    'found': True,
                    'reason': 'recent HTTP Last-Modified',
                    'sample': last_modified,
                    'status': 'match_http',
                    'error': whois_error,
                }
        return {
            'found': False,
            'reason': '',
            'sample': '',
            'status': 'ok_no_match',
            'error': whois_error,
        }
    except requests.exceptions.RequestException as e:
        return {
            'found': None,
            'reason': '',
            'sample': '',
            'status': 'request_error',
            'error': f'{whois_error} | {e}'.strip(' |'),
        }


def find(input_csv, output_csv):
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = list(reader.fieldnames)
        for col in [
            'website_refresh_flag', 'website_refresh_reason', 'website_refresh_sample',
            'website_refresh_status', 'website_refresh_error',
        ]:
            if col not in fieldnames:
                fieldnames.append(col)
        if 'signal_tag' not in fieldnames:
            fieldnames.append('signal_tag')
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        detected = 0
        for row in reader:
            domain = normalize_domain(row.get('domain') or row.get('website') or '')
            result = detect_refresh(domain)
            row['website_refresh_flag'] = 'TRUE' if result.get('found') else 'FALSE'
            row['website_refresh_reason'] = result.get('reason', '')
            row['website_refresh_sample'] = result.get('sample', '')
            row['website_refresh_status'] = result.get('status', '')
            row['website_refresh_error'] = result.get('error', '')

            existing_signals = [s.strip() for s in (row.get('signal_tag', '') or '').split(';') if s.strip()]
            if result.get('found') and 'website_refresh' not in existing_signals:
                existing_signals.append('website_refresh')
                detected += 1
                print(f"  🔔 {row.get('company_name')}: website refresh detected")
            row['signal_tag'] = ';'.join(existing_signals)
            writer.writerow(row)
            time.sleep(0.25)

        print(f"\n  Website refresh detected: {detected} companies")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', '-i', required=True)
    parser.add_argument('--output', '-o', required=True)
    args = parser.parse_args()
    find(args.input, args.output)


if __name__ == '__main__':
    main()