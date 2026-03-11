"""collect_companies.py

Enrich a WA SOS export CSV with real data from OpenCorporates + web discovery.

The original version left website and officers BLANK — which meant every
downstream script (headcount, waterfall, email permutation) had nothing to
work with. This rewrite actually populates those columns.

Data flow:
  1. Search OpenCorporates for each company → get officers, registry URL, status
  2. Attempt to resolve a website via:
     a) OpenCorporates registered_address / agent metadata (sometimes has URL)
     b) DuckDuckGo search: "company name" seattle WA site
     c) Direct domain guess: clean company name → .com
  3. Write enriched CSV with real website + officers as JSON

Input:  CSV with at least `company_name` column (and ideally `registered_date`)
Output: CSV with added columns: website, domain, officers (JSON), opencorp_url,
        company_number, jurisdiction_code, current_status, signal_tag, collected_date
"""
import csv
import json
import re
import requests
import socket
import time
import argparse
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse

import dns.resolver
from bs4 import BeautifulSoup

# ── OpenCorporates ─────────────────────────────────────────────────────────────

OC_SEARCH = "https://api.opencorporates.com/v0.4/companies/search?q={q}&jurisdiction_code=us_wa"
OC_COMPANY = "https://api.opencorporates.com/v0.4/companies/us_wa/{number}"

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}


def search_opencorporates(name: str) -> dict | None:
    """Search OpenCorporates and return full company data including officers.
    
    Falls back gracefully if the API returns 401/403 (free tier removed).
    """
    q = quote_plus(name)
    url = OC_SEARCH.format(q=q)
    try:
        r = requests.get(url, timeout=12, headers=HEADERS)
        if r.status_code in (401, 403, 429):
            # API key required or rate limited — skip silently
            return None
        r.raise_for_status()
        data = r.json()
        results = data.get('results', {}).get('companies', [])
        if not results:
            return None
        c = results[0].get('company', {})
        result = {
            'name': c.get('name', ''),
            'opencorp_url': c.get('opencorporates_url', ''),
            'company_number': c.get('company_number', ''),
            'jurisdiction_code': c.get('jurisdiction_code', ''),
            'current_status': c.get('current_status', ''),
            'incorporation_date': c.get('incorporation_date', ''),
            'registered_address': '',
            'officers': [],
            'previous_names': [],
        }
        # Try to get the registered address
        addr = c.get('registered_address') or c.get('registered_address_in_full') or ''
        if isinstance(addr, dict):
            addr = addr.get('street_address', '') or addr.get('in_full', '')
        result['registered_address'] = str(addr)

        # Try to fetch detailed company page for officers
        number = c.get('company_number')
        if number:
            try:
                detail_url = OC_COMPANY.format(number=number)
                dr = requests.get(detail_url, timeout=12, headers=HEADERS)
                if dr.status_code == 200:
                    detail = dr.json().get('results', {}).get('company', {})
                    # Officers
                    officers_raw = detail.get('officers', [])
                    for o in officers_raw:
                        officer = o.get('officer', {})
                        if officer.get('name'):
                            result['officers'].append({
                                'name': officer.get('name', ''),
                                'title': officer.get('position', '') or officer.get('role', ''),
                            })
                    # Previous names
                    prev = detail.get('previous_names', [])
                    for p in prev:
                        pn = p.get('company_name') if isinstance(p, dict) else str(p)
                        if pn:
                            result['previous_names'].append(pn)
            except Exception:
                pass  # officer detail is optional
        return result
    except Exception as e:
        # Don't print 401/403 errors — they just mean no API key
        if '401' not in str(e) and '403' not in str(e):
            print(f"  ✗ OpenCorporates error for '{name}': {e}")
        return None


def scrape_opencorporates_html(name: str) -> dict | None:
    """
    Fallback: scrape the OpenCorporates HTML search page when API is locked.
    Returns basic company data (name, URL, officers if on the page).
    """
    try:
        search_url = f"https://opencorporates.com/companies?q={quote_plus(name)}&jurisdiction_code=us_wa"
        r = requests.get(search_url, timeout=12, headers=HEADERS)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        # Find first result link
        result_link = soup.select_one('a.company_search_result, li.search-result a, .results a[href*="/companies/us_wa/"]')
        if not result_link:
            # Try broader selector
            for a in soup.find_all('a', href=True):
                if '/companies/us_wa/' in a['href']:
                    result_link = a
                    break
        if not result_link:
            return None
        
        company_url = result_link['href']
        if not company_url.startswith('http'):
            company_url = 'https://opencorporates.com' + company_url
        
        result = {
            'name': result_link.get_text(strip=True),
            'opencorp_url': company_url,
            'officers': [],
        }
        
        # Try to fetch the company detail page for officers
        try:
            dr = requests.get(company_url, timeout=12, headers=HEADERS)
            if dr.status_code == 200:
                detail_soup = BeautifulSoup(dr.text, 'html.parser')
                # Look for officers section
                officers_section = detail_soup.find(id='officers') or detail_soup.find(class_='officers')
                if officers_section:
                    for item in officers_section.find_all('li'):
                        name_el = item.find(class_='officer_name') or item.find('a')
                        role_el = item.find(class_='officer_role') or item.find(class_='role')
                        if name_el:
                            result['officers'].append({
                                'name': name_el.get_text(strip=True),
                                'title': role_el.get_text(strip=True) if role_el else '',
                            })
        except Exception:
            pass
        
        return result
    except Exception:
        return None


# ── Domain discovery ───────────────────────────────────────────────────────────

def clean_company_for_domain(name: str) -> str:
    """Strip LLC, Inc, Corp, etc. and return a slug suitable for domain guessing."""
    suffixes = r'\b(llc|inc|corp|corporation|co|company|ltd|limited|pllc|lp|llp|group|services|holdings)\b'
    clean = re.sub(suffixes, '', name, flags=re.IGNORECASE)
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', clean).strip()
    return clean.lower().replace(' ', '')


def normalize_domain(value: str) -> str:
    """Normalize URL/domain strings to a plain host name."""
    if not value:
        return ''
    value = value.strip()
    if not value:
        return ''
    if value.startswith('//'):
        value = 'https:' + value
    elif not value.startswith('http'):
        value = 'https://' + value
    try:
        parsed = urlparse(value)
    except Exception:
        return ''
    domain = parsed.netloc.lower().strip()
    if domain.startswith('www.'):
        domain = domain[4:]
    domain = domain.split(':')[0]
    return domain


def domain_has_dns(domain: str) -> bool:
    """Quick A/AAAA check to ensure domain resolves."""
    try:
        socket.getaddrinfo(domain, None)
        return True
    except Exception:
        return False


def domain_has_mx(domain: str) -> bool:
    """Quick MX check to validate a domain is real."""
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        return len(answers) > 0
    except Exception:
        return False


def domain_responds(domain: str) -> bool:
    """Check if a domain has a working website."""
    for scheme in ['https', 'http']:
        try:
            r = requests.head(f'{scheme}://{domain}', timeout=5, headers=HEADERS,
                              allow_redirects=True)
            if r.status_code < 400:
                return True
        except Exception:
            continue
    return False


def try_domain_variants(slug: str) -> str:
    """Try many modern TLD variants for a slug and return first valid hit."""
    tlds = [
        '.com', '.net', '.org', '.co', '.us', '.biz',
        '.io', '.dev', '.app', '.tech', '.software', '.company', '.business',
        '.co.uk', '.com.au', '.ca', '.de', '.ch', '.eu',
        '.ai', '.cloud', '.digital', '.online', '.site', '.space',
    ]
    for tld in tlds:
        domain = slug + tld
        if domain_has_dns(domain) or domain_has_mx(domain) or domain_responds(domain):
            return domain
    return ''


def discover_domain_duckduckgo(company_name: str) -> str:
    """Search DuckDuckGo for the company website."""
    query = f'"{company_name}" seattle WA website'
    skip_domains = ['facebook.com', 'linkedin.com', 'yelp.com', 'twitter.com',
                    'instagram.com', 'bbb.org', 'yellowpages.com', 'mapquest.com',
                    'google.com', 'bing.com', 'wa.gov', 'sec.gov', 'wikipedia.org',
                    'bloomberg.com', 'dnb.com', 'opencorporates.com', 'zoominfo.com',
                    'crunchbase.com', 'indeed.com', 'glassdoor.com', 'github.com',
                    'medium.com', 'reddit.com', 'producthunt.com', 'techcrunch.com']

    def is_allowed_domain(domain: str) -> bool:
        return bool(domain) and not any(domain == s or domain.endswith('.' + s) for s in skip_domains)

    try:
        r = requests.get(
            'https://html.duckduckgo.com/html/',
            params={'q': query},
            headers=HEADERS,
            timeout=10,
        )
        soup = BeautifulSoup(r.text, 'html.parser')

        # Prefer explicit LinkedIn company URLs as a high-signal hint.
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            if 'linkedin.com/company/' not in href:
                continue
            m = re.search(r'linkedin\.com/company/([a-z0-9\-]+)', href.lower())
            if not m:
                continue
            slug_guess = m.group(1).replace('-', '')
            if slug_guess:
                candidate = slug_guess + '.com'
                if domain_has_dns(candidate) or domain_responds(candidate):
                    return candidate

        # DuckDuckGo HTML results have .result__url spans
        for link in soup.select('a.result__url'):
            href = link.get('href', '') or link.get_text(strip=True)
            if href:
                domain = normalize_domain(href)
                if is_allowed_domain(domain):
                    return domain
        # Fallback: try result__a links
        for link in soup.select('a.result__a'):
            href = link.get('href', '')
            if href and href.startswith('http'):
                domain = normalize_domain(href)
                if is_allowed_domain(domain):
                    return domain
        # Final fallback: generic anchor scan for hidden URLs in DDG HTML
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            if not href:
                continue
            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                continue
            elif not href.startswith('http'):
                href = 'https://' + href
            domain = normalize_domain(href)
            if is_allowed_domain(domain):
                return domain
    except Exception as e:
        print(f"  ✗ DuckDuckGo search failed: {e}")
    return ''


def discover_website(company_name: str) -> str:
    """
    Try multiple strategies to find a company's website domain:
      1. DuckDuckGo search with filtering
      2. Direct domain variants from cleaned slug
      3. Hyphenated/short-name variants for multi-word companies
    """
    # Strategy 1: DuckDuckGo
    domain = discover_domain_duckduckgo(company_name)
    if domain and (domain_has_dns(domain) or domain_has_mx(domain) or domain_responds(domain)):
        return domain

    # Strategy 2: Guess common domain patterns
    slug = clean_company_for_domain(company_name)
    if slug:
        guessed = try_domain_variants(slug)
        if guessed:
            return guessed

        words = re.split(r'[^a-z0-9]+', company_name.lower())
        words = [w for w in words if w and w not in {
            'llc', 'inc', 'corp', 'company', 'co', 'ltd', 'group',
            'services', 'holdings', 'the', 'and'
        }]

        if len(words) > 1:
            guessed = try_domain_variants('-'.join(words))
            if guessed:
                return guessed

        if len(words) > 2:
            guessed = try_domain_variants(words[0] + words[-1])
            if guessed:
                return guessed

    return domain or ''  # return DDG result even if we couldn't verify it


# ── Signal tagging ─────────────────────────────────────────────────────────────

def detect_formation_signal(row: dict) -> str:
    """Check if this is a new business formation (registered in last 24 months)."""
    date_str = row.get('registered_date', '') or row.get('incorporation_date', '')
    if not date_str:
        return ''
    for fmt in ['%Y-%m-%d', '%m/%d/%Y', '%m-%d-%Y', '%Y-%m-%dT%H:%M:%S']:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            cutoff = datetime.now() - timedelta(days=730)  # ~24 months
            if dt >= cutoff:
                return 'new_business'
            return ''
        except ValueError:
            continue
    return ''


# ── Parse officers from WA SOS columns ─────────────────────────────────────────

def extract_officers_from_sos(row: dict) -> list[dict]:
    """
    WA SOS CCFS exports include governor/officer columns like:
      governor_1, governor_1_title, governor_2, governor_2_title, ...
      registered_agent
    Parse these into our standard officer format.
    """
    officers = []
    seen_names = set()
    
    # Check for governor_N / governor_N_title pattern (most common in WA SOS exports)
    for i in range(1, 10):
        name = row.get(f'governor_{i}', '') or row.get(f'Governor{i}', '') or ''
        title = row.get(f'governor_{i}_title', '') or row.get(f'Governor{i}Title', '') or ''
        name = name.strip()
        if name and name.lower() not in seen_names:
            seen_names.add(name.lower())
            officers.append({'name': name, 'title': title.strip()})
    
    # Check for officer_N pattern
    for i in range(1, 10):
        name = row.get(f'officer_{i}', '') or row.get(f'Officer{i}', '') or ''
        title = row.get(f'officer_{i}_title', '') or row.get(f'Officer{i}Title', '') or ''
        name = name.strip()
        if name and name.lower() not in seen_names:
            seen_names.add(name.lower())
            officers.append({'name': name, 'title': title.strip()})
    
    # Check registered_agent (often the owner for small businesses)
    agent = (row.get('registered_agent', '') or row.get('Registered Agent', '') or '').strip()
    if agent and agent.lower() not in seen_names:
        # Only add if it looks like a person name (not a company name)
        parts = agent.split()
        if 2 <= len(parts) <= 4 and not any(w.lower() in agent.lower() for w in ['llc', 'inc', 'corp', 'ltd', 'service', 'company', 'group']):
            seen_names.add(agent.lower())
            officers.append({'name': agent, 'title': 'Registered Agent'})
    
    # Check for generic 'officer_name' / 'agent_name' columns
    for col in ['officer_name', 'agent_name', 'principal_name', 'owner_name']:
        name = (row.get(col, '') or '').strip()
        if name and name.lower() not in seen_names:
            seen_names.add(name.lower())
            officers.append({'name': name, 'title': ''})
    
    return officers


# ── Main enrichment loop ──────────────────────────────────────────────────────

def enrich(input_csv: str, output_csv: str):
    with open(input_csv, newline='', encoding='utf-8') as fin:
        reader = csv.DictReader(fin)
        input_fieldnames = list(reader.fieldnames)
        rows = list(reader)

    output_fields = input_fieldnames.copy()
    for col in ['website', 'domain', 'officers', 'opencorp_url', 'company_number',
                'jurisdiction_code', 'current_status', 'registered_address',
                'signal_tag', 'collected_date']:
        if col not in output_fields:
            output_fields.append(col)

    today = datetime.now().strftime('%Y-%m-%d')
    enriched = 0
    domains_found = 0

    with open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        writer = csv.DictWriter(fout, fieldnames=output_fields)
        writer.writeheader()

        for i, row in enumerate(rows, 1):
            company = row.get('company_name') or row.get('Company') or row.get('name') or ''
            if not company:
                print(f"[{i}/{len(rows)}] Skipping row — no company name")
                continue

            print(f"[{i}/{len(rows)}] {company}")
            row['collected_date'] = today

            # ── OpenCorporates lookup (API first, then HTML scrape fallback) ──
            oc = search_opencorporates(company)
            if not oc:
                oc = scrape_opencorporates_html(company)
            
            # ── Officers: WA SOS columns first (most reliable), then OC fallback ──
            sos_officers = extract_officers_from_sos(row)
            oc_officers = oc.get('officers', []) if oc else []
            
            # Merge: SOS officers take priority, add OC officers if new names
            all_officers = list(sos_officers)
            seen_names = {o['name'].lower() for o in all_officers}
            for o in oc_officers:
                if o['name'].lower() not in seen_names:
                    all_officers.append(o)
                    seen_names.add(o['name'].lower())
            
            if oc:
                enriched += 1
                row['opencorp_url'] = oc.get('opencorp_url', '')
                row['company_number'] = oc.get('company_number', '')
                row['jurisdiction_code'] = oc.get('jurisdiction_code', '')
                row['current_status'] = oc.get('current_status', '')
                row['registered_address'] = oc.get('registered_address', '')
                # Use incorporation date if we don't have one
                if not row.get('registered_date') and oc.get('incorporation_date'):
                    row['registered_date'] = oc['incorporation_date']
            else:
                row['opencorp_url'] = ''
                row['company_number'] = ''
                row['jurisdiction_code'] = ''
                row['current_status'] = row.get('status', '')
                row['registered_address'] = row.get('principal_office', '')
            
            # Write officers as JSON
            row['officers'] = json.dumps(all_officers) if all_officers else ''
            if all_officers:
                print(f"  ✓ {len(all_officers)} officers: {', '.join(o['name'] for o in all_officers[:3])}")
            else:
                print(f"  ✗ No officers found")

            # ── Domain discovery ──
            existing_website = row.get('website', '') or row.get('domain', '')
            if existing_website:
                domain = existing_website
                if domain.startswith('http'):
                    domain = urlparse(domain).netloc
                row['domain'] = domain
                if not row.get('website'):
                    row['website'] = f'https://{domain}'
                print(f"  ✓ Domain (from input): {domain}")
            else:
                print(f"  🔍 Searching for website...")
                domain = discover_website(company)
                if domain:
                    domains_found += 1
                    if domain.startswith('http'):
                        domain = urlparse(domain).netloc
                    row['domain'] = domain
                    row['website'] = f'https://{domain}'
                    print(f"  ✓ Domain found: {domain}")
                else:
                    row['domain'] = ''
                    row['website'] = ''
                    print(f"  ✗ No website found")

            # ── Formation signal ──
            signals = []
            formation = detect_formation_signal(row)
            if formation:
                signals.append(formation)
                print(f"  🔔 Signal: new_business (filed {row.get('registered_date', '?')})")
            row['signal_tag'] = ';'.join(signals) if signals else ''

            writer.writerow(row)
            time.sleep(0.8)  # polite rate limiting

    print(f"\n✅ Enrichment complete:")
    print(f"  Companies processed: {len(rows)}")
    print(f"  OpenCorporates matches: {enriched}")
    print(f"  Domains discovered: {domains_found}")
    print(f"  Output: {output_csv}")


def main():
    parser = argparse.ArgumentParser(description='Enrich WA SOS companies with OpenCorporates + domain discovery')
    parser.add_argument('--input', '-i', required=True, help='WA SOS CSV export')
    parser.add_argument('--output', '-o', required=True, help='Enriched output CSV')
    args = parser.parse_args()
    enrich(args.input, args.output)

if __name__ == '__main__':
    main()
