"""waterfall_enricher.py

Multi-source contact discovery with waterfall / cascade logic.

Inspired by:
  • theHarvester — 30+ async discovery modules, unified parser, set-based dedup
  • h8mail — target_factory sequential chaining, "chasing" re-enrichment loop
  • Apollo — data contributor network + web crawling + third-party provider blend

Sources (in priority order — each one adds what previous ones missed):
  1. theHarvester        — email harvest from search engines (free, no key)
  2. Team page scraper   — crawl /about /team /staff pages for emails
  3. Officer permutation — take WA SOS / OpenCorporates officer names → email_permutator
  4. Hunter.io           — 25 free searches/month (needs API key)
  5. Google Dork scraper — "site:domain.com @domain.com" (experimental)

Each discovered email is tagged with its source so we can track which channel
yields the best results over time (Apollo does this to weight sources).

Usage:
  python waterfall_enricher.py --domain seattlestudio.com --company "Seattle Studio LLC"
  python waterfall_enricher.py --input enriched_companies.csv --output contacts_raw.csv
  python waterfall_enricher.py --input enriched_companies.csv --output contacts_raw.csv --hunter-key YOUR_KEY
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

from local_secrets import load_local_env

load_local_env()

from trillium_config import DM_TITLES, GENERIC_LOCAL_PARTS, EMAIL_PATTERNS

# ── Source 1: theHarvester ─────────────────────────────────────────────────────

def source_theharvester(domain: str, timeout: int = 120) -> list[dict]:
    """
    Run theHarvester against a domain. Returns list of
    {email, first_name, last_name, source} dicts.
    """
    results = []
    try:
        proc = subprocess.run(
            ['theHarvester', '-d', domain, '-b', 'all', '-l', '200'],
            capture_output=True, text=True, timeout=timeout
        )
        text = proc.stdout + '\n' + proc.stderr
        # Save raw output for debugging
        os.makedirs('waterfall_outputs', exist_ok=True)
        with open(f'waterfall_outputs/{domain}_theharvester.txt', 'w') as f:
            f.write(text)
        # Parse emails
        name_email_re = re.compile(r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[<(]\s*(\S+@\S+\.\S+)\s*[>)]')
        email_re = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
        for m in name_email_re.finditer(text):
            name_parts = m.group(1).split()
            results.append({
                'email': m.group(2).lower().strip(),
                'first_name': name_parts[0],
                'last_name': name_parts[-1],
                'source': 'theHarvester',
            })
        seen = {r['email'] for r in results}
        for m in email_re.finditer(text):
            email = m.group().lower().strip().rstrip('.')
            if email not in seen:
                first, last = infer_name_from_email(email)
                results.append({
                    'email': email,
                    'first_name': first,
                    'last_name': last,
                    'source': 'theHarvester',
                })
                seen.add(email)
    except FileNotFoundError:
        print("  ⚠ theHarvester not installed, skipping source")
    except subprocess.TimeoutExpired:
        print(f"  ⚠ theHarvester timed out for {domain}")
    except Exception as e:
        print(f"  ⚠ theHarvester error for {domain}: {e}")
    return results


# ── Source 2: Team page scraper ────────────────────────────────────────────────

TEAM_PATHS = ['/about', '/team', '/our-team', '/staff', '/people', '/about-us',
              '/contact', '/leadership', '/who-we-are', '/meet-the-team']

TEAM_PATH_HINTS = ('team', 'staff', 'people', 'leadership', 'about', 'contact', 'careers', 'join')


def extract_emails_from_text(text: str, domain: str) -> set[str]:
    """Extract direct and lightly obfuscated emails for a specific domain."""
    found = set()
    if not text:
        return found

    direct_re = re.compile(rf'[\w.+-]+@{re.escape(domain)}', re.I)
    for m in direct_re.finditer(text):
        found.add(m.group().lower().strip().rstrip('.'))

    # Common obfuscations: jane [at] example.com, jane(at)example.com, jane at example.com
    obfuscated_re = re.compile(
        rf'([a-z0-9._%+-]{{1,64}})\s*(?:\[?\(?\s*at\s*\)?\]?|@)\s*{re.escape(domain)}',
        re.I,
    )
    for m in obfuscated_re.finditer(text):
        local = m.group(1).lower().strip('.-_')
        if local:
            found.add(f'{local}@{domain}')

    return found


def discover_candidate_paths(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Discover likely team/contact paths from homepage links."""
    discovered = []
    seen = set()

    def add_path(candidate: str):
        if not candidate:
            return
        if not candidate.startswith('/'):
            return
        candidate = candidate.split('#', 1)[0].split('?', 1)[0]
        if candidate in seen:
            return
        if any(hint in candidate.lower() for hint in TEAM_PATH_HINTS):
            seen.add(candidate)
            discovered.append(candidate)

    for anchor in soup.find_all('a', href=True):
        href = (anchor.get('href') or '').strip()
        if not href:
            continue
        if href.startswith('mailto:'):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        base_host = urlparse(base_url).netloc
        if parsed.netloc and parsed.netloc != base_host:
            continue
        add_path(parsed.path)

    return discovered

def source_team_page(domain: str, timeout: int = 5) -> list[dict]:
    """Scrape company team/about/contact pages for emails."""
    results = []
    seen = set()
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
    page_urls = []

    def add_contact(email: str, source_hint: str, context_element=None):
        email = email.lower().strip().rstrip('.')
        if email in seen:
            return
        if ('@' + domain) not in email:
            return
        local = email.split('@')[0]
        if local in GENERIC_LOCAL_PARTS:
            return
        first, last = infer_name_from_email(email)
        title = ''
        if context_element is not None:
            name = extract_person_name(context_element)
            if name:
                first, last = name
            context_text = context_element.get_text(' ', strip=True).lower()
            for dm_title in DM_TITLES:
                if dm_title in context_text:
                    title = dm_title.title()
                    break
        seen.add(email)
        results.append({
            'email': email,
            'first_name': first,
            'last_name': last,
            'title': title,
            'source': source_hint,
        })

    # First check if domain responds at all (skip entirely if not)
    base_url = ''
    try:
        r = requests.head(f'https://{domain}', timeout=(3, 3), headers=headers, allow_redirects=True)
        base_url = r.url or f'https://{domain}'
    except Exception:
        try:
            r = requests.head(f'http://{domain}', timeout=(3, 3), headers=headers, allow_redirects=True)
            base_url = r.url or f'http://{domain}'
        except Exception:
            return results  # domain doesn't respond, skip all paths

    # Start with homepage to discover additional candidate paths.
    candidate_paths = list(TEAM_PATHS)
    try:
        home = requests.get(base_url, timeout=(3, timeout), headers=headers, allow_redirects=True)
        if home.status_code == 200:
            soup = BeautifulSoup(home.text, 'html.parser')
            for discovered in discover_candidate_paths(base_url, soup):
                if discovered not in candidate_paths:
                    candidate_paths.append(discovered)
            # Extract any immediate homepage emails.
            for email in extract_emails_from_text(home.text, domain):
                add_contact(email, 'team_page')
    except Exception:
        pass

    for path in candidate_paths:
        for scheme in ['https', 'http']:
            url = f'{scheme}://{domain}{path}'
            if url in page_urls:
                continue
            page_urls.append(url)
            try:
                resp = requests.get(url, timeout=(3, timeout), headers=headers, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, 'html.parser')
                # Look for mailto links first (highest confidence)
                for a in soup.find_all('a', href=True):
                    if a['href'].startswith('mailto:'):
                        email = a['href'].replace('mailto:', '').split('?')[0].lower().strip()
                        parent = a.find_parent(['div', 'li', 'td', 'article', 'section'])
                        add_contact(email, 'team_page', context_element=parent)

                # Scan full page text for direct and obfuscated emails tied to this domain.
                for email in extract_emails_from_text(resp.text, domain):
                    add_contact(email, 'team_page')
                break  # https worked, don't try http
            except Exception:
                continue
        time.sleep(0.15)

    return results


def extract_person_name(element) -> tuple[str, str] | None:
    """Try to extract a person name from an HTML element (team card, list item)."""
    # Look for heading tags that often contain names
    for tag in ['h2', 'h3', 'h4', 'h5', 'strong', 'b']:
        heading = element.find(tag)
        if heading:
            text = heading.get_text(strip=True)
            parts = text.split()
            if 2 <= len(parts) <= 4 and all(p[0].isupper() for p in parts[:2]):
                return (parts[0], parts[-1])
    # Look for class names containing 'name'
    name_el = element.find(class_=re.compile(r'name', re.I))
    if name_el:
        text = name_el.get_text(strip=True)
        parts = text.split()
        if 2 <= len(parts) <= 4:
            return (parts[0], parts[-1])
    return None


# ── Source 3: Officer name → email permutation ─────────────────────────────────

def source_officer_permutation(domain: str, officers: list[dict]) -> list[dict]:
    """
    Take officer names from WA SOS / OpenCorporates and permutate emails.
    officers = [{'name': 'Jane Doe', 'title': 'CEO'}, ...]
    """
    from email_permutator import generate_permutations
    results = []
    for officer in officers:
        name = officer.get('name', '')
        parts = name.strip().split()
        if len(parts) < 2:
            continue
        first = parts[0]
        last = parts[-1]
        candidates = generate_permutations(first, last, domain)
        # Return top-3 most probable patterns (first.last, first, flast)
        for email in candidates[:3]:
            results.append({
                'email': email,
                'first_name': first,
                'last_name': last,
                'source': 'officer_permutation',
                'title': officer.get('title', ''),
            })
    return results


# ── Source 4: Hunter.io free tier ──────────────────────────────────────────────

def source_hunter(domain: str, api_key: str | None = None) -> list[dict]:
    """
    Hunter.io domain search (25 free searches/month).
    Returns discovered emails with confidence scores.
    """
    if not api_key:
        api_key = os.environ.get('HUNTER_API_KEY')
    if not api_key:
        return []
    results = []
    try:
        url = 'https://api.hunter.io/v2/domain-search'
        resp = requests.get(url, params={'domain': domain, 'api_key': api_key}, timeout=10)
        data = resp.json()
        if 'data' in data and 'emails' in data['data']:
            for entry in data['data']['emails']:
                results.append({
                    'email': entry.get('value', '').lower(),
                    'first_name': entry.get('first_name', ''),
                    'last_name': entry.get('last_name', ''),
                    'source': 'hunter.io',
                    'title': entry.get('position', ''),
                    'hunter_confidence': entry.get('confidence', 0),
                })
    except Exception as e:
        print(f"  ⚠ Hunter.io error for {domain}: {e}")
    return results


# ── Source 5: Google Dork search ───────────────────────────────────────────────

def source_google_dork(domain: str) -> list[dict]:
    """
    Experimental: Google search for emails at a domain.
    Uses a simple search URL scrape — may hit CAPTCHAs. Low priority source.
    """
    results = []
    email_re = re.compile(r'[\w.+-]+@' + re.escape(domain))
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
    queries = [
        f'"{domain}" email contact',
        f'"@{domain}"',
    ]
    for query in queries:
        try:
            # Use DuckDuckGo HTML (less aggressive blocking than Google)
            resp = requests.get(
                'https://html.duckduckgo.com/html/',
                params={'q': query},
                headers=headers,
                timeout=10
            )
            for m in email_re.finditer(resp.text):
                email = m.group().lower().strip().rstrip('.')
                local = email.split('@')[0]
                if local not in GENERIC_LOCAL_PARTS:
                    first, last = infer_name_from_email(email)
                    results.append({
                        'email': email,
                        'first_name': first,
                        'last_name': last,
                        'source': 'google_dork',
                    })
            time.sleep(1)  # rate limit
        except Exception:
            continue
    return results


# ── Helpers ────────────────────────────────────────────────────────────────────

def infer_name_from_email(email: str) -> tuple[str, str]:
    """Try to infer first/last from email local part."""
    local = email.split('@')[0].lower()
    for sep in ['.', '_', '-']:
        if sep in local:
            parts = local.split(sep)
            if len(parts) >= 2 and all(p.isalpha() for p in parts[:2]):
                return (parts[0].capitalize(), parts[1].capitalize())
    return ('', '')


def deduplicate(contacts: list[dict]) -> list[dict]:
    """Deduplicate contacts by email. Keep the first occurrence (highest priority source)."""
    seen = set()
    unique = []
    for contact in contacts:
        email = contact.get('email', '').lower()
        if email and email not in seen:
            seen.add(email)
            unique.append(contact)
    return unique


def filter_decision_makers(contacts: list[dict]) -> list[dict]:
    """Promote contacts whose title matches DM_TITLES to the top."""
    dm = []
    other = []
    for c in contacts:
        title = c.get('title', '').lower()
        if any(t in title for t in DM_TITLES):
            c['is_dm'] = True
            dm.append(c)
        else:
            c['is_dm'] = False
            other.append(c)
    return dm + other


# ── Waterfall orchestrator ─────────────────────────────────────────────────────

def waterfall_enrich(domain: str, company: str = '', officers: list[dict] = None,
                     hunter_key: str = None, skip_theharvester: bool = False,
                     skip_dorks: bool = False, company_meta: dict = None) -> list[dict]:
    """
    Run all sources in waterfall order. Each source adds what previous ones missed.
    Returns deduplicated, scored contact list.
    company_meta: extra columns from the company row to carry forward (signal_tag, dates, etc.)
    """
    all_contacts = []
    # Source 1: theHarvester (broadest free source)
    if not skip_theharvester:
        print(f"  🔍 Source 1/5: theHarvester → {domain}")
        contacts = source_theharvester(domain)
        print(f"    Found {len(contacts)} emails")
        all_contacts.extend(contacts)
    # Source 2: Team page scraper
    print(f"  🔍 Source 2/5: Team pages → {domain}")
    contacts = source_team_page(domain)
    print(f"    Found {len(contacts)} emails")
    all_contacts.extend(contacts)
    # Source 3: Officer name permutation
    if officers:
        print(f"  🔍 Source 3/5: Officer permutation → {len(officers)} officers")
        contacts = source_officer_permutation(domain, officers)
        print(f"    Generated {len(contacts)} candidates")
        all_contacts.extend(contacts)
    else:
        print(f"  ⏭ Source 3/5: No officers provided, skipping")
    # Source 4: Hunter.io
    if hunter_key or os.environ.get('HUNTER_API_KEY'):
        print(f"  🔍 Source 4/5: Hunter.io → {domain}")
        contacts = source_hunter(domain, api_key=hunter_key)
        print(f"    Found {len(contacts)} emails")
        all_contacts.extend(contacts)
    else:
        print(f"  ⏭ Source 4/5: No Hunter API key, skipping")
    # Source 5: Google Dorks (experimental, low priority)
    if not skip_dorks:
        print(f"  🔍 Source 5/5: DuckDuckGo dork → {domain}")
        contacts = source_google_dork(domain)
        print(f"    Found {len(contacts)} emails")
        all_contacts.extend(contacts)
    # Tag all with company and carry forward metadata from company row
    meta = company_meta or {}
    for c in all_contacts:
        c['company'] = c.get('company', '') or company
        # Carry forward signal_tag, dates, domain, website from company row
        for key in ('signal_tag', 'registered_date', 'collected_date', 'domain', 'website'):
            if key not in c or not c[key]:
                c[key] = meta.get(key, '')
    # Deduplicate (first source wins — priority order matters)
    unique = deduplicate(all_contacts)
    # Promote decision-makers to top
    unique = filter_decision_makers(unique)
    print(f"  ✅ Total unique contacts: {len(unique)}")
    return unique


# ── Batch mode ─────────────────────────────────────────────────────────────────

def batch_enrich(input_csv: str, output_csv: str, hunter_key: str = None,
                 skip_theharvester: bool = False, skip_dorks: bool = False):
    """
    Read enriched_companies.csv (output of collect_companies.py) and run
    waterfall enrichment on each company's domain.
    """
    with open(input_csv, newline='', encoding='utf-8') as fin:
        reader = csv.DictReader(fin)
        rows = list(reader)
    all_contacts = []
    for i, row in enumerate(rows, 1):
        company = row.get('company_name', '')
        domain = row.get('website') or row.get('domain') or ''
        # Clean domain
        if domain.startswith('http'):
            domain = urlparse(domain).netloc
        if not domain:
            print(f"[{i}/{len(rows)}] {company}: no domain, skipping")
            continue
        print(f"\n[{i}/{len(rows)}] {company} → {domain}")
        # Gather officers if available
        officers = []
        if row.get('officers'):
            try:
                officers = json.loads(row['officers'])
            except Exception:
                pass
        # Build metadata dict to carry forward to contacts
        company_meta = {
            'signal_tag': row.get('signal_tag', ''),
            'registered_date': row.get('registered_date', ''),
            'collected_date': row.get('collected_date', ''),
            'domain': domain,
            'website': row.get('website', '') or row.get('domain', ''),
        }
        contacts = waterfall_enrich(
            domain=domain,
            company=company,
            officers=officers,
            hunter_key=hunter_key,
            skip_theharvester=skip_theharvester,
            skip_dorks=skip_dorks,
            company_meta=company_meta,
        )
        all_contacts.extend(contacts)
        time.sleep(1)  # be polite between companies
    # Write output
    fieldnames = ['email', 'first_name', 'last_name', 'company', 'title',
                  'source', 'is_dm', 'hunter_confidence',
                  'signal_tag', 'registered_date', 'collected_date', 'domain', 'website']
    with open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for c in all_contacts:
            writer.writerow(c)
    print(f"\n✅ Wrote {len(all_contacts)} contacts to {output_csv}")
    # Source breakdown
    from collections import Counter
    sources = Counter(c.get('source', '?') for c in all_contacts)
    print("\n📊 Source breakdown:")
    for src, count in sources.most_common():
        print(f"  {src}: {count}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Waterfall contact enrichment')
    parser.add_argument('--domain', help='Single domain to enrich')
    parser.add_argument('--company', help='Company name (single mode)', default='')
    parser.add_argument('--input', '-i', help='CSV input (batch mode)')
    parser.add_argument('--output', '-o', help='CSV output', default='contacts_raw.csv')
    parser.add_argument('--hunter-key', help='Hunter.io API key')
    parser.add_argument('--skip-theharvester', action='store_true')
    parser.add_argument('--skip-dorks', action='store_true')
    args = parser.parse_args()

    if args.input:
        batch_enrich(args.input, args.output, hunter_key=args.hunter_key,
                     skip_theharvester=args.skip_theharvester,
                     skip_dorks=args.skip_dorks)
    elif args.domain:
        contacts = waterfall_enrich(
            domain=args.domain, company=args.company,
            hunter_key=args.hunter_key,
            skip_theharvester=args.skip_theharvester,
            skip_dorks=args.skip_dorks,
        )
        for c in contacts:
            print(f"  {c['email']} ({c['source']}) — {c.get('first_name','')} {c.get('last_name','')}")
    else:
        parser.print_help()

if __name__ == '__main__':
    main()
