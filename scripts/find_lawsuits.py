"""find_lawsuits.py

Query CourtListener for mentions of company names in the last 12 months to detect active litigation.

Input: companies_enriched.csv with `company_name` column.
Output: companies_lawsuits.csv with added columns: lawsuits_found (TRUE/FALSE), lawsuits_count, lawsuits_sample

Notes: CourtListener public API is used here for initial automated checks. Results must be normalized; name collisions are possible.
"""
import argparse
import csv
import requests
from datetime import datetime, timedelta
import time
import urllib.parse

COURTLISTENER_SEARCH = 'https://www.courtlistener.com/api/rest/v3/search/?q={q}&type=o'

def query_courtlistener(name, since_date):
    q = urllib.parse.quote_plus(name)
    url = COURTLISTENER_SEARCH.format(q=q)
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data.get('results', [])
        # filter by date if available in 'date_filed' or 'date'
        recent = []
        for item in results:
            # CourtListener objects vary; try common date fields
            date_str = item.get('date_filed') or item.get('date') or item.get('decision_date')
            if not date_str:
                recent.append(item)
                continue
            try:
                dt = datetime.fromisoformat(date_str)
                if dt >= since_date:
                    recent.append(item)
            except Exception:
                recent.append(item)
        return recent
    except Exception as e:
        print(f"CourtListener query failed for {name}: {e}")
        return []

def find(input_csv, output_csv):
    since = datetime.utcnow() - timedelta(days=365)
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = list(reader.fieldnames)
        for col in ['lawsuits_found', 'lawsuits_count', 'lawsuits_sample']:
            if col not in fieldnames:
                fieldnames.append(col)
        # Ensure signal_tag column exists
        if 'signal_tag' not in fieldnames:
            fieldnames.append('signal_tag')
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        found_count = 0
        for row in reader:
            name = row.get('company_name') or row.get('Company') or row.get('name')
            if not name:
                row['lawsuits_found'] = 'FALSE'
                row['lawsuits_count'] = 0
                row['lawsuits_sample'] = ''
                writer.writerow(row)
                continue
            results = query_courtlistener(name, since)
            count = len(results)
            row['lawsuits_found'] = 'TRUE' if count > 0 else 'FALSE'
            row['lawsuits_count'] = count
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
                print(f"  🔔 {name}: {count} lawsuit(s) found")
            row['signal_tag'] = ';'.join(existing_signals)
            writer.writerow(row)
            time.sleep(0.5)
        print(f"\n  Lawsuits detected: {found_count} companies")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input','-i', required=True)
    parser.add_argument('--output','-o', required=True)
    args = parser.parse_args()
    find(args.input, args.output)

if __name__ == '__main__':
    main()
