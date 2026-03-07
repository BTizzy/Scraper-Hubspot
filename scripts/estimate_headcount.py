"""estimate_headcount.py

Crawl common about/team pages for each company domain and estimate headcount.

Input: companies_enriched.csv with column `website` or `domain` (domain is preferred).
Output: companies_sized.csv with columns: company, domain, headcount_estimate, headcount_method, headcount_pass

Heuristics used:
- Look for pages: /about, /team, /our-team, /staff, /people
- Count occurrence of headings (h3,h4) and list items that look like person entries
- If estimate between 5 and 30 inclusive, mark headcount_pass = TRUE
"""
import argparse
import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import time

SEARCH_PATHS = ['/about', '/team', '/our-team', '/staff', '/people', '/about-us']

def domain_from_website(website):
    if not website:
        return ''
    if website.startswith('http'):
        p = urlparse(website)
        return p.netloc
    return website

def fetch_page(domain, path):
    url = domain
    if not domain.startswith('http'):
        url = 'https://' + domain
    full = urljoin(url, path)
    try:
        r = requests.get(full, timeout=8)
        if r.status_code == 200:
            return r.text, full
    except Exception:
        return None, full
    return None, full

def estimate_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    # Count headings likely to contain names
    headings = soup.find_all(['h3', 'h4', 'h5'])
    count = 0
    for h in headings:
        text = h.get_text(separator=' ').strip()
        # simple heuristic: heading with 2+ words and a capitalized first word
        if len(text.split()) >= 1:
            count += 1
    # also count list items that look like person entries
    lis = soup.find_all('li')
    for li in lis:
        t = li.get_text(separator=' ').strip()
        if any(word.lower() in t.lower() for word in ['founder', 'ceo', 'president', 'owner', 'head', 'director', 'partner', 'lead']):
            count += 1
    # fallback: look for elements with class containing 'team' and count children
    team_divs = soup.select('[class*="team"], [id*="team"]')
    for d in team_divs:
        people = d.find_all(['li','div','article'])
        if people:
            count = max(count, len(people))
    return count

def estimate(input_csv, output_csv):
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = reader.fieldnames + ['domain', 'headcount_estimate', 'headcount_method', 'headcount_pass']
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            website = row.get('website') or row.get('domain') or ''
            domain = domain_from_website(website)
            row['domain'] = domain
            estimate = ''
            method = ''
            passed = 'FALSE'
            if domain:
                for path in SEARCH_PATHS:
                    html, full = fetch_page(domain, path)
                    if html:
                        cnt = estimate_from_html(html)
                        if cnt:
                            estimate = cnt
                            method = full
                            break
                    time.sleep(0.5)
            try:
                val = int(estimate) if estimate != '' else 0
                if 5 <= val <= 30:
                    passed = 'TRUE'
            except:
                passed = 'FALSE'
            row['headcount_estimate'] = estimate
            row['headcount_method'] = method
            row['headcount_pass'] = passed
            writer.writerow(row)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input','-i', required=True)
    parser.add_argument('--output','-o', required=True)
    args = parser.parse_args()
    estimate(args.input, args.output)

if __name__ == '__main__':
    main()
