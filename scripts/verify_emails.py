"""verify_emails.py

Perform MX verification and generic address checks on candidate emails.

Input: contacts_raw.csv with column `email` and `company`.
Output: contacts_verified.csv with added columns: mx_pass (TRUE/FALSE), reject_reason, verification_score
"""
import csv
import argparse
import dns.resolver
import whois
import smtplib
import random
import string
from datetime import datetime

GENERIC_LOCAL = ['info', 'hello', 'contact', 'admin', 'support', 'office', 'team', 'sales', 'noreply', 'mail']

def verify_email_domain(email):
    """Check if domain can receive email: MX record first, then A record fallback (RFC 5321 §5)."""
    try:
        domain = email.split('@', 1)[1]
    except Exception:
        return False
    # Try MX records first
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        if len(answers) > 0:
            return True
    except Exception:
        pass
    # RFC 5321: if no MX record, fall back to A record — the domain itself acts as mail server
    # This is common for small businesses that use hosted email (Gmail, Outlook)
    try:
        answers = dns.resolver.resolve(domain, 'A')
        if len(answers) > 0:
            return True
    except Exception:
        pass
    return False

def is_generic(email):
    try:
        local = email.split('@', 1)[0].lower()
    except Exception:
        return True
    return local in GENERIC_LOCAL

def domain_age_days(domain):
    try:
        w = whois.whois(domain)
        cd = w.creation_date
        # creation_date can be list or datetime
        if isinstance(cd, list):
            cd = cd[0]
        if not cd:
            return None
        if isinstance(cd, str):
            try:
                cd = datetime.fromisoformat(cd)
            except Exception:
                return None
        delta = datetime.utcnow() - cd
        return delta.days
    except Exception:
        return None

def smtp_check(email, from_address='verify@example.com'):
    """Attempt SMTP RCPT check and catch-all detection.

    Returns: (smtp_ok, catch_all_flag)
      smtp_ok: True/False/None (None=couldn't determine)
      catch_all_flag: True/False/None (None=unknown)
    NOTE: Many mail servers block or greylist these probes. Use with care.
    """
    try:
        domain = email.split('@',1)[1]
    except Exception:
        return None, None
    try:
        mx_records = dns.resolver.resolve(domain, 'MX')
        # choose lowest preference
        mx = sorted(mx_records, key=lambda r: r.preference)[0].exchange.to_text().rstrip('.')
    except Exception:
        return None, None

    # helper to test an address
    def test_rcpt(addr):
        try:
            server = smtplib.SMTP(timeout=10)
            server.set_debuglevel(0)
            server.connect(mx)
            server.helo(server.local_hostname)
            server.mail(from_address)
            code, resp = server.rcpt(addr)
            server.quit()
            return code, resp
        except Exception:
            return None, None

    # test target
    tgt_code, _ = test_rcpt(email)
    # test random address to detect catch-all
    rand_local = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    rand_email = f"{rand_local}@{domain}"
    rand_code, _ = test_rcpt(rand_email)

    # Interpret codes
    def code_accepts(code):
        if code is None:
            return None
        return int(code) in (250, 251)

    tgt_accept = code_accepts(tgt_code)
    rand_accept = code_accepts(rand_code)

    # Determine catch-all
    if rand_accept is True and tgt_accept is True:
        return True, True
    if rand_accept is False and tgt_accept is True:
        return True, False
    if tgt_accept is False:
        return False, False
    return None, None

def score_email(email, smtp=False):
    if is_generic(email):
        return 'REJECT — generic address', None, None, None, None
    mx = verify_email_domain(email)
    domain = email.split('@',1)[1] if '@' in email else ''
    age = None
    try:
        age = domain_age_days(domain)
    except Exception:
        age = None
    smtp_ok = None
    catch_all = None
    if smtp:
        try:
            smtp_ok, catch_all = smtp_check(email)
        except Exception:
            smtp_ok, catch_all = None, None

    if not mx:
        return 'REJECT — dead domain', mx, age, smtp_ok, catch_all
    return 'PASS', mx, age, smtp_ok, catch_all

def verify(input_csv, output_csv, smtp=False):
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = reader.fieldnames + ['mx_pass', 'reject_reason', 'verification_score', 'domain_age_days', 'smtp_ok', 'catch_all']
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            email = row.get('email', '').strip()
            if not email:
                row['mx_pass'] = 'FALSE'
                row['reject_reason'] = 'Missing email'
                row['verification_score'] = 'REJECT — missing'
                row['domain_age_days'] = ''
                row['smtp_ok'] = ''
                row['catch_all'] = ''
                writer.writerow(row)
                continue
            score, mx, age, smtp_ok, catch_all = score_email(email, smtp=smtp)
            row['verification_score'] = score
            row['domain_age_days'] = age if age is not None else ''
            row['smtp_ok'] = str(smtp_ok) if smtp_ok is not None else ''
            row['catch_all'] = str(catch_all) if catch_all is not None else ''
            if score.startswith('PASS'):
                row['mx_pass'] = 'TRUE'
                row['reject_reason'] = ''
            else:
                row['mx_pass'] = 'FALSE'
                row['reject_reason'] = score
            writer.writerow(row)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', '-i', required=True)
    parser.add_argument('--output', '-o', required=True)
    parser.add_argument('--smtp', action='store_true', help='Enable SMTP RCPT checks (may be blocked by mail servers)')
    args = parser.parse_args()
    verify(args.input, args.output, smtp=args.smtp)

if __name__ == '__main__':
    main()
