"""build_csv.py

Apply quality gates and build a HubSpot-ready CSV.

Input:  contacts_scored_qualified.csv (or any CSV with contact data)
Output: hubspot_import.csv with exact headers for HubSpot mapping

Quality gates:
  - Must have email + company
  - Generic emails (info@, hello@, etc.) → REJECT
  - Must have MX pass OR verification_score containing PASS
  - If signal_tag is blank, still include but tag as 'manual_review'
  - Must be able to derive a first name

This version aligns column names with the upstream pipeline:
  email, first_name, last_name, company, title, domain, signal_tag, source, etc.
"""
import csv
import argparse

from trillium_config import get_quality_floodgates

HUBSPOT_HEADERS = ['Email', 'First Name', 'Last Name', 'Company', 'Job Title',
                   'Phone', 'Website', 'City', 'Signal Tag', 'Confidence Level',
                   'LinkedIn URL', 'Notes']

GENERIC_LOCAL = ['info', 'hello', 'contact', 'admin', 'support', 'office',
                 'team', 'sales', 'noreply', 'mail', 'billing', 'hr',
                 'jobs', 'careers', 'press', 'media', 'marketing',
                 'webmaster', 'postmaster', 'abuse', 'security',
                 'reception', 'frontdesk', 'general', 'enquiries',
                 'inquiries', 'feedback', 'help', 'service', 'accounts']


def split_name(first_name, last_name, email):
    """Get first/last from explicit columns, then fall back to email local part."""
    first = (first_name or '').strip()
    last = (last_name or '').strip()
    if first:
        return first.capitalize(), last.capitalize() if last else ''
    # Fallback: parse email local part
    try:
        local = email.split('@', 1)[0]
        for sep in ['.', '_', '-']:
            if sep in local:
                parts = local.split(sep)
                return parts[0].capitalize(), parts[-1].capitalize()
        return local.capitalize(), ''
    except Exception:
        return '', ''


def is_generic(email):
    try:
        local = email.split('@', 1)[0].lower()
        return local in GENERIC_LOCAL
    except Exception:
        return True


def person_company_key(first_name, last_name, company):
    """Stable person+company key used to prevent duplicate outreach rows."""
    first = (first_name or '').strip().lower()
    last = (last_name or '').strip().lower()
    comp = (company or '').strip().lower()
    if not comp:
        return ''
    return f"{first}|{last}|{comp}"


def passes_quality(row, mode='strict_verify'):
    email = (row.get('email') or row.get('Email') or '').strip()
    company = (row.get('company') or row.get('Company') or '').strip()
    mx = (row.get('mx_pass') or '').upper()
    vscore = (row.get('verification_score') or '').upper()
    confidence = (row.get('confidence_level') or '').upper()
    source = (row.get('source') or '').lower().strip()
    smtp_ok = (row.get('smtp_ok') or '').upper()
    smtp_status = (row.get('smtp_status') or '').lower().strip()
    catch_all = (row.get('catch_all') or '').upper()
    smtp_accept = smtp_ok in ('ACCEPT', 'TRUE')
    source_sources = (row.get('source_sources') or '').strip()
    try:
        source_count = int(str(row.get('source_count') or '').strip() or 0)
    except ValueError:
        source_count = 0
    if source_count <= 0 and source_sources:
        source_count = len([s for s in source_sources.split(';') if s.strip()])

    floodgates = get_quality_floodgates()
    min_provisional_sources = int(floodgates.get('hosted_min_source_count_for_provisional_officer', 2))
    consensus_gate = bool(floodgates.get('enable_source_consensus_gate', True))

    if not email:
        return False, 'Missing email'
    if not company:
        return False, 'Missing company'
    if is_generic(email):
        return False, 'Generic email'
    # Accept if MX passed OR verification_score says PASS
    if mx != 'TRUE' and 'PASS' not in vscore:
        return False, 'MX check failed'
    # Accept A, B, and C confidence tiers for HubSpot import.
    # Ryan can filter by Confidence Level column in HubSpot.
    if confidence not in ('A', 'B'):
        return False, f'Low confidence ({confidence or "unknown"})'
    # strict_verify means mailbox-valid output for every exported row.
    if mode == 'strict_verify' and (not smtp_accept or catch_all == 'TRUE'):
        return False, 'Strict verify requires SMTP acceptance on a non-catch-all mailbox'
    # Officer permutation emails are guessed patterns.
    # strict_verify mode requires SMTP acceptance on non-catch-all domains.
    # hosted_discovery mode allows transport-level unknowns as provisional output.
    if source == 'officer_permutation':
        if smtp_accept and catch_all != 'TRUE':
            return True, ''
        if smtp_ok == 'REJECT':
            return False, 'Officer permutation mailbox rejected'
        if mode == 'hosted_discovery' and smtp_status in ('transport_blocked', 'mx_lookup_failed') and confidence in ('A', 'B'):
            if consensus_gate and source_count < min_provisional_sources:
                return False, f'Low source consensus ({source_count}) for provisional officer permutation'
            return True, ''
        return False, 'Officer permutation without SMTP acceptance'
    return True, ''


def build(input_csv, output_csv, reject_csv, mode='strict_verify'):
    try:
        with open(input_csv, newline='', encoding='utf-8') as fin:
            reader = csv.DictReader(fin)
            rows = list(reader)
            input_fields = reader.fieldnames or []
    except FileNotFoundError:
        print(f"\n⚠ Input file not found: {input_csv}")
        print(f"  (This usually means no contacts passed the scoring threshold)")
        # Create empty HubSpot file with headers so downstream doesn't break
        with open(output_csv, 'w', newline='', encoding='utf-8') as fout:
            writer = csv.DictWriter(fout, fieldnames=HUBSPOT_HEADERS)
            writer.writeheader()
        print(f"\n✅ HubSpot CSV built:")
        print(f"  Passed: 0 contacts → {output_csv}")
        print(f"  Rejected: 0 contacts → {reject_csv}")
        return

    passed = 0
    rejected = 0
    seen_emails = set()
    seen_people = set()

    with open(output_csv, 'w', newline='', encoding='utf-8') as fout, \
         open(reject_csv, 'w', newline='', encoding='utf-8') as frej:
        writer = csv.DictWriter(fout, fieldnames=HUBSPOT_HEADERS)
        # Ensure reject_reason column appears exactly once, at the end
        rej_fields = [f for f in input_fields if f != 'reject_reason'] + ['reject_reason']
        rej_writer = csv.DictWriter(frej, fieldnames=rej_fields, extrasaction='ignore')
        writer.writeheader()
        rej_writer.writeheader()

        for row in rows:
            ok, reason = passes_quality(row, mode=mode)
            if not ok:
                row['reject_reason'] = reason
                rej_writer.writerow(row)
                rejected += 1
                continue

            email = (row.get('email') or row.get('Email') or '').strip()
            first_name = row.get('first_name') or row.get('First Name') or ''
            last_name = row.get('last_name') or row.get('Last Name') or ''
            first, last = split_name(first_name, last_name, email)
            company = row.get('company') or row.get('Company') or ''

            if not first:
                row['reject_reason'] = 'Missing first name'
                rej_writer.writerow(row)
                rejected += 1
                continue

            email_key = email.lower()
            if email_key in seen_emails:
                row['reject_reason'] = 'Duplicate email'
                rej_writer.writerow(row)
                rejected += 1
                continue

            person_key = person_company_key(first, last, company)
            if person_key and person_key in seen_people:
                row['reject_reason'] = 'Duplicate person/company'
                rej_writer.writerow(row)
                rejected += 1
                continue

            signal = row.get('signal_tag') or row.get('Signal Tag') or ''
            domain = row.get('domain') or row.get('website') or row.get('Website') or ''

            out = {
                'Email': email,
                'First Name': first,
                'Last Name': last,
                'Company': company,
                'Job Title': row.get('title') or row.get('job_title') or row.get('Job Title') or '',
                'Phone': row.get('phone') or row.get('Phone') or '',
                'Website': domain,
                'City': row.get('city') or row.get('City') or 'Seattle',
                'Signal Tag': signal if signal else 'manual_review',
                'Confidence Level': row.get('confidence_level') or row.get('Confidence Level') or '',
                'LinkedIn URL': row.get('linkedin_url') or row.get('LinkedIn URL') or '',
                'Notes': row.get('notes') or row.get('Notes') or
                         row.get('score_breakdown') or '',
            }
            writer.writerow(out)
            seen_emails.add(email_key)
            if person_key:
                seen_people.add(person_key)
            passed += 1

    print(f"\n✅ HubSpot CSV built:")
    print(f"  Passed: {passed} contacts → {output_csv}")
    print(f"  Rejected: {rejected} contacts → {reject_csv}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', '-i', required=True)
    parser.add_argument('--output', '-o', required=True)
    parser.add_argument('--rejects', '-r', default='rejects.csv')
    parser.add_argument('--mode', choices=['strict_verify', 'hosted_discovery'], default='strict_verify',
                        help='strict_verify enforces mailbox validity; hosted_discovery allows provisional transport-blocked rows')
    args = parser.parse_args()
    build(args.input, args.output, args.rejects, mode=args.mode)

if __name__ == '__main__':
    main()
