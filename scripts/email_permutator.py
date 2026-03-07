"""email_permutator.py

Generate candidate email addresses from a person's name + company domain.

Inspired by how Apollo, Hunter.io, and Clearbit work:
  1. Take known first/last name from team pages, LinkedIn, or officer records
  2. Generate all common B2B email patterns ({first}.{last}@, {fi}{last}@, etc.)
  3. MX-verify each candidate against the domain
  4. Score by pattern prevalence (first.last@ is most common in B2B)

This is the #1 gap vs paid tools — Apollo has 265M contacts because they
permutate + verify at scale. This script gives us the same logic, just slower.

Usage:
  python email_permutator.py --first Jane --last Doe --domain seattlestudio.com
  python email_permutator.py --input officers.csv --output permuted_emails.csv
"""
import argparse
import csv
import dns.resolver
import smtplib
import time
import sys

from trillium_config import EMAIL_PATTERNS, GENERIC_LOCAL_PARTS

# ── Pattern engine ─────────────────────────────────────────────────────────────

def generate_permutations(first: str, last: str, domain: str) -> list[str]:
    """Generate candidate emails from name + domain using common B2B patterns."""
    first = first.strip().lower()
    last = last.strip().lower()
    domain = domain.strip().lower()
    if not first or not domain:
        return []
    fi = first[0]
    li = last[0] if last else ''
    candidates = []
    for pattern in EMAIL_PATTERNS:
        try:
            email = pattern.format(
                first=first, last=last, fi=fi, li=li, domain=domain
            )
            # skip if pattern produced empty segments (e.g., no last name)
            local = email.split('@')[0]
            if local and '..' not in local and local not in GENERIC_LOCAL_PARTS:
                candidates.append(email)
        except (KeyError, IndexError):
            continue
    return list(dict.fromkeys(candidates))  # dedupe, preserve order


# ── Verification ───────────────────────────────────────────────────────────────

def domain_has_mx(domain: str) -> bool:
    """Check if a domain has valid MX records."""
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        return len(answers) > 0
    except Exception:
        return False


def smtp_verify(email: str, mx_host: str = None, timeout: int = 10) -> str:
    """
    SMTP RCPT-TO probe. Returns:
      'ACCEPT' — server accepted the recipient
      'REJECT' — server rejected (550/551/553)
      'UNKNOWN' — timeout, connection refused, or inconclusive
    """
    domain = email.split('@')[1]
    if mx_host is None:
        try:
            records = dns.resolver.resolve(domain, 'MX')
            mx_host = sorted(records, key=lambda r: r.preference)[0].exchange.to_text().rstrip('.')
        except Exception:
            return 'UNKNOWN'
    try:
        srv = smtplib.SMTP(timeout=timeout)
        srv.connect(mx_host)
        srv.helo('verify.local')
        srv.mail('test@verify.local')
        code, _ = srv.rcpt(email)
        srv.quit()
        if code in (250, 251):
            return 'ACCEPT'
        elif code in (550, 551, 553, 554):
            return 'REJECT'
        return 'UNKNOWN'
    except Exception:
        return 'UNKNOWN'


def detect_catchall(domain: str, mx_host: str = None, timeout: int = 10) -> bool | None:
    """Send a random address to the domain to detect catch-all. Returns True/False/None."""
    import random, string
    rand_local = ''.join(random.choices(string.ascii_lowercase + string.digits, k=14))
    result = smtp_verify(f"{rand_local}@{domain}", mx_host=mx_host, timeout=timeout)
    if result == 'ACCEPT':
        return True   # catch-all
    elif result == 'REJECT':
        return False   # not catch-all — good, individual verification works
    return None


# ── Scoring ────────────────────────────────────────────────────────────────────

# Pattern prevalence weights (higher = more common in B2B world)
PATTERN_WEIGHTS = {
    "{first}.{last}@{domain}": 1.0,
    "{first}@{domain}": 0.85,
    "{fi}{last}@{domain}": 0.80,
    "{first}{last}@{domain}": 0.75,
    "{first}_{last}@{domain}": 0.60,
    "{fi}.{last}@{domain}": 0.55,
    "{first}{li}@{domain}": 0.50,
    "{last}@{domain}": 0.40,
    "{first}.{li}@{domain}": 0.35,
    "{fi}{li}@{domain}": 0.25,
}


def score_candidate(email: str, first: str, last: str, domain: str,
                    smtp_result: str = 'UNKNOWN', is_catchall: bool | None = None) -> float:
    """
    Score a candidate email 0.0–1.0.
    Factors: pattern prevalence, SMTP acceptance, catch-all status.
    """
    score = 0.0
    fi = first[0].lower() if first else ''
    li = last[0].lower() if last else ''
    local = email.split('@')[0].lower()
    # Find which pattern this email matches
    for pattern, weight in PATTERN_WEIGHTS.items():
        try:
            expected_local = pattern.split('@')[0].format(
                first=first.lower(), last=last.lower(), fi=fi, li=li
            )
            if local == expected_local:
                score = weight
                break
        except Exception:
            continue
    # Adjust for SMTP result
    if smtp_result == 'ACCEPT':
        if is_catchall:
            score *= 0.6   # catch-all dampens confidence
        else:
            score = min(1.0, score + 0.15)
    elif smtp_result == 'REJECT':
        score = 0.0  # definitely bad
    # MX check is implicit (we only get here if domain has MX)
    return round(score, 3)


# ── Batch mode ─────────────────────────────────────────────────────────────────

def permutate_batch(input_csv: str, output_csv: str, verify: bool = False):
    """
    Read a CSV with columns: first_name (or name), last_name, domain (or website).
    Write candidate emails with scores.
    """
    with open(input_csv, newline='', encoding='utf-8') as fin, \
         open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = ['first_name', 'last_name', 'domain', 'email', 'pattern_score',
                       'smtp_result', 'catch_all', 'final_score', 'company']
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            first = row.get('first_name') or row.get('first') or ''
            last = row.get('last_name') or row.get('last') or ''
            domain = row.get('domain') or row.get('website') or ''
            company = row.get('company') or row.get('company_name') or ''
            # split full name if needed
            if not first and row.get('name'):
                parts = row['name'].strip().split()
                first = parts[0] if parts else ''
                last = ' '.join(parts[1:]) if len(parts) > 1 else ''
            if not first or not domain:
                continue
            # Clean domain
            if domain.startswith('http'):
                from urllib.parse import urlparse
                domain = urlparse(domain).netloc
            if not domain_has_mx(domain):
                print(f"  ✗ {domain}: no MX records, skipping")
                continue
            candidates = generate_permutations(first, last, domain)
            # Optional: detect catch-all once per domain
            is_catchall = None
            mx_host = None
            if verify:
                try:
                    records = dns.resolver.resolve(domain, 'MX')
                    mx_host = sorted(records, key=lambda r: r.preference)[0].exchange.to_text().rstrip('.')
                except Exception:
                    pass
                is_catchall = detect_catchall(domain, mx_host=mx_host)
                if is_catchall:
                    print(f"  ⚠ {domain}: catch-all detected")
            for email in candidates:
                smtp_result = 'UNKNOWN'
                if verify and mx_host:
                    smtp_result = smtp_verify(email, mx_host=mx_host)
                    time.sleep(0.3)  # be polite
                final = score_candidate(email, first, last, domain,
                                         smtp_result=smtp_result, is_catchall=is_catchall)
                writer.writerow({
                    'first_name': first,
                    'last_name': last,
                    'domain': domain,
                    'email': email,
                    'pattern_score': PATTERN_WEIGHTS.get(
                        next((p for p in PATTERN_WEIGHTS if email == p.format(
                            first=first.lower(), last=last.lower(),
                            fi=first[0].lower(), li=(last[0].lower() if last else ''),
                            domain=domain
                        )), ''), 0),
                    'smtp_result': smtp_result,
                    'catch_all': str(is_catchall) if is_catchall is not None else '',
                    'final_score': final,
                    'company': company,
                })
            time.sleep(0.2)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Email permutation engine')
    parser.add_argument('--first', help='First name')
    parser.add_argument('--last', help='Last name', default='')
    parser.add_argument('--domain', help='Company domain')
    parser.add_argument('--input', '-i', help='CSV input (batch mode)')
    parser.add_argument('--output', '-o', help='CSV output (batch mode)')
    parser.add_argument('--verify', action='store_true', help='Run SMTP verification on candidates')
    args = parser.parse_args()

    if args.input and args.output:
        permutate_batch(args.input, args.output, verify=args.verify)
    elif args.first and args.domain:
        candidates = generate_permutations(args.first, args.last, args.domain)
        print(f"Generated {len(candidates)} candidates for {args.first} {args.last} @ {args.domain}:")
        for c in candidates:
            print(f"  {c}")
    else:
        parser.print_help()

if __name__ == '__main__':
    main()
