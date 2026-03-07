"""discover_contacts.py

Run theHarvester against domains to collect candidate emails and names.

Input: companies_enriched.csv with column `website` or `domain` (if website is full URL, script extracts domain).
Output: contacts_raw.csv with columns: company, domain, name, email, source_tool, notes

Requires: theHarvester installed in the same Python environment (pip install theHarvester)
"""
import csv
import argparse
import subprocess
import shlex
import time
import re
import os
from urllib.parse import urlparse

def domain_from_website(website):
    if not website:
        return ''
    if not website.startswith('http'):
        website = 'https://' + website
    try:
        p = urlparse(website)
        return p.netloc
    except:
        return website

def run_theharvester(domain, limit=100):
    # theHarvester command that outputs raw results to stdout
    cmd = f"theHarvester -d {domain} -b google,bing,yahoo,duckduckgo -l {limit}"
    print(f"Running: {cmd}")
    try:
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=120)
        return proc.stdout
    except Exception as e:
        print(f"theHarvester error for {domain}: {e}")
        return ''

def parse_theharvester_output(text):
    # Improved parser: extract email tokens via regex and try to capture 'Name <email>' patterns
    email_re = re.compile(r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})')
    name_email_re = re.compile(r'([A-Z][a-z]+(?: [A-Z][a-z]+){0,2})\s*[<\(]([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})[>\)]')
    emails = set()
    names = {}
    # find name <email> patterns first
    for m in name_email_re.finditer(text):
        nm = m.group(1).strip()
        em = m.group(2).strip()
        emails.add(em)
        names[em] = nm
    # fallback: any email-looking token
    for m in email_re.finditer(text):
        em = m.group(1).strip()
        emails.add(em)
        if em not in names:
            # try to infer from local-part: first.last or first_last or firstlast
            local = em.split('@',1)[0]
            if '.' in local:
                parts = local.split('.')
                fname = parts[0].capitalize()
                lname = parts[-1].capitalize()
                names[em] = f"{fname} {lname}".strip()
            elif '_' in local:
                parts = local.split('_')
                fname = parts[0].capitalize()
                lname = parts[-1].capitalize()
                names[em] = f"{fname} {lname}".strip()
            else:
                names[em] = ''
    return list(emails), names

def discover(input_csv, output_csv):
    os.makedirs('theharvester_outputs', exist_ok=True)
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = ['company', 'domain', 'name', 'email', 'source_tool', 'notes']
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            company = row.get('company_name') or row.get('name') or ''
            website = row.get('website') or row.get('domain') or ''
            domain = domain_from_website(website)
            if not domain:
                print(f"Skipping {company}: no domain")
                continue
            out = run_theharvester(domain)
            # save raw output for manual inspection
            try:
                with open(f'theharvester_outputs/{domain}.txt', 'w', encoding='utf-8') as rf:
                    rf.write(out)
            except Exception:
                pass
            emails, names = parse_theharvester_output(out)
            for e in emails:
                nm = names.get(e, '')
                writer.writerow({'company': company, 'domain': domain, 'name': nm, 'email': e, 'source_tool': 'theHarvester', 'notes': ''})
            time.sleep(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', '-i', required=True)
    parser.add_argument('--output', '-o', required=True)
    args = parser.parse_args()
    discover(args.input, args.output)

if __name__ == '__main__':
    main()
