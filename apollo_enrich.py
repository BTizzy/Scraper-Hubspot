#!/usr/bin/env python3
"""
Apollo.io Lead Enrichment Script for Trillium Hiring

Usage:
  python3 apollo_enrich.py --mode companies --input apollo_enrichment_list.csv --output enriched_companies.csv --api-key YOUR_API_KEY
  python3 apollo_enrich.py --mode contacts --input enriched_companies.csv --output enriched_contacts.csv --api-key YOUR_API_KEY
  python3 apollo_enrich.py --mode search --output apollo_new_leads.csv --api-key YOUR_API_KEY

Requirements:
  pip install requests
"""

import argparse
import csv
import json
import sys
import time
import requests

APOLLO_BASE_URL = "https://api.apollo.io"

# ICP filters
TARGET_INDUSTRIES = [
    "construction", "dentistry", "dental", "veterinary", "chiropractic",
    "optometry", "physical therapy", "manufacturing", "machine shop",
    "metal fabrication", "food manufacturing", "printing", "auto dealership",
    "restaurant", "real estate", "insurance", "legal services", "accounting",
    "consulting", "nonprofit"
]

TARGET_TITLES = [
    "owner", "president", "ceo", "founder", "managing partner",
    "principal", "partner", "director"
]

TARGET_LOCATIONS = [
    "seattle", "king county", "bellevue", "redmond", "kirkland",
    "issaquah", "kent", "renton", "federal way", "tukwila",
    "shoreline", "lynnwood", "bothell", "wa", "washington"
]

EXCLUDED_INDUSTRIES = [
    "staffing", "recruiting", "hr consulting", "hr tech", "payroll",
    "human resources consulting"
]


def get_headers(api_key):
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": api_key
    }


def enrich_company(domain, api_key):
    """Enrich a single company by domain."""
    url = f"{APOLLO_BASE_URL}/v1/organizations/enrich"
    params = {"api_key": api_key, "domain": domain}
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            org = data.get("organization", {})
            return {
                "name": org.get("name", ""),
                "domain": domain,
                "industry": org.get("industry", ""),
                "employee_count": org.get("estimated_num_employees", ""),
                "revenue": org.get("estimated_annual_revenue", ""),
                "phone": org.get("phone", ""),
                "city": org.get("city", ""),
                "state": org.get("state", ""),
                "country": org.get("country", ""),
                "linkedin_url": org.get("linkedin_url", ""),
                "raw": json.dumps(org)
            }
        elif resp.status_code == 429:
            print("  Rate limited, waiting 60s...")
            time.sleep(60)
            return enrich_company(domain, api_key)
        else:
            print(f"  Error {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"  Exception: {e}")
        return None


def bulk_enrich_companies(domains, api_key):
    """Enrich up to 10 companies per call."""
    url = f"{APOLLO_BASE_URL}/v1/organizations/bulk_enrich"
    params = {"api_key": api_key}
    results = []
    # Process in batches of 10
    for i in range(0, len(domains), 10):
        batch = domains[i:i+10]
        payload = {"domains": batch}
        try:
            resp = requests.post(url, params=params, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                for org in data.get("organizations", []):
                    results.append({
                        "name": org.get("name", ""),
                        "domain": org.get("primary_domain", ""),
                        "industry": org.get("industry", ""),
                        "employee_count": org.get("estimated_num_employees", ""),
                        "revenue": org.get("estimated_annual_revenue", ""),
                        "phone": org.get("phone", ""),
                        "city": org.get("city", ""),
                        "state": org.get("state", ""),
                        "country": org.get("country", ""),
                        "linkedin_url": org.get("linkedin_url", ""),
                    })
            elif resp.status_code == 429:
                print(f"  Rate limited at batch {i//10 + 1}, waiting 60s...")
                time.sleep(60)
                # Retry this batch
                i -= 10
                continue
            else:
                print(f"  Error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"  Exception: {e}")
        time.sleep(1)  # Rate limiting
    return results


def search_people(title, domain, api_key):
    """Search for people by title and company domain."""
    url = f"{APOLLO_BASE_URL}/v1/people/search"
    params = {
        "api_key": api_key,
        "q_organization_domains": domain,
        "person_titles[]": title,
        "per_page": 5
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            people = []
            for person in data.get("people", []):
                people.append({
                    "first_name": person.get("first_name", ""),
                    "last_name": person.get("last_name", ""),
                    "title": person.get("title", ""),
                    "email": person.get("email", ""),
                    "phone": person.get("phone_numbers", [{}])[0].get("sanitized_number", "") if person.get("phone_numbers") else "",
                    "linkedin_url": person.get("linkedin_url", ""),
                    "company": person.get("organization", {}).get("name", ""),
                    "domain": domain
                })
            return people
        elif resp.status_code == 429:
            print("  Rate limited, waiting 60s...")
            time.sleep(60)
            return search_people(title, domain, api_key)
        else:
            print(f"  Error {resp.status_code}: {resp.text[:200]}")
            return []
    except Exception as e:
        print(f"  Exception: {e}")
        return []


def enrich_person(name, domain, api_key):
    """Enrich a person by name and company domain."""
    url = f"{APOLLO_BASE_URL}/v1/people/match"
    params = {
        "api_key": api_key,
        "name": name,
        "organization_domain": domain,
        "reveal_personal_emails": True,
        "reveal_phone_number": True
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            person = data.get("person", {})
            if person:
                return {
                    "first_name": person.get("first_name", ""),
                    "last_name": person.get("last_name", ""),
                    "title": person.get("title", ""),
                    "email": person.get("email", ""),
                    "phone": person.get("phone_numbers", [{}])[0].get("sanitized_number", "") if person.get("phone_numbers") else "",
                    "linkedin_url": person.get("linkedin_url", ""),
                    "company": person.get("organization", {}).get("name", ""),
                    "domain": domain
                }
            return None
        elif resp.status_code == 429:
            print("  Rate limited, waiting 60s...")
            time.sleep(60)
            return enrich_person(name, domain, api_key)
        else:
            print(f"  Error {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"  Exception: {e}")
        return None


def search_companies(industry, location, size_range, api_key, page=1):
    """Search Apollo for companies matching ICP."""
    url = f"{APOLLO_BASE_URL}/v1/mixed_companies/search"
    params = {
        "api_key": api_key,
        "q_organization_keyword_tags[]": industry,
        "organization_locations[]": location,
        "organization_num_employees_ranges[]": size_range,
        "per_page": 100,
        "page": page
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            companies = []
            for org in data.get("organizations", []):
                companies.append({
                    "name": org.get("name", ""),
                    "domain": org.get("primary_domain", ""),
                    "industry": org.get("industry", ""),
                    "employee_count": org.get("estimated_num_employees", ""),
                    "revenue": org.get("estimated_annual_revenue", ""),
                    "phone": org.get("phone", ""),
                    "city": org.get("city", ""),
                    "state": org.get("state", ""),
                    "linkedin_url": org.get("linkedin_url", ""),
                })
            return companies, data.get("pagination", {}).get("total_pages", 1)
        elif resp.status_code == 429:
            print("  Rate limited, waiting 60s...")
            time.sleep(60)
            return search_companies(industry, location, size_range, api_key, page)
        else:
            print(f"  Error {resp.status_code}: {resp.text[:200]}")
            return [], 0
    except Exception as e:
        print(f"  Exception: {e}")
        return [], 0


def is_icp_match(company):
    """Check if a company matches Trillium's ICP."""
    # Check employee count
    emp_count = company.get("employee_count", "")
    if emp_count:
        try:
            count = int(emp_count)
            if count < 2 or count > 50:
                return False
        except ValueError:
            pass  # Range like "11-50" — keep it

    # Check industry exclusions
    industry = company.get("industry", "").lower()
    for excluded in EXCLUDED_INDUSTRIES:
        if excluded in industry:
            return False

    # Check for obviously wrong companies
    name = company.get("name", "").lower()
    wrong = ["amazon", "microsoft", "boeing", "google", "spacex", "uber", "tiktok",
             "nintendo", "stripe", "sofi", "affirm", "crowdstrike", "f5", "canonical",
             "anthropic", "pitchbook", "mckinstry", "hermanson", "compass group",
             "sysco", "walmart", "wm", "delaware north", "swissport", "seattle children",
             "cumming group", "addison group", "robert half", "lhh", "jobot", "medix"]
    for w in wrong:
        if w in name:
            return False

    return True


def mode_companies(args):
    """Enrich companies from input CSV."""
    domains = []
    with open(args.input, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            domain = row.get("Website", "").strip()
            if domain and domain != "Website":
                # Clean up domain
                domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
                if domain:
                    domains.append(domain)

    print(f"Enriching {len(domains)} companies...")
    results = bulk_enrich_companies(domains, args.api_key)

    # Filter by ICP
    icp_matches = [r for r in results if is_icp_match(r)]
    print(f"ICP matches: {len(icp_matches)} out of {len(results)} enriched")

    # Write output
    if icp_matches:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=icp_matches[0].keys())
            writer.writeheader()
            writer.writerows(icp_matches)
        print(f"Wrote {len(icp_matches)} companies to {args.output}")
    else:
        print("No ICP matches found")


def mode_contacts(args):
    """Find contacts at enriched companies."""
    companies = []
    with open(args.input, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            companies.append(row)

    print(f"Finding contacts at {len(companies)} companies...")
    all_contacts = []
    for i, company in enumerate(companies):
        domain = company.get("domain", "")
        name = company.get("name", "")
        if not domain:
            continue

        print(f"  [{i+1}/{len(companies)}] {name} ({domain})")

        # Try to find owner/president/CEO
        for title in TARGET_TITLES:
            people = search_people(title, domain, args.api_key)
            if people:
                all_contacts.extend(people)
                break  # Found someone, move to next company

        time.sleep(0.5)  # Rate limiting

    # Write output
    if all_contacts:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=all_contacts[0].keys())
            writer.writeheader()
            writer.writerows(all_contacts)
        print(f"Wrote {len(all_contacts)} contacts to {args.output}")
    else:
        print("No contacts found")


def mode_search(args):
    """Search Apollo for new companies matching ICP."""
    all_companies = []

    industries = [
        "construction", "dental", "veterinary", "chiropractic", "optometry",
        "physical therapy", "manufacturing", "machine shop", "metal fabrication",
        "food manufacturing", "auto dealership", "restaurant", "real estate",
        "insurance", "legal services", "accounting", "consulting"
    ]

    locations = ["Seattle, Washington", "King County, Washington"]
    size_ranges = ["1,25", "25,50"]

    for industry in industries:
        for location in locations:
            for size_range in size_ranges:
                print(f"Searching: {industry} in {location} ({size_range} employees)")
                companies, total_pages = search_companies(industry, location, size_range, args.api_key)
                icp_matches = [c for c in companies if is_icp_match(c)]
                all_companies.extend(icp_matches)
                print(f"  Found {len(companies)} companies, {len(icp_matches)} ICP matches")

                # Only go through first 3 pages to save credits
                for page in range(2, min(total_pages + 1, 4)):
                    companies, _ = search_companies(industry, location, size_range, args.api_key, page)
                    icp_matches = [c for c in companies if is_icp_match(c)]
                    all_companies.extend(icp_matches)
                    print(f"  Page {page}: {len(companies)} companies, {len(icp_matches)} ICP matches")
                    time.sleep(1)

                time.sleep(2)

    # Deduplicate by domain
    seen_domains = set()
    unique_companies = []
    for c in all_companies:
        domain = c.get("domain", "")
        if domain and domain not in seen_domains:
            seen_domains.add(domain)
            unique_companies.append(c)

    print(f"\nTotal unique ICP matches: {len(unique_companies)}")

    # Write output
    if unique_companies:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=unique_companies[0].keys())
            writer.writeheader()
            writer.writerows(unique_companies)
        print(f"Wrote {len(unique_companies)} companies to {args.output}")
    else:
        print("No companies found")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apollo.io Lead Enrichment for Trillium Hiring")
    parser.add_argument("--mode", choices=["companies", "contacts", "search"], required=True)
    parser.add_argument("--input", help="Input CSV file")
    parser.add_argument("--output", required=True, help="Output CSV file")
    parser.add_argument("--api-key", required=True, help="Apollo.io API key")
    args = parser.parse_args()

    if args.mode == "companies":
        if not args.input:
            print("--input required for companies mode")
            sys.exit(1)
        mode_companies(args)
    elif args.mode == "contacts":
        if not args.input:
            print("--input required for contacts mode")
            sys.exit(1)
        mode_contacts(args)
    elif args.mode == "search":
        mode_search(args)
