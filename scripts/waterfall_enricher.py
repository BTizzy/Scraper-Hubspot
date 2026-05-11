"""waterfall_enricher.py

Multi-source contact discovery with waterfall / cascade logic.

Inspired by:
  • theHarvester — 30+ async discovery modules, unified parser, set-based dedup
  • h8mail — target_factory sequential chaining, "chasing" re-enrichment loop
  • Apollo — data contributor network + web crawling + third-party provider blend

Sources (in priority order — each one adds what previous ones missed):
    1. theHarvester        — email harvest from search engines (free, no key)
    2. Team page scraper   — crawl /about /team /staff pages for emails
    3. Sitewide scan       — bounded crawl of internal pages for direct/obfuscated emails
    4. Sitemap recent scan — prioritize recently updated public pages
    5. Wayback archive     — recover emails from archived public pages
    6. Officer permutation — take WA SOS / OpenCorporates officer names → email_permutator
    7. Hunter.io           — 25 free searches/month (needs API key)
    8. Google Dork scraper — "site:domain.com @domain.com" (experimental)

Each discovered email is tagged with its source so we can track which channel
yields the best results over time (Apollo does this to weight sources).

Usage:
  python waterfall_enricher.py --domain seattlestudio.com --company "Seattle Studio LLC"
  python waterfall_enricher.py --input enriched_companies.csv --output contacts_raw.csv
  python waterfall_enricher.py --input enriched_companies.csv --output contacts_raw.csv --hunter-key YOUR_KEY
"""
import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, UTC
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
    def parse_output(text: str):
        if not text:
            return
        name_email_re = re.compile(r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[<(]\s*(\S+@\S+\.\S+)\s*[>)]')
        email_re = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+' )
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

    try:
        backend_sources = (os.environ.get('THEHARVESTER_SOURCES', '') or '').strip()
        backend_arg = backend_sources if backend_sources else 'all'
        proc = subprocess.run(
            ['theHarvester', '-d', domain, '-b', backend_arg, '-l', '200'],
            capture_output=True, text=True, timeout=timeout
        )
        text = proc.stdout + '\n' + proc.stderr
        # Save raw output for debugging
        os.makedirs('waterfall_outputs', exist_ok=True)
        with open(f'waterfall_outputs/{domain}_theharvester.txt', 'w') as f:
            f.write(text)
        parse_output(text)
    except FileNotFoundError:
        print("  ⚠ theHarvester not installed, skipping source")
    except subprocess.TimeoutExpired as e:
        # Keep partial output when available so timeout doesn't mean zero yield.
        partial_text = ''
        if e.stdout:
            partial_text += e.stdout
        if e.stderr:
            partial_text += ('\n' + e.stderr)
        if partial_text:
            parse_output(partial_text)
            print(f"  ⚠ theHarvester timed out for {domain} (kept {len(results)} partial emails)")
        else:
            print(f"  ⚠ theHarvester timed out for {domain}")
    except Exception as e:
        print(f"  ⚠ theHarvester error for {domain}: {e}")
    return results


# ── Source 2: Team page scraper ────────────────────────────────────────────────

TEAM_PATHS = ['/about', '/team', '/our-team', '/staff', '/people', '/about-us',
              '/contact', '/leadership', '/who-we-are', '/meet-the-team']

TEAM_PATH_HINTS = ('team', 'staff', 'people', 'leadership', 'about', 'contact', 'careers', 'join')
RECENT_PAGE_HINTS = ('contact', 'team', 'staff', 'people', 'leadership', 'about', 'meet', 'join', 'careers')
DOCUMENT_PATH_HINTS = ('contact', 'team', 'staff', 'directory', 'capability', 'brochure', 'overview', 'about')
DOCUMENT_EXTENSIONS = ('.pdf', '.txt', '.vcf', '.csv')

NAME_STOP_TOKENS = {
    'about', 'automatic', 'blog', 'careers', 'comprehensive', 'contact', 'county',
    'electric', 'fixtures', 'garbage', 'generators', 'helpful', 'heaters', 'home',
    'leadership', 'our', 'people', 'plumbing', 'popular', 'privacy', 'services',
    'small', 'staff', 'systems', 'team', 'terms', 'tips', 'water',
}


def is_plausible_person_name(name: str) -> bool:
    """Heuristic filter for people names used in permutation generation."""
    raw = (name or '').strip()
    if not raw:
        return False
    if re.search(r"[^A-Za-z\s\-']", raw):
        return False

    parts = [p.strip(".,!?:;\"()[]{}") for p in raw.split() if p.strip()]
    if len(parts) < 2 or len(parts) > 4:
        return False

    token_re = re.compile(r"^[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?$")
    for part in parts:
        if not token_re.match(part):
            return False
        if part.lower() in NAME_STOP_TOKENS:
            return False

    return True


def normalize_name_token(token: str) -> str:
    """Keep only alphabetic characters for permutation input tokens."""
    return re.sub(r"[^A-Za-z]", "", (token or "")).lower()


def is_valid_business_email_format(email: str, domain: str) -> bool:
    """Accept only conservative business-email formats tied to domain."""
    if not email or ('@' not in email):
        return False
    expected_suffix = '@' + (domain or '').lower().strip()
    lower_email = email.lower().strip()
    if not expected_suffix or not lower_email.endswith(expected_suffix):
        return False
    local = lower_email.split('@', 1)[0]
    if not local or len(local) > 64:
        return False
    if local[0] in '.-_ ' or local[-1] in '.-_ ':
        return False
    if '..' in local or '__' in local or '--' in local:
        return False
    return bool(re.match(r'^[a-z0-9._-]+$', local))


def sanitize_email_candidate(email: str, domain: str) -> str:
    """Normalize common escaped-prefix artifacts before validation."""
    raw = (email or '').lower().strip().rstrip('.')
    if '@' not in raw:
        return ''
    local, host = raw.split('@', 1)
    if host != (domain or '').lower().strip():
        return raw
    local = re.sub(r'^(?:u00(?:3e|3c|26)|x(?:3e|3c|26)|gt;|lt;|amp;)+', '', local, flags=re.I)
    local = local.strip('>;<:"\'()[]{}')
    return f'{local}@{host}' if local else ''


def extract_emails_from_text(text: str, domain: str) -> set[str]:
    """Extract direct and lightly obfuscated emails for a specific domain."""
    found = set()
    if not text:
        return found

    direct_re = re.compile(rf'[\w.+-]+@{re.escape(domain)}', re.I)
    for m in direct_re.finditer(text):
        email = sanitize_email_candidate(m.group(), domain)
        if email:
            found.add(email)

    # Common obfuscations: jane [at] example.com, jane(at)example.com, jane at example.com
    obfuscated_re = re.compile(
        rf'([a-z0-9._%+-]{{1,64}})\s*(?:\[?\(?\s*at\s*\)?\]?|@)\s*{re.escape(domain)}',
        re.I,
    )
    for m in obfuscated_re.finditer(text):
        local = m.group(1).lower().strip('.-_')
        if local:
            email = sanitize_email_candidate(f'{local}@{domain}', domain)
            if email:
                found.add(email)

    return found


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse common sitemap / archive timestamps into timezone-aware datetimes."""
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        if len(raw) == 14 and raw.isdigit():
            return datetime.strptime(raw, '%Y%m%d%H%M%S').replace(tzinfo=UTC)
        normalized = raw.replace('Z', '+00:00')
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except Exception:
        return None


def parse_sitemap_xml(xml_text: str) -> tuple[list[dict], list[str]]:
    """Return (url_entries, child_sitemaps) from sitemap XML content."""
    if not xml_text or '<' not in xml_text:
        return [], []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []

    def strip_ns(tag: str) -> str:
        return tag.split('}', 1)[-1]

    url_entries = []
    child_sitemaps = []
    for node in root.iter():
        node_type = strip_ns(node.tag)
        if node_type not in ('url', 'sitemap'):
            continue
        loc = ''
        lastmod = ''
        for child in list(node):
            child_type = strip_ns(child.tag)
            text = (child.text or '').strip()
            if child_type == 'loc':
                loc = text
            elif child_type == 'lastmod':
                lastmod = text
        if not loc:
            continue
        if node_type == 'url':
            url_entries.append({'loc': loc, 'lastmod': lastmod})
        else:
            child_sitemaps.append(loc)
    return url_entries, child_sitemaps


def score_candidate_url(url: str, lastmod: str = '') -> int:
    """Heuristically rank URLs more likely to contain recent contact evidence."""
    parsed = urlparse(url)
    path = (parsed.path or '/').lower()
    score = 0
    for hint in RECENT_PAGE_HINTS:
        if hint in path:
            score += 20
    if path.endswith(('.pdf', '.txt', '.vcf')):
        score += 10
    if path in ('/', '/index.html', '/index.htm'):
        score += 5
    lastmod_dt = parse_iso_datetime(lastmod)
    if lastmod_dt is not None:
        age_days = max((datetime.now(UTC) - lastmod_dt).days, 0)
        if age_days <= 180:
            score += 20
        elif age_days <= 365:
            score += 12
        elif age_days <= 730:
            score += 5
    return score


def rank_candidate_urls(entries: list[dict], domain: str, max_urls: int = 12) -> list[dict]:
    """Rank and de-duplicate sitemap / archive URL candidates for a domain."""
    ranked = []
    seen = set()
    target_host = domain.lower().strip()
    for entry in entries:
        url = (entry.get('loc') or entry.get('url') or '').strip()
        if not url:
            continue
        parsed = urlparse(url)
        host = (parsed.netloc or '').lower().lstrip('www.')
        if host and host != target_host:
            continue
        normalized = url.split('#', 1)[0]
        if normalized in seen:
            continue
        seen.add(normalized)
        ranked.append({
            'loc': normalized,
            'lastmod': entry.get('lastmod', ''),
            'score': score_candidate_url(normalized, entry.get('lastmod', '')),
        })
    ranked.sort(key=lambda item: (-item['score'], item['loc']))
    return ranked[:max_urls]


def select_wayback_candidates(rows: list[dict], domain: str, max_urls: int = 8) -> list[dict]:
    """Select promising archived URLs from CDX rows for a domain."""
    candidates = []
    for row in rows:
        status = str(row.get('statuscode', '')).strip()
        mime = (row.get('mimetype', '') or '').lower()
        if status != '200':
            continue
        if mime and ('html' not in mime and 'text' not in mime and 'pdf' not in mime):
            continue
        original = (row.get('original') or '').strip()
        if not original:
            continue
        candidates.append({'loc': original, 'lastmod': row.get('timestamp', '')})
    return rank_candidate_urls(candidates, domain=domain, max_urls=max_urls)


def extract_emails_from_bytes(blob: bytes, domain: str) -> set[str]:
    """Decode arbitrary content best-effort and extract emails for the target domain."""
    if not blob:
        return set()
    text_parts = []
    pdf_text = extract_text_from_pdf_bytes(blob)
    if pdf_text:
        text_parts.append(pdf_text)
    text_parts.append(blob.decode('utf-8', errors='ignore'))
    text = '\n'.join(part for part in text_parts if part)
    return extract_emails_from_text(text, domain)


def extract_text_from_pdf_bytes(blob: bytes) -> str:
    """Extract searchable text from a small PDF payload when possible."""
    if not blob or b'%PDF' not in blob[:1024]:
        return ''
    try:
        from pypdf import PdfReader
    except Exception:
        return ''

    try:
        reader = PdfReader(io.BytesIO(blob))
        pages = []
        for page in reader.pages[:10]:
            page_text = page.extract_text() or ''
            if page_text:
                pages.append(page_text)
        return '\n'.join(pages)
    except Exception:
        return ''


def is_document_url(url: str) -> bool:
    """Return True when the URL looks like a linked public document."""
    lowered = (url or '').split('?', 1)[0].lower()
    if lowered.endswith(DOCUMENT_EXTENSIONS):
        return True
    return any(hint in lowered for hint in DOCUMENT_PATH_HINTS) and '.pdf' in lowered


def discover_document_urls(base_url: str, soup: BeautifulSoup, max_urls: int = 8) -> list[str]:
    """Discover likely public document URLs from page anchors."""
    discovered = []
    seen = set()
    base_host = urlparse(base_url).netloc

    for anchor in soup.find_all('a', href=True):
        href = (anchor.get('href') or '').strip()
        if not href or href.startswith('mailto:'):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc and parsed.netloc != base_host:
            continue
        normalized = full.split('#', 1)[0]
        if normalized in seen or not is_document_url(normalized):
            continue
        seen.add(normalized)
        discovered.append(normalized)
        if len(discovered) >= max_urls:
            break

    return discovered


def fetch_document_emails(document_urls: list[str], domain: str, source_hint: str,
                          headers: dict, timeout: int = 6, max_docs: int = 8) -> list[dict]:
    """Fetch linked public documents and extract target-domain emails."""
    results = []
    seen = set()

    for document_url in document_urls[:max_docs]:
        try:
            resp = requests.get(document_url, timeout=(3, timeout), headers=headers, allow_redirects=True)
            if resp.status_code != 200:
                continue
            for email in extract_emails_from_bytes(resp.content, domain):
                email = email.lower().strip().rstrip('.')
                if email in seen or ('@' + domain) not in email:
                    continue
                local = email.split('@')[0]
                if local in GENERIC_LOCAL_PARTS:
                    continue
                first, last = infer_name_from_email(email)
                results.append({
                    'email': email,
                    'first_name': first,
                    'last_name': last,
                    'source': source_hint,
                })
                seen.add(email)
        except Exception:
            continue
        time.sleep(0.1)

    return results


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
    document_urls = []

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
            for document_url in discover_document_urls(base_url, soup):
                if document_url not in document_urls:
                    document_urls.append(document_url)
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
                for document_url in discover_document_urls(resp.url or url, soup):
                    if document_url not in document_urls:
                        document_urls.append(document_url)
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

    for contact in fetch_document_emails(document_urls, domain, 'team_page', headers, timeout=timeout):
        add_contact(contact['email'], 'team_page')

    return results


def source_sitewide_scan(domain: str, timeout: int = 6, max_pages: int = 15) -> list[dict]:
    """Bounded internal crawl to find direct/obfuscated emails across normal pages."""
    results = []
    seen = set()
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
    document_urls = []

    def add_contact(email: str):
        email = email.lower().strip().rstrip('.')
        if email in seen:
            return
        if ('@' + domain) not in email:
            return
        local = email.split('@')[0]
        if local in GENERIC_LOCAL_PARTS:
            return
        first, last = infer_name_from_email(email)
        seen.add(email)
        results.append({
            'email': email,
            'first_name': first,
            'last_name': last,
            'source': 'site_scan',
        })

    base_url = ''
    home_html = ''
    for scheme in ('https', 'http'):
        try:
            resp = requests.get(f'{scheme}://{domain}', timeout=(3, timeout), headers=headers, allow_redirects=True)
            if resp.status_code == 200:
                base_url = resp.url or f'{scheme}://{domain}'
                home_html = resp.text or ''
                break
        except Exception:
            continue

    if not base_url:
        return results

    to_visit = [urlparse(base_url).path or '/']
    visited = set()

    # Seed with likely high-signal paths if homepage has sparse links.
    for seed in ['/contact', '/about', '/team', '/staff', '/people', '/leadership', '/careers']:
        if seed not in to_visit:
            to_visit.append(seed)

    while to_visit and len(visited) < max_pages:
        path = to_visit.pop(0)
        if path in visited:
            continue
        visited.add(path)

        page_url = urljoin(base_url, path)
        parsed_url = urlparse(page_url)
        if parsed_url.netloc and parsed_url.netloc != urlparse(base_url).netloc:
            continue

        try:
            resp = requests.get(page_url, timeout=(3, timeout), headers=headers, allow_redirects=True)
            if resp.status_code != 200:
                continue
            html = resp.text or ''

            for email in extract_emails_from_text(html, domain):
                add_contact(email)

            soup = BeautifulSoup(html, 'html.parser')
            for document_url in discover_document_urls(resp.url or page_url, soup):
                if document_url not in document_urls:
                    document_urls.append(document_url)
            for a in soup.find_all('a', href=True):
                href = (a.get('href') or '').strip()
                if not href:
                    continue
                if href.startswith('mailto:'):
                    email = href.replace('mailto:', '').split('?', 1)[0]
                    if email:
                        add_contact(email)
                    continue

                full = urljoin(base_url, href)
                p = urlparse(full)
                if p.netloc and p.netloc != urlparse(base_url).netloc:
                    continue
                next_path = p.path or '/'
                if next_path not in visited and next_path not in to_visit and len(to_visit) < max_pages * 2:
                    # Prefer likely people/contact pages first.
                    if any(h in next_path.lower() for h in TEAM_PATH_HINTS):
                        to_visit.insert(0, next_path)
                    else:
                        to_visit.append(next_path)
        except Exception:
            continue

        time.sleep(0.1)

    for contact in fetch_document_emails(document_urls, domain, 'site_scan', headers, timeout=timeout):
        add_contact(contact['email'])

    return results


def source_sitemap_recent(domain: str, timeout: int = 6, max_pages: int = 10) -> list[dict]:
    """Fetch sitemap URLs and scan recent high-signal pages for direct emails."""
    results = []
    seen = set()
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}

    def add_contact(email: str):
        email = email.lower().strip().rstrip('.')
        if email in seen or ('@' + domain) not in email:
            return
        local = email.split('@')[0]
        if local in GENERIC_LOCAL_PARTS:
            return
        first, last = infer_name_from_email(email)
        seen.add(email)
        results.append({
            'email': email,
            'first_name': first,
            'last_name': last,
            'source': 'sitemap_recent',
        })

    sitemap_urls = [f'https://{domain}/sitemap.xml', f'https://{domain}/sitemap_index.xml', f'http://{domain}/sitemap.xml']
    discovered_entries = []
    fetched_sitemaps = set()
    while sitemap_urls and len(fetched_sitemaps) < 4:
        sitemap_url = sitemap_urls.pop(0)
        if sitemap_url in fetched_sitemaps:
            continue
        fetched_sitemaps.add(sitemap_url)
        try:
            resp = requests.get(sitemap_url, timeout=(3, timeout), headers=headers, allow_redirects=True)
            if resp.status_code != 200:
                continue
            url_entries, child_sitemaps = parse_sitemap_xml(resp.text)
            discovered_entries.extend(url_entries)
            for child in child_sitemaps:
                if child not in fetched_sitemaps and len(sitemap_urls) < 8:
                    sitemap_urls.append(child)
        except Exception:
            continue

    for entry in rank_candidate_urls(discovered_entries, domain=domain, max_urls=max_pages):
        url = entry['loc']
        try:
            resp = requests.get(url, timeout=(3, timeout), headers=headers, allow_redirects=True)
            if resp.status_code != 200:
                continue
            content_type = (resp.headers.get('Content-Type') or '').lower()
            emails = extract_emails_from_bytes(resp.content, domain) if 'pdf' in content_type else extract_emails_from_text(resp.text, domain)
            for email in emails:
                add_contact(email)
        except Exception:
            continue
        time.sleep(0.1)

    return results


def source_wayback_archive(domain: str, timeout: int = 6, max_pages: int = 8) -> list[dict]:
    """Scan recent Internet Archive snapshots for direct emails on archived pages."""
    results = []
    seen = set()
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}

    def add_contact(email: str):
        email = email.lower().strip().rstrip('.')
        if email in seen or ('@' + domain) not in email:
            return
        local = email.split('@')[0]
        if local in GENERIC_LOCAL_PARTS:
            return
        first, last = infer_name_from_email(email)
        seen.add(email)
        results.append({
            'email': email,
            'first_name': first,
            'last_name': last,
            'source': 'wayback_archive',
        })

    try:
        resp = requests.get(
            'https://web.archive.org/cdx/search/cdx',
            params={
                'url': f'*.{domain}/*',
                'output': 'json',
                'fl': 'timestamp,original,statuscode,mimetype',
                'filter': 'statuscode:200',
                'from': '2023',
                'limit': '60',
                'collapse': 'urlkey',
            },
            timeout=(3, timeout),
            headers=headers,
        )
        rows = resp.json()
        if not rows or len(rows) < 2:
            return results
        headers_row = rows[0]
        records = [dict(zip(headers_row, row)) for row in rows[1:] if isinstance(row, list)]
    except Exception:
        return results

    for candidate in select_wayback_candidates(records, domain=domain, max_urls=max_pages):
        timestamp = candidate.get('lastmod', '')
        original = candidate.get('loc', '')
        if not timestamp or not original:
            continue
        archived_url = f'https://web.archive.org/web/{timestamp}id_/{original}'
        try:
            resp = requests.get(archived_url, timeout=(3, timeout), headers=headers, allow_redirects=True)
            if resp.status_code != 200:
                continue
            content_type = (resp.headers.get('Content-Type') or '').lower()
            emails = extract_emails_from_bytes(resp.content, domain) if 'pdf' in content_type else extract_emails_from_text(resp.text, domain)
            for email in emails:
                add_contact(email)
        except Exception:
            continue
        time.sleep(0.1)

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
        if not is_plausible_person_name(name):
            continue
        parts = name.strip().split()
        if len(parts) < 2:
            continue
        first = normalize_name_token(parts[0])
        last = normalize_name_token(parts[-1])
        if not first or not last:
            continue
        candidates = generate_permutations(first, last, domain)
        # Return top-3 most probable patterns (first.last, first, flast)
        for email in candidates[:3]:
            if not is_valid_business_email_format(email, domain):
                continue
            results.append({
                'email': email,
                'first_name': first.capitalize(),
                'last_name': last.capitalize(),
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
    """Deduplicate by email while preserving multi-source provenance."""
    by_email = {}
    for contact in contacts:
        email = contact.get('email', '').lower()
        if not email:
            continue
        src = (contact.get('source', '') or '').strip()
        if email not in by_email:
            merged = dict(contact)
            merged['email'] = email
            merged['source_sources'] = src if src else ''
            merged['source_count'] = 1 if src else 0
            by_email[email] = merged
            continue

        existing = by_email[email]
        # Preserve first source as canonical 'source', but merge provenance list.
        sources = {s for s in (existing.get('source_sources', '') or '').split(';') if s}
        if src:
            sources.add(src)
        existing['source_sources'] = ';'.join(sorted(sources))
        existing['source_count'] = len(sources)

        # Opportunistically fill missing identity fields from other sightings.
        for key in ('first_name', 'last_name', 'title', 'company', 'signal_tag'):
            if not existing.get(key) and contact.get(key):
                existing[key] = contact.get(key)

        # Keep strongest hunter confidence if present.
        try:
            existing_conf = float(existing.get('hunter_confidence') or 0)
            new_conf = float(contact.get('hunter_confidence') or 0)
            if new_conf > existing_conf:
                existing['hunter_confidence'] = new_conf
        except Exception:
            pass

    return list(by_email.values())


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
    # Source 3: Sitewide bounded scan
    print(f"  🔍 Source 3/8: Sitewide scan → {domain}")
    contacts = source_sitewide_scan(domain)
    print(f"    Found {len(contacts)} emails")
    all_contacts.extend(contacts)
    # Source 4: Sitemap-guided recent page scan
    print(f"  🔍 Source 4/8: Sitemap recent pages → {domain}")
    contacts = source_sitemap_recent(domain)
    print(f"    Found {len(contacts)} emails")
    all_contacts.extend(contacts)
    # Source 5: Archived page scan
    print(f"  🔍 Source 5/8: Wayback archive → {domain}")
    contacts = source_wayback_archive(domain)
    print(f"    Found {len(contacts)} emails")
    all_contacts.extend(contacts)
    # Source 6: Officer name permutation
    if officers:
        print(f"  🔍 Source 6/8: Officer permutation → {len(officers)} officers")
        contacts = source_officer_permutation(domain, officers)
        print(f"    Generated {len(contacts)} candidates")
        all_contacts.extend(contacts)
    else:
        print(f"  ⏭ Source 6/8: No officers provided, skipping")
    # Source 7: Hunter.io
    if hunter_key or os.environ.get('HUNTER_API_KEY'):
        print(f"  🔍 Source 7/8: Hunter.io → {domain}")
        contacts = source_hunter(domain, api_key=hunter_key)
        print(f"    Found {len(contacts)} emails")
        all_contacts.extend(contacts)
    else:
        print(f"  ⏭ Source 7/8: No Hunter API key, skipping")
    # Source 8: Google Dorks (experimental, low priority)
    if not skip_dorks:
        print(f"  🔍 Source 8/8: DuckDuckGo dork → {domain}")
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
                  'source', 'source_sources', 'source_count', 'is_dm', 'hunter_confidence',
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
