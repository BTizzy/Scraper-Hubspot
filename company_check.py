#!/usr/bin/env python3
"""
Lead generation script for Trillium Hiring.
Searches for small companies (5-25 employees) in Seattle/King County
with active HR job postings.
"""

import json
import time
import urllib.request
import urllib.parse
import re

# Companies found from LinkedIn job searches with their sizes
# Format: (company_name, linkedin_slug, location, industry, hr_role, job_url)
companies_to_check = [
    # From LinkedIn job results - need to verify size
    ("SeekOut", "seek0ut", "Bellevue, WA", "Software/IT Services", "Sr. HR Generalist", "https://www.linkedin.com/jobs/hr-generalist-jobs-seattle-wa"),
    ("Bluetooth SIG", "bluetooth-sig", "Kirkland, WA", "Technology/Nonprofit", "HR Generalist II", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("TerraPower", "terrapower", "Bellevue, WA", "Nuclear Energy/Engineering", "HR Generalist", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("McKinstry", "mckinstry", "Seattle, WA", "Construction/Engineering", "HR Generalist", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("Cumming Group", "cumming-group", "Seattle, WA", "Construction Consulting", "HR Business Partner", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("PitchBook", "pitchbook", "Seattle, WA", "Financial Services/Data", "HR Specialist", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("MOONTON GAMES", "moontongames", "Bellevue, WA", "Gaming/IT Services", "HR Manager", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("Stoke Space", "stoke-space", "Kent, WA", "Aerospace/Engineering", "HR Manager", "https://www.linkedin.com/jobs/view/human-resources-manager-at-stoke-space-4379792832"),
    ("Jerry", "jerryinc", "Seattle, WA", "Insurtech/IT Services", "Senior People Operations Manager", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("Seagull", "seagull-ai", "Redmond, WA", "Software/IT Services", "Payroll & HR Generalist", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("Tessera", "tessera-works", "Auburn, WA", "Nonprofit/Facilities", "HR & Recruiting Partner", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("Edison47", "edison47", "Gig Harbor, WA", "Real Estate", "HR Coordinator", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("Hermanson Company", "hermanson-company", "Kent, WA", "Construction/Engineering", "HR Generalist", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("Solid Ground", "solid-ground", "Seattle, WA", "Nonprofit/Social Services", "HR Coordinator", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("DCG ONE", "dcgone", "Seattle, WA", "Marketing Agency", "People Operations Coordinator", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("LumiMeds", "lumimeds", "Seattle, WA", "Healthtech/Telehealth", "HR Manager", "https://www.linkedin.com/jobs/search/?keywords=HR%20Manager&location=Seattle"),
    ("OneEnergy", "oneenergy", "Seattle, WA", "Renewable Energy", "HR Generalist", "https://www.linkedin.com/jobs/junior-hr-jobs-seattle-wa"),
    ("CRH", "crh", "Tacoma, WA", "Construction", "HR Generalist", "https://www.linkedin.com/jobs/hr-generalist-jobs-seattle-wa"),
    ("Sysco", "sysco", "Kent, WA", "Food Distribution", "HR Manager", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
    ("Compass Group USA", "compass-group-north-america", "Seattle, WA", "Food Services", "Sr. HR Manager", "https://www.linkedin.com/jobs/human-resources-jobs-seattle-wa"),
]

# Known sizes from LinkedIn (already checked)
known_sizes = {
    "SeekOut": "51-200",
    "Bluetooth SIG": "51-200",
    "TerraPower": "501-1,000",
    "McKinstry": "1,001-5,000",
    "Cumming Group": "1,001-5,000",
    "PitchBook": "1,001-5,000",
    "MOONTON GAMES": "1,001-5,000",
    "Stoke Space": "201-500",
    "Jerry": "201-500",
    "Seagull": "201-500",
    "Tessera": "1,001-5,000",
    "Edison47": "51-200",
    "Hermanson Company": "201-500",
    "Solid Ground": "201-500",
    "DCG ONE": "201-500",
    "LumiMeds": "2-10",
    "OneEnergy": "11-50",
    "CRH": "10,001+",
    "Sysco": "10,001+",
    "Compass Group USA": "10,001+",
}

# Filter to only 5-25 employee companies
qualified = []
for company in companies_to_check:
    name = company[0]
    size = known_sizes.get(name, "unknown")
    # Parse size range
    if size in ["2-10", "11-50", "51-200"]:
        # Extract upper bound
        match = re.search(r'(\d+)-(\d+)', size)
        if match:
            upper = int(match.group(2))
            if upper <= 25:
                qualified.append(company)
        elif size == "2-10":
            qualified.append(company)

print(f"Companies with 5-25 employees: {len(qualified)}")
for q in qualified:
    print(f"  {q[0]} - {known_sizes.get(q[0], 'unknown')} employees - {q[3]}")
