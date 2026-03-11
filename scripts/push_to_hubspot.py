"""push_to_hubspot.py

Optional: Push hubspot_import.csv contacts to HubSpot via API v3.

Requires HUBSPOT_API_KEY env var (private app access token).
Currently the Trillium account has CONTACT WRITE: NOT_AVAILABLE,
so this script will fail until write access is reauthorized.

Usage:
  python push_to_hubspot.py --input hubspot_import.csv
  python push_to_hubspot.py --input hubspot_import.csv --dry-run
"""
import argparse
import csv
import json
import os
import sys
import time

import requests

from local_secrets import load_local_env

load_local_env()

HUBSPOT_API_BASE = 'https://api.hubapi.com/crm/v3/objects/contacts'

# Map CSV headers to HubSpot property internal names
FIELD_MAP = {
    'Email': 'email',
    'First Name': 'firstname',
    'Last Name': 'lastname',
    'Company': 'company',
    'Job Title': 'jobtitle',
    'Phone': 'phone',
    'Website': 'website',
    'City': 'city',
    'Signal Tag': 'hs_lead_status',
    'LinkedIn URL': 'hs_linkedin_url',
    'Confidence Level': 'business_type',  # repurpose existing custom property
}


def get_api_key() -> str:
    key = os.environ.get('HUBSPOT_API_KEY', '')
    if not key:
        print("Error: HUBSPOT_API_KEY env var not set.")
        print("Set it to your HubSpot private app access token.")
        sys.exit(1)
    return key


def search_contact_by_email(email: str, api_key: str) -> str | None:
    """Search HubSpot for an existing contact by email. Returns contact ID or None."""
    url = 'https://api.hubapi.com/crm/v3/objects/contacts/search'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'filterGroups': [{
            'filters': [{
                'propertyName': 'email',
                'operator': 'EQ',
                'value': email,
            }]
        }],
        'limit': 1,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            results = resp.json().get('results', [])
            if results:
                return results[0].get('id')
    except Exception:
        pass
    return None


def create_contact(properties: dict, api_key: str) -> dict:
    """Create a new contact in HubSpot."""
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {'properties': properties}
    resp = requests.post(HUBSPOT_API_BASE, headers=headers, json=payload, timeout=10)
    return resp.json()


def update_contact(contact_id: str, properties: dict, api_key: str) -> dict:
    """Update an existing contact in HubSpot."""
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    url = f'{HUBSPOT_API_BASE}/{contact_id}'
    payload = {'properties': properties}
    resp = requests.patch(url, headers=headers, json=payload, timeout=10)
    return resp.json()


def row_to_properties(row: dict) -> dict:
    """Convert a CSV row to HubSpot properties dict."""
    properties = {}
    for csv_col, hs_prop in FIELD_MAP.items():
        value = (row.get(csv_col) or '').strip()
        if value:
            properties[hs_prop] = value
    # Add notes as a note body if present
    notes = (row.get('Notes') or '').strip()
    if notes:
        properties['notes_last_contacted'] = notes[:500]
    return properties


def push(input_csv: str, dry_run: bool = False):
    api_key = get_api_key()

    with open(input_csv, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("No contacts to push.")
        return

    created = 0
    updated = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        email = (row.get('Email') or '').strip()
        if not email:
            print(f"  [{i}/{len(rows)}] Skip — no email")
            continue

        properties = row_to_properties(row)

        if dry_run:
            print(f"  [{i}/{len(rows)}] DRY RUN: {email} → {json.dumps(properties, indent=2)}")
            continue

        # Check for existing contact
        existing_id = search_contact_by_email(email, api_key)

        try:
            if existing_id:
                result = update_contact(existing_id, properties, api_key)
                if 'id' in result:
                    updated += 1
                    print(f"  [{i}/{len(rows)}] Updated: {email} (ID: {existing_id})")
                else:
                    errors += 1
                    print(f"  [{i}/{len(rows)}] Error updating {email}: {result}")
            else:
                result = create_contact(properties, api_key)
                if 'id' in result:
                    created += 1
                    print(f"  [{i}/{len(rows)}] Created: {email} (ID: {result['id']})")
                else:
                    errors += 1
                    print(f"  [{i}/{len(rows)}] Error creating {email}: {result}")
        except Exception as e:
            errors += 1
            print(f"  [{i}/{len(rows)}] Exception for {email}: {e}")

        time.sleep(0.2)  # rate limit

    print(f"\nHubSpot push complete:")
    print(f"  Created: {created}")
    print(f"  Updated: {updated}")
    print(f"  Errors:  {errors}")


def main():
    parser = argparse.ArgumentParser(description='Push contacts to HubSpot via API v3')
    parser.add_argument('--input', '-i', required=True, help='hubspot_import.csv')
    parser.add_argument('--dry-run', action='store_true', help='Print what would be pushed without calling API')
    args = parser.parse_args()
    push(args.input, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
