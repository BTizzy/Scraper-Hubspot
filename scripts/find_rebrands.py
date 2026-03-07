"""find_rebrands.py

Detect potential rebrands by checking OpenCorporates previous names and scanning website copy for 'formerly', 'rebranded', etc.

Input: companies_enriched.csv (expects `company_name` and optionally `opencorp_url`)
Output: companies_rebrands.csv with columns: rebrand_flag, rebrand_reason, rebrand_sample
"""
import argparse
import csv
import requests
from urllib.parse import urljoin, urlparse
import time

OPENCORP_SEARCH = "https://api.opencorporates.com/v0.4/companies/search?q={q}&jurisdiction_code=us_wa"

KEYWORDS = ['formerly', 'formerly known as', 'rebrand', 'rebranded', 'now called', 'previously known as', 'formerly called']

def search_opencorp(name):
    try:
        q = requests.utils.requote_uri(name)
        url = OPENCORP_SEARCH.format(q=q)
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = data.get('results', {}).get('companies', [])
        if not results:
            return None
        return results[0].get('company')
    except Exception:
        return None

def scan_website_for_keywords(domain):
    if not domain:
        return None, ''
    if not domain.startswith('http'):
        domain = 'https://' + domain
    try:
        r = requests.get(domain, timeout=8)
        if r.status_code != 200:
            return None, ''
        text = r.text.lower()
        for kw in KEYWORDS:
            if kw in text:
                # return a small snippet
                idx = text.find(kw)
                snippet = text[max(0, idx-80):idx+120]
                return True, snippet
        return False, ''
    except Exception:
        return None, ''

def find(input_csv, output_csv):
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = list(reader.fieldnames)
        for col in ['rebrand_flag', 'rebrand_reason', 'rebrand_sample']:
            if col not in fieldnames:
                fieldnames.append(col)
        # Ensure signal_tag column exists
        if 'signal_tag' not in fieldnames:
            fieldnames.append('signal_tag')
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        rebrand_count = 0
        for row in reader:
            name = row.get('company_name') or row.get('Company') or row.get('name')
            opencorp = row.get('opencorp_url') or ''
            rebrand_flag = 'FALSE'
            reason = ''
            sample = ''
            # Check OpenCorporates for previous names
            oc = search_opencorp(name)
            if oc:
                aliases = []
                for k in ['previous_names', 'alternate_names', 'alternative_names', 'other_names']:
                    v = oc.get(k)
                    if v:
                        aliases.extend(v if isinstance(v, list) else [v])
                if aliases:
                    rebrand_flag = 'TRUE'
                    reason = 'OpenCorporates previous/alternate names'
                    sample = '; '.join([str(x) for x in aliases[:3]])

            # Scan homepage for textual signals
            website = row.get('website') or row.get('domain') or ''
            domain = website
            if website and website.startswith('http'):
                p = urlparse(website)
                domain = f"{p.scheme}://{p.netloc}"
            found, snippet = scan_website_for_keywords(domain)
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

            # ── Append signal tag ──
            existing_signals = [s.strip() for s in (row.get('signal_tag', '') or '').split(';') if s.strip()]
            if rebrand_flag == 'TRUE' and 'rebrand' not in existing_signals:
                existing_signals.append('rebrand')
                rebrand_count += 1
                print(f"  🔔 {name}: rebrand detected — {reason}")
            row['signal_tag'] = ';'.join(existing_signals)

            writer.writerow(row)
            time.sleep(0.5)
        print(f"\n  Rebrands detected: {rebrand_count} companies")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input','-i', required=True)
    parser.add_argument('--output','-o', required=True)
    args = parser.parse_args()
    find(args.input, args.output)

if __name__ == '__main__':
    main()
