#!/Users/ryanbartell/.pyenv/versions/3.13.2/bin/python3
"""
Clawd Direct Lead Gen — Seattle/King County Professional Services
Uses web_search + web_fetch to find companies and contacts directly.
No Comet/Perplexity AppleScript dependency.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
PIPELINE_DIR = Path.home() / "lead-generation-automation"
OUTPUT_DIR = PIPELINE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_CSV = OUTPUT_DIR / "leads_seattle_fresh.csv"

# ── Target Criteria ───────────────────────────────────────────────────────────
TARGET_INDUSTRIES = [
    "Law Firms",
    "Legal Services",
    "Accounting",
    "CPA",
    "Consulting",
    "Management Consulting",
    "Financial Advisory",
    "Financial Services",
    "Real Estate",
    "Architecture",
    "Engineering",
    "IT Services",
    "Marketing Agency",
]

TARGET_LOCATIONS = [
    "Seattle", "King County", "Bellevue", "Redmond",
    "Kirkland", "Issaquah", "Kent", "Renton",
]

TARGET_TITLES = [
    "Owner", "President", "CEO", "Managing Partner",
    "Founder", "Partner", "Senior Partner", "Principal",
    "Managing Director", "COO", "CFO",
]

COMPANY_SIZE_RANGE = (5, 25)

EXCLUDED_INDUSTRIES = [
    "HR Services", "Human Resources", "Staffing",
    "Recruiting", "Recruitment Agency", "Talent Agency",
    "Executive Search", "PEO",
]

# ── Telegram ───────────────────────────────────────────────────────────────────
def tg_send(message: str):
    try:
        config_path = Path.home() / ".openclaw/openclaw.json"
        if not config_path.exists():
            return
        config = json.loads(config_path.read_text())
        token = config["channels"]["telegram"]["botToken"]
        allow_from = config["channels"]["telegram"].get("allowFrom", [])
        if allow_from:
            chat_id = allow_from[0]
        else:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.load(resp)
            for update in reversed(data.get("result", [])):
                if "message" in update:
                    chat_id = str(update["message"]["chat"]["id"])
                    break
            else:
                return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id, "text": message, "parse_mode": "Markdown"
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[tg] Send failed: {e}")


# ── Web Search via DuckDuckGo (no API key needed) ────────────────────────────
def ddg_search(query: str, max_results: int = 10) -> list:
    """Search DuckDuckGo HTML and return results."""
    import urllib.parse
    results = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Parse result snippets
        from html.parser import HTMLParser

        # Simple regex-based extraction
        result_blocks = re.findall(
            r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>',
            html
        )
        snippets = re.findall(
            r'<a rel="nofollow" class="result__snippet"[^>]*>(.*?)</a>',
            html
        )

        for i, (link, title) in enumerate(result_blocks):
            if i >= max_results:
                break
            # Clean HTML tags from title
            clean_title = re.sub(r'<[^>]+>', '', title).strip()
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            if clean_title:
                results.append({
                    "title": clean_title,
                    "url": link,
                    "snippet": snippet
                })
    except Exception as e:
        print(f"[ddg] Search failed: {e}")
    return results


# ── Fetch page content ───────────────────────────────────────────────────────
def fetch_page(url: str, max_chars: int = 5000) -> str:
    """Fetch a web page and return text content."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # Strip tags
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception as e:
        print(f"[fetch] Failed {url}: {e}")
        return ""


# ── Extract emails from text ─────────────────────────────────────────────────
def extract_emails(text: str) -> list:
    """Extract email addresses from text, filtering out obvious fakes."""
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = list(set(re.findall(pattern, text)))
    # Filter
    filtered = []
    skip_domains = {'example.com', 'test.com', 'sentry.io', 'wixpress.com', 'google.com', 'facebook.com'}
    for e in emails:
        domain = e.split('@')[1].lower()
        if domain not in skip_domains and not e.startswith('noreply@') and not e.startswith('no-reply@'):
            filtered.append(e.lower())
    return filtered


# ── Extract phone numbers ────────────────────────────────────────────────────
def extract_phones(text: str) -> list:
    """Extract US phone numbers from text."""
    pattern = r'(?:\+1[-.\s]?)?\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})'
    matches = re.findall(pattern, text)
    phones = []
    for m in matches:
        phone = f"({m[0]}) {m[1]}-{m[2]}"
        if phone not in phones:
            phones.append(phone)
    return phones


# ── Extract names near titles ────────────────────────────────────────────────
def extract_names_with_titles(text: str, titles: list) -> list:
    """Find names that appear near target job titles."""
    results = []
    lines = text.split('\n')
    title_pattern = '|'.join(re.escape(t) for t in titles)

    for i, line in enumerate(lines):
        line = line.strip()
        if not line or len(line) > 200:
            continue
        if re.search(title_pattern, line, re.IGNORECASE):
            # Check nearby lines for names
            for offset in [-2, -1, 1, 2]:
                j = i + offset
                if 0 <= j < len(lines):
                    nearby = lines[j].strip()
                    name_match = re.match(r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})$', nearby)
                    if name_match:
                        name = name_match.group(1)
                        parts = name.split()
                        if len(parts) >= 2:
                            results.append({
                                'first_name': parts[0],
                                'last_name': ' '.join(parts[1:]),
                                'title': re.search(title_pattern, line, re.IGNORECASE).group(0)
                            })
    return results


# ── Email verification (DNS/MX only, fast) ───────────────────────────────────
def verify_email_dns(email: str) -> dict:
    """Quick email verification via DNS/MX lookup."""
    import dns.resolver
    result = {"email": email, "is_valid": False, "confidence": 0, "checks": {}}
    try:
        # Syntax
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            result["checks"]["syntax"] = False
            return result
        result["checks"]["syntax"] = True
        result["confidence"] = 20

        # Disposable check
        disposable = {'tempmail.com', '10minutemail.com', 'guerrillamail.com', 'mailinator.com', 'yopmail.com'}
        domain = email.split('@')[1]
        if domain in disposable:
            result["checks"]["disposable"] = True
            return result
        result["checks"]["disposable"] = False
        result["confidence"] = 35

        # MX lookup
        try:
            mx_records = dns.resolver.resolve(domain, 'MX', lifetime=3)
            if mx_records:
                result["checks"]["dns"] = True
                result["confidence"] = 70
                result["is_valid"] = True
            else:
                result["checks"]["dns"] = False
        except:
            result["checks"]["dns"] = False

    except Exception as e:
        result["error"] = str(e)

    return result


# ── Generate email permutations ──────────────────────────────────────────────
def gen_emails(first: str, last: str, domain: str) -> list:
    """Generate likely email addresses."""
    f = first.lower().replace(' ', '')
    l = last.lower().replace(' ', '')
    d = domain.replace('http://', '').replace('https://', '').replace('www.', '').split('/')[0]
    emails = [
        f"{f}.{l}@{d}",
        f"{f}{l}@{d}",
        f"{f[0]}{l}@{d}",
        f"{f}@{d}",
        f"{f}.{l[0]}@{d}",
        f"{f[0]}.{l}@{d}",
    ]
    return list(dict.fromkeys(emails))  # dedupe preserving order


# ── Main pipeline ─────────────────────────────────────────────────────────────
def find_companies_for_industry(industry: str, location: str, max_results: int = 15) -> list:
    """Search for companies matching criteria."""
    companies = []

    # Search queries designed to find small professional services firms
    queries = [
        f"{industry} firms {location} Washington 5-25 employees",
        f"small {industry} companies {location} WA",
        f"{industry} {location} Washington state small business",
        f"top {industry} firms {location} Washington",
    ]

    seen_domains = set()
    for query in queries:
        results = ddg_search(query, max_results=8)
        for r in results:
            url = r.get('url', '')
            title = r.get('title', '')
            snippet = r.get('snippet', '')

            # Extract domain
            domain_match = re.search(r'https?://(?:www\.)?([^/]+)', url)
            if not domain_match:
                continue
            domain = domain_match.group(1)

            # Skip if already seen
            if domain in seen_domains:
                continue
            seen_domains.add(domain)

            # Skip excluded
            skip = False
            for excl in ['linkedin.com', 'facebook.com', 'yelp.com', 'bbb.org',
                         'yellowpages.com', 'indeed.com', 'glassdoor.com',
                         'crunchbase.com', 'bloomberg.com', 'wikipedia.org',
                         'amazon.com', 'google.com', 'apple.com']:
                if excl in domain:
                    skip = True
                    break
            if skip:
                continue

            # Clean company name from title
            name = title.split(' - ')[0].split(' | ')[0].strip()
            name = re.sub(r'\s*[-|]\s*(LinkedIn|Facebook|Yelp|BBB).*', '', name, flags=re.IGNORECASE).strip()

            if len(name) < 3 or len(name) > 80:
                continue

            companies.append({
                'name': name,
                'domain': domain,
                'website': f"https://{domain}" if not url.startswith('http') else url.split(domain)[0] + domain,
                'industry': industry,
                'location': location,
                'source_url': url,
                'snippet': snippet,
            })

        if len(companies) >= max_results:
            break

    return companies[:max_results]


def find_contacts_at_company(company: dict) -> list:
    """Find decision-maker contacts at a company."""
    contacts = []
    domain = company['domain']
    name = company['name']

    # Strategy 1: Fetch the website and look for team/about pages
    team_paths = ['/team', '/about', '/about-us', '/leadership', '/partners', '/attorneys', '/staff', '/people']
    base_url = f"https://{domain}"

    # First try the homepage
    home_text = fetch_page(base_url, max_chars=4000)
    home_emails = extract_emails(home_text)
    home_phones = extract_phones(home_text)
    home_names = extract_names_with_titles(home_text, TARGET_TITLES)

    for n in home_names:
        contact = {**n, 'company': name, 'domain': domain, 'source': 'homepage'}
        if home_emails:
            contact['email'] = home_emails[0]
        if home_phones:
            contact['phone'] = home_phones[0]
        contacts.append(contact)

    # Try team pages
    for path in team_paths:
        if len(contacts) >= 3:
            break
        url = base_url + path
        text = fetch_page(url, max_chars=5000)
        if not text:
            continue

        emails = extract_emails(text)
        phones = extract_phones(text)
        names = extract_names_with_titles(text, TARGET_TITLES)

        for n in names:
            # Check if we already have this person
            already = any(
                c['first_name'] == n['first_name'] and c['last_name'] == n['last_name']
                for c in contacts
            )
            if already:
                continue
            contact = {**n, 'company': name, 'domain': domain, 'source': f'website:{path}'}
            if emails:
                contact['email'] = emails[0]
                if len(emails) > 1:
                    contact['email_alternatives'] = emails[1:]
            if phones:
                contact['phone'] = phones[0]
            contacts.append(contact)

    # Strategy 2: Search for company leadership via DDG
    if len(contacts) < 2:
        queries = [
            f"{name} {company.get('location', 'Seattle')} owner president CEO",
            f"{name} managing partner founder contact",
            f"site:linkedin.com/in \"{name}\" partner president owner",
        ]
        for query in queries:
            if len(contacts) >= 3:
                break
            results = ddg_search(query, max_results=5)
            for r in results:
                snippet = r.get('snippet', '') + ' ' + r.get('title', '')
                emails = extract_emails(snippet)
                phones = extract_phones(snippet)
                names = extract_names_with_titles(snippet + '\n' + r.get('title', ''), TARGET_TITLES)

                for n in names:
                    already = any(
                        c['first_name'] == n['first_name'] and c['last_name'] == n['last_name']
                        for c in contacts
                    )
                    if already:
                        continue
                    contact = {
                        **n, 'company': name, 'domain': domain,
                        'source': 'search_engine'
                    }
                    if emails:
                        contact['email'] = emails[0]
                    if phones:
                        contact['phone'] = phones[0]
                    contacts.append(contact)

                if len(contacts) >= 3:
                    break

    return contacts[:5]  # Max 5 contacts per company


def process_lead(contact: dict) -> dict:
    """Process a contact: generate emails, verify, build lead record."""
    first = contact.get('first_name', '')
    last = contact.get('last_name', '')
    domain = contact.get('domain', '')

    if not first or not last or not domain:
        return None

    # If we already have an email from scraping, verify it
    existing_email = contact.get('email', '')
    if existing_email:
        verification = verify_email_dns(existing_email)
        if verification['is_valid']:
            contact['email'] = existing_email
            contact['confidence_score'] = min(verification['confidence'] + 20, 95)
        else:
            # Try generating permutations
            emails = gen_emails(first, last, domain)
            best_email = None
            best_conf = 0
            for email in emails:
                v = verify_email_dns(email)
                if v['is_valid'] and v['confidence'] > best_conf:
                    best_email = email
                    best_conf = v['confidence']
            if best_email:
                contact['email'] = best_email
                contact['confidence_score'] = best_conf
            else:
                contact['email'] = emails[0] if emails else ''
                contact['confidence_score'] = 30
    else:
        # Generate and verify
        emails = gen_emails(first, last, domain)
        best_email = None
        best_conf = 0
        for email in emails:
            v = verify_email_dns(email)
            if v['is_valid'] and v['confidence'] > best_conf:
                best_email = email
                best_conf = v['confidence']
        if best_email:
            contact['email'] = best_email
            contact['confidence_score'] = best_conf
        elif emails:
            contact['email'] = emails[0]
            contact['confidence_score'] = 25
        else:
            return None

    return contact


def run(industries: list, locations: list, target_count: int):
    """Main pipeline."""
    started = datetime.now()
    print(f"\n{'='*60}")
    print(f"🦞 CLAWD LEAD GEN — Seattle/King County Professional Services")
    print(f"{'='*60}")
    print(f"Industries: {', '.join(industries)}")
    print(f"Locations:  {', '.join(locations)}")
    print(f"Target:     {target_count} leads")
    print(f"{'='*60}\n")

    tg_send(
        f"🦞 *Lead Gen Started*\n"
        f"Target: {target_count} leads\n"
        f"Industries: {', '.join(industries[:4])}...\n"
        f"Locations: {', '.join(locations[:3])}...\n\n"
        f"Working on it — will ping you when done."
    )

    all_leads = []
    seen_emails = set()
    seen_companies = set()

    # Phase 1: Company Discovery
    print("\n📊 PHASE 1: Company Discovery")
    print("-" * 40)
    all_companies = []
    for industry in industries:
        for location in locations[:4]:  # Limit locations for speed
            if len(all_companies) >= target_count * 3:
                break
            print(f"  Searching: {industry} in {location}...")
            companies = find_companies_for_industry(industry, location, max_results=8)
            for c in companies:
                if c['name'] not in seen_companies:
                    seen_companies.add(c['name'])
                    all_companies.append(c)
            print(f"    Found {len(companies)} companies (total: {len(all_companies)})")
            time.sleep(1)  # Rate limit

    print(f"\n  Total companies discovered: {len(all_companies)}")

    # Phase 2: Contact Discovery + Email
    print(f"\n👤 PHASE 2: Contact Discovery & Email Generation")
    print("-" * 40)

    for i, company in enumerate(all_companies):
        if len(all_leads) >= target_count:
            break

        print(f"\n  [{i+1}/{len(all_companies)}] {company['name']}")
        contacts = find_contacts_at_company(company)
        print(f"    Found {len(contacts)} contacts")

        for contact in contacts:
            if len(all_leads) >= target_count:
                break

            lead = process_lead(contact)
            if not lead:
                continue

            email = lead.get('email', '')
            if email in seen_emails:
                continue
            if not email:
                continue

            seen_emails.add(email)

            # Build final record
            record = {
                'First Name': lead.get('first_name', ''),
                'Last Name': lead.get('last_name', ''),
                'Email': email,
                'Company': lead.get('company', ''),
                'Title': lead.get('title', ''),
                'Phone': lead.get('phone', ''),
                'Website': f"https://{lead.get('domain', '')}",
                'Company Size': f"{COMPANY_SIZE_RANGE[0]}-{COMPANY_SIZE_RANGE[1]}",
                'Industry': company.get('industry', ''),
                'Location': company.get('location', ''),
                'Confidence Score': lead.get('confidence_score', 0),
                'Source': lead.get('source', ''),
                'Last Verified': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            all_leads.append(record)
            print(f"    ✅ {record['First Name']} {record['Last Name']} — {email} ({record['Title']})")

        time.sleep(0.5)

    # Phase 3: Export
    print(f"\n📤 PHASE 3: Export")
    print("-" * 40)

    if not all_leads:
        print("  No leads found!")
        tg_send("⚠️ Lead gen finished but found 0 leads. Try broadening criteria.")
        return

    # Write CSV
    fieldnames = [
        'First Name', 'Last Name', 'Email', 'Company', 'Title',
        'Phone', 'Website', 'Company Size', 'Industry', 'Location',
        'Confidence Score', 'Source', 'Last Verified'
    ]

    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_leads)

    elapsed = int((datetime.now() - started).total_seconds())

    # Summary
    print(f"\n{'='*60}")
    print(f"✅ DONE — {len(all_leads)} leads in {elapsed}s")
    print(f"📄 CSV: {OUTPUT_CSV}")
    print(f"{'='*60}")

    # Print top leads
    for lead in all_leads[:10]:
        print(f"  • {lead['First Name']} {lead['Last Name']} — {lead['Title']} @ {lead['Company']}")
        print(f"    📧 {lead['Email']}")
        if lead.get('Phone'):
            print(f"    📞 {lead['Phone']}")
        print()

    # Telegram summary
    summary_lines = []
    for lead in all_leads[:8]:
        line = f"• {lead['First Name']} {lead['Last Name']} — {lead['Title']} @ {lead['Company']}"
        line += f"\n  📧 {lead['Email']}"
        summary_lines.append(line)

    more = f"\n_+{len(all_leads) - 8} more in CSV_" if len(all_leads) > 8 else ""

    tg_send(
        f"✅ *{len(all_leads)} leads found* ({elapsed}s)\n\n"
        + "\n\n".join(summary_lines)
        + more
        + f"\n\n📄 CSV: `{OUTPUT_CSV}`"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clawd Direct Lead Gen")
    parser.add_argument("--industries", nargs="+", default=TARGET_INDUSTRIES[:4])
    parser.add_argument("--locations", nargs="+", default=TARGET_LOCATIONS[:4])
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    run(args.industries, args.locations, args.limit)
