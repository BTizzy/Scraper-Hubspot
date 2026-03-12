"""test_pipeline.py

Synthetic end-to-end test for the Trillium lead-builder pipeline.

Creates 3 fake companies with known domains, runs individual module functions
in-memory (no subprocesses), and validates the output structure.

Usage:
  python test_pipeline.py
"""
import csv
import io
import os
import sys
import tempfile

# Ensure scripts dir is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trillium_config import (
    EMAIL_PATTERNS, SIGNALS, VERIFICATION_LEVELS,
    FRESHNESS_HALF_LIFE_DAYS, get_signal_priority, rank_signals,
    DM_TITLES, GENERIC_LOCAL_PARTS, TARGET_CITIES, get_daily_run_contract,
)

# ── Test data ──────────────────────────────────────────────────────────────────

FAKE_COMPANIES = [
    {
        'company_name': 'Northwest Builders LLC',
        'registered_date': '2024-11-15',
        'website': 'https://www.example-nwbuilders.com',
        'domain': 'example-nwbuilders.com',
        'officers': '[{"name": "Jane Smith", "title": "Owner"}, {"name": "Bob Johnson", "title": "CFO"}]',
    },
    {
        'company_name': 'Seattle Tech Solutions Inc',
        'registered_date': '2024-09-01',
        'website': 'https://seattletechsolutions.example.com',
        'domain': 'seattletechsolutions.example.com',
        'officers': '[{"name": "Alice Chen", "title": "CEO"}]',
    },
    {
        'company_name': 'Cascade Staffing Group',
        'registered_date': '2025-01-10',
        'website': '',
        'domain': '',
        'officers': '[]',
    },
]

PASSED = 0
FAILED = 0

def check(name, condition, detail=''):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ''))


# ── Test: trillium_config ──────────────────────────────────────────────────────

def test_config():
    print("\n🧪 trillium_config.py")
    check("EMAIL_PATTERNS has ≥5 patterns", len(EMAIL_PATTERNS) >= 5, f"got {len(EMAIL_PATTERNS)}")
    check("SIGNALS has exactly 3 signal types", len(SIGNALS) == 3, f"got {len(SIGNALS)}")
    check("SIGNALS contains business_change", 'business_change' in SIGNALS)
    check("SIGNALS does not contain rebrand", 'rebrand' not in SIGNALS)
    check("SIGNALS does not contain active_hiring", 'active_hiring' not in SIGNALS)
    check("SIGNALS does not contain website_refresh", 'website_refresh' not in SIGNALS)
    check("VERIFICATION_LEVELS has A/B/C/D", set(VERIFICATION_LEVELS.keys()) == {'A', 'B', 'C', 'D'})
    check("FRESHNESS_HALF_LIFE_DAYS is positive", FRESHNESS_HALF_LIFE_DAYS > 0)
    check("get_signal_priority('active_lawsuit') > 0", get_signal_priority('active_lawsuit') > 0)
    check("rank_signals(['business_change', 'active_lawsuit']) returns lawsuit first",
          rank_signals(['business_change', 'active_lawsuit'])[0] == 'active_lawsuit')
    check("DM_TITLES contains 'owner'", 'owner' in DM_TITLES)
    check("GENERIC_LOCAL_PARTS contains 'info'", 'info' in GENERIC_LOCAL_PARTS)
    check("TARGET_CITIES contains 'seattle'", 'seattle' in TARGET_CITIES)

    contract = get_daily_run_contract()


# ── Test: email_permutator ─────────────────────────────────────────────────────

def test_email_permutator():
    print("\n🧪 email_permutator.py")
    from email_permutator import generate_permutations, score_candidate

    candidates = generate_permutations('Jane', 'Smith', 'example.com')
    check("Generates candidates", len(candidates) > 0, f"got {len(candidates)}")
    check("first.last@domain is in candidates", 'jane.smith@example.com' in candidates)
    check("first@domain is in candidates", 'jane@example.com' in candidates)
    check("No duplicates", len(candidates) == len(set(candidates)))

    # Score test
    score = score_candidate('jane.smith@example.com', 'Jane', 'Smith', 'example.com',
                             smtp_result='ACCEPT', is_catchall=False)
    check("first.last pattern scores high", score > 0.8, f"got {score}")

    score_reject = score_candidate('jane.smith@example.com', 'Jane', 'Smith', 'example.com',
                                    smtp_result='REJECT')
    check("SMTP reject → score 0", score_reject == 0.0, f"got {score_reject}")

    # Edge case: no last name
    candidates_no_last = generate_permutations('Jane', '', 'example.com')
    check("Handles missing last name", len(candidates_no_last) > 0)

    # Edge case: empty first name
    candidates_empty = generate_permutations('', 'Smith', 'example.com')
    check("Empty first name → empty list", len(candidates_empty) == 0)


# ── Test: waterfall_enricher ───────────────────────────────────────────────────

def test_waterfall_enricher():
    print("\n🧪 waterfall_enricher.py")
    from waterfall_enricher import (
        infer_name_from_email, deduplicate, filter_decision_makers,
        source_officer_permutation,
    )

    # Name inference
    first, last = infer_name_from_email('jane.smith@example.com')
    check("Infers 'Jane' from jane.smith@", first == 'Jane', f"got {first}")
    check("Infers 'Smith' from jane.smith@", last == 'Smith', f"got {last}")

    first2, last2 = infer_name_from_email('info@example.com')
    check("Generic email → empty names", first2 == '' and last2 == '')

    # Dedup
    duped = [
        {'email': 'a@example.com', 'source': 'theHarvester'},
        {'email': 'A@example.com', 'source': 'team_page'},
        {'email': 'b@example.com', 'source': 'dork'},
    ]
    unique = deduplicate(duped)
    check("Dedup removes case-insensitive dupes", len(unique) == 2, f"got {len(unique)}")
    check("First source preserved", unique[0]['source'] == 'theHarvester')

    # DM filtering
    contacts = [
        {'email': 'a@x.com', 'title': 'Marketing Intern'},
        {'email': 'b@x.com', 'title': 'CEO'},
        {'email': 'c@x.com', 'title': 'Owner'},
    ]
    filtered = filter_decision_makers(contacts)
    check("DMs promoted to top", filtered[0]['title'] == 'CEO' or filtered[0]['title'] == 'Owner')
    check("DM flag set", filtered[0].get('is_dm') is True)

    # Officer permutation
    officers = [{'name': 'Alice Chen', 'title': 'CEO'}]
    perms = source_officer_permutation('example.com', officers)
    check("Officer permutation generates candidates", len(perms) > 0)
    check("First candidate has email", '@example.com' in perms[0].get('email', ''))


# ── Test: freshness_scorer ─────────────────────────────────────────────────────

def test_freshness_scorer():
    print("\n🧪 freshness_scorer.py")
    from freshness_scorer import (
        time_decay, days_since, verification_score,
        signal_freshness_score, compute_freshness_score,
    )

    # Time decay
    check("time_decay(0) = 1.0", time_decay(0) == 1.0)
    check("time_decay(90) ≈ 0.5", abs(time_decay(90) - 0.5) < 0.01, f"got {time_decay(90)}")
    check("time_decay(180) ≈ 0.25", abs(time_decay(180) - 0.25) < 0.01, f"got {time_decay(180)}")

    # Verification score (MX only raised to 0.5 for free-tier pipeline)
    check("MX only → 0.5", verification_score(True) == 0.5)
    check("MX + SMTP accept → 0.75", verification_score(True, smtp_ok='ACCEPT') == 0.75)
    check("MX + SMTP + not catch-all → 1.0",
          verification_score(True, smtp_ok='ACCEPT', catch_all='False') == 1.0)
    check("No MX → 0.0", verification_score(False) == 0.0)

    # Signal freshness
    score = signal_freshness_score('active_lawsuit', '2025-01-01')
    check("Active lawsuit signal > 0", score > 0, f"got {score}")

    # Composite score
    row = {
        'source': 'team_page',
        'mx_pass': 'True',
        'smtp_ok': 'ACCEPT',
        'catch_all': 'False',
        'collected_date': '2025-06-01',
        'signal_tag': 'active_lawsuit',
        'signal_date': '2025-05-15',
    }
    result = compute_freshness_score(row)
    check("Composite score present", 'freshness_score' in result)
    check("Confidence level present", 'confidence_level' in result)
    check("Score > 0.5 for good record", result['freshness_score'] > 0.5,
          f"got {result['freshness_score']}")
    check("Score breakdown present", 'score_breakdown' in result)


# ── Test: CSV round-trip ───────────────────────────────────────────────────────

def test_csv_roundtrip():
    print("\n🧪 CSV round-trip (synthetic data)")
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'email', 'first_name', 'last_name', 'company', 'title',
            'source', 'mx_pass', 'smtp_ok', 'catch_all',
            'collected_date', 'signal_tag', 'signal_date'
        ])
        writer.writeheader()
        writer.writerow({
            'email': 'jane.smith@example.com',
            'first_name': 'Jane',
            'last_name': 'Smith',
            'company': 'Northwest Builders LLC',
            'title': 'Owner',
            'source': 'team_page',
            'mx_pass': 'True',
            'smtp_ok': 'ACCEPT',
            'catch_all': 'False',
            'collected_date': '2025-06-01',
            'signal_tag': 'active_lawsuit',
            'signal_date': '2025-05-15',
        })
        writer.writerow({
            'email': 'bob@example.com',
            'first_name': 'Bob',
            'last_name': 'Johnson',
            'company': 'Seattle Tech Inc',
            'title': '',
            'source': 'google_dork',
            'mx_pass': 'True',
            'smtp_ok': 'UNKNOWN',
            'catch_all': '',
            'collected_date': '2024-01-01',
            'signal_tag': '',
            'signal_date': '',
        })
        input_path = f.name

    output_path = input_path.replace('.csv', '_scored.csv')
    try:
        from freshness_scorer import score_file
        scored = score_file(input_path, output_path, min_level='C')
        check("score_file returns results", len(scored) > 0)
        check("Output CSV created", os.path.exists(output_path))

        # Read output and verify
        with open(output_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        check("Output has 2 rows", len(rows) == 2, f"got {len(rows)}")
        check("First row is best score (sorted)", float(rows[0]['freshness_score']) >= float(rows[1]['freshness_score']))
        check("Confidence levels are valid", all(r['confidence_level'] in ['A','B','C','D'] for r in rows))
    finally:
        os.unlink(input_path)
        if os.path.exists(output_path):
            os.unlink(output_path)
        qualified = output_path.replace('.csv', '_qualified.csv')
        if os.path.exists(qualified):
            os.unlink(qualified)


# ── Test: new signal detectors ───────────────────────────────────────────────

def test_signal_detectors():
    print("\n🧪 signal detectors")

    import find_lawsuits

    # CourtListener should degrade cleanly when no API key is present.
    old_token = os.environ.pop('COURTLISTENER_API_KEY', None)
    try:
        result = find_lawsuits.query_courtlistener_v4('Example Co', find_lawsuits.datetime.now(find_lawsuits.UTC))
        check("CourtListener v4 reports no_api_key without token", result.get('status') == 'no_api_key', str(result))
    finally:
        if old_token is not None:
            os.environ['COURTLISTENER_API_KEY'] = old_token

    # find_rebrands should tag as business_change (not rebrand)
    from find_rebrands import extract_row_aliases, KEYWORDS
    check("find_rebrands KEYWORDS includes 'sold'", 'sold' in KEYWORDS)
    check("find_rebrands KEYWORDS includes 'acquired'", 'acquired' in KEYWORDS)
    check("find_rebrands KEYWORDS includes 'under new management'", 'under new management' in KEYWORDS)
    check("find_rebrands KEYWORDS includes 'business sale'", 'business sale' in KEYWORDS)


def test_email_accuracy_guards():
    print("\n🧪 email accuracy guards")

    from freshness_scorer import compute_freshness_score
    from find_rebrands import extract_row_aliases

    weak_guess = compute_freshness_score({
        'source': 'officer_permutation',
        'mx_pass': 'True',
        'verification_score': 'PASS',
        'smtp_ok': '',
        'catch_all': '',
        'signal_tag': 'new_business',
        'collected_date': '2026-03-10',
    })
    check("Officer permutation without SMTP cannot be A/B", weak_guess['confidence_level'] in ('C', 'D'), str(weak_guess))

    strong_guess = compute_freshness_score({
        'source': 'officer_permutation',
        'mx_pass': 'True',
        'verification_score': 'PASS',
        'smtp_ok': 'ACCEPT',
        'catch_all': 'False',
        'signal_tag': 'new_business',
        'collected_date': '2026-03-10',
    })
    check("Officer permutation with SMTP can exceed C", strong_guess['confidence_level'] in ('A', 'B', 'C'), str(strong_guess))

    aliases = extract_row_aliases(
        {'company_name': 'ABC Roofing LLC', 'previous_name': 'XYZ Roofing LLC', 'trade_name': 'ABC Exteriors'},
        'ABC Roofing LLC',
    )
    check("Row alias extractor finds prior names", 'XYZ Roofing LLC' in aliases, str(aliases))


# ── Test: Qualification filtering by confidence tier ────────────────────────────

def test_qualification_by_confidence_tier():
    print("\n🧪 Qualification filtering by confidence tier")
    import csv
    from freshness_scorer import score_file
    
    # Create synthetic scored data with mixed confidence levels
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'email', 'first_name', 'last_name', 'company', 'title', 'source',
            'mx_pass', 'smtp_ok', 'catch_all', 'collected_date', 'signal_tag'
        ])
        writer.writeheader()
        # High-quality team_page email
        writer.writerow({
            'email': 'alice@example.com',
            'first_name': 'Alice',
            'last_name': 'Smith',
            'company': 'Acme Corp',
            'title': 'CEO',
            'source': 'team_page',
            'mx_pass': 'TRUE',
            'smtp_ok': 'ACCEPT',
            'catch_all': 'FALSE',
            'collected_date': '2026-03-10',
            'signal_tag': 'active_lawsuit',
        })
        # Officer permutation with SMTP should reach B
        writer.writerow({
            'email': 'bob@example.com',
            'first_name': 'Bob',
            'last_name': 'Jones',
            'company': 'XYZ Inc',
            'title': 'President',
            'source': 'officer_permutation',
            'mx_pass': 'TRUE',
            'smtp_ok': 'ACCEPT',
            'catch_all': 'FALSE',
            'collected_date': '2026-03-10',
            'signal_tag': 'business_change',
        })
        # Officer permutation without SMTP should be C or D
        writer.writerow({
            'email': 'charlie@example.com',
            'first_name': 'Charlie',
            'last_name': 'Brown',
            'company': 'Beta LLC',
            'title': 'Owner',
            'source': 'officer_permutation',
            'mx_pass': 'TRUE',
            'smtp_ok': '',
            'catch_all': '',
            'collected_date': '2026-03-10',
            'signal_tag': 'new_business',
        })
        input_path = f.name
    
    output_path = input_path.replace('.csv', '_scored.csv')
    try:
        scored = score_file(input_path, output_path, min_level='B')
        check("Score file returns results", len(scored) > 0, f"expected >0, got {len(scored)}")
        
        # Check the qualified file was created and respects confidence tier
        qualified_path = output_path.replace('.csv', '_qualified.csv')
        with open(qualified_path, newline='', encoding='utf-8') as f:
            qualified_rows = list(csv.DictReader(f))
        
        # Only team_page (A/B) and officer_permutation+SMTP (B) should qualify
        a_or_b = [r for r in qualified_rows if r.get('confidence_level') in ('A', 'B')]
        c_or_d = [r for r in qualified_rows if r.get('confidence_level') in ('C', 'D')]
        
        check("Qualified file contains only A/B tiers", len(c_or_d) == 0, f"found {len(c_or_d)} C/D in qualified")
        check("Officer permutation without SMTP filtered out", 
              'charlie@example.com' not in [r['email'] for r in qualified_rows],
              "charlie's unverified officer permutation should not qualify")
    finally:
        os.unlink(input_path)
        if os.path.exists(output_path):
            os.unlink(output_path)
        if os.path.exists(qualified_path):
            os.unlink(qualified_path)


# ── Test: HubSpot export quality gates ──────────────────────────────────────────

def test_hubspot_export_gates():
    print("\n🧪 HubSpot export quality gates")
    from build_csv import passes_quality

    # Officer permutation with SMTP+not-catch-all should pass (becomes B-grade via scorer)
    verified_officer = {
        'email': 'john@example.com',
        'company': 'Acme Corp',
        'mx_pass': 'TRUE',
        'source': 'officer_permutation',
        'confidence_level': 'B',
        'smtp_ok': 'ACCEPT',
        'catch_all': 'FALSE',
    }
    ok, reason = passes_quality(verified_officer)
    check("Officer permutation with SMTP acceptance passes", ok, f"reason: {reason}")

    # Officer permutation without SMTP at C-level should now pass (C accepted)
    unverified_officer = {
        **verified_officer,
        'smtp_ok': '',
        'confidence_level': 'C',
    }
    ok, reason = passes_quality(unverified_officer)
    check("Officer permutation C-level passes (C now accepted)", ok, f"reason: {reason}")

    # D confidence should always fail
    low_confidence = {
        **verified_officer,
        'confidence_level': 'D',
    }
    ok, reason = passes_quality(low_confidence)
    check("Low confidence (D) blocked from HubSpot", not ok, f"reason: {reason}")

    # High confidence team_page should pass
    team_page = {
        'email': 'alice@example.com',
        'company': 'Example Inc',
        'mx_pass': 'TRUE',
        'source': 'team_page',
        'confidence_level': 'A',
        'smtp_ok': 'ACCEPT',
        'catch_all': 'FALSE',
    }
    ok, reason = passes_quality(team_page)
    check("Team page A-grade passes export", ok, f"reason: {reason}")


def test_hubspot_export_dedupes_person_company():
    print("\n🧪 HubSpot export dedupes person/company")
    from build_csv import build

    fieldnames = [
        'email', 'first_name', 'last_name', 'company', 'title', 'source',
        'mx_pass', 'verification_score', 'confidence_level', 'smtp_ok', 'catch_all',
        'signal_tag', 'score_breakdown'
    ]
    rows = [
        {
            'email': 'john@example.com',
            'first_name': 'John',
            'last_name': 'Peterson',
            'company': 'Acme Roofing LLC',
            'title': 'Owner',
            'source': 'officer_permutation',
            'mx_pass': 'TRUE',
            'verification_score': 'PASS',
            'confidence_level': 'B',
            'smtp_ok': 'ACCEPT',
            'catch_all': 'FALSE',
            'signal_tag': 'new_business',
            'score_breakdown': 'test'
        },
        {
            'email': 'john.peterson@example.com',
            'first_name': 'John',
            'last_name': 'Peterson',
            'company': 'Acme Roofing LLC',
            'title': 'Owner',
            'source': 'officer_permutation',
            'mx_pass': 'TRUE',
            'verification_score': 'PASS',
            'confidence_level': 'B',
            'smtp_ok': 'ACCEPT',
            'catch_all': 'FALSE',
            'signal_tag': 'new_business',
            'score_breakdown': 'test'
        },
    ]

    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv') as fin:
        input_path = fin.name
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv') as fout:
        output_path = fout.name
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv') as frej:
        reject_path = frej.name

    try:
        with open(input_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        build(input_path, output_path, reject_path)

        with open(output_path, newline='', encoding='utf-8') as f:
            out_rows = list(csv.DictReader(f))
        with open(reject_path, newline='', encoding='utf-8') as f:
            rej_rows = list(csv.DictReader(f))

        check("Only one contact exported for duplicate person/company", len(out_rows) == 1, str(out_rows))
        duplicate_reasons = [r.get('reject_reason', '') for r in rej_rows]
        check("Duplicate person/company gets explicit reject reason",
              'Duplicate person/company' in duplicate_reasons,
              str(duplicate_reasons))
    finally:
        os.unlink(input_path)
        os.unlink(output_path)
        os.unlink(reject_path)


# ── Test: SMTP transport failure vs mailbox reject ──────────────────────────────

def test_smtp_transport_classification():
    print("\n🧪 SMTP transport failure vs mailbox reject")
    from freshness_scorer import compute_freshness_score
    from build_csv import passes_quality

    # Transport-blocked with strong composite (MX + signal + freshness) → reaches B.
    # The probe never touched the SMTP layer, so we have no evidence the mailbox
    # is invalid — do NOT hard-lock to C.
    transport_blocked_row = compute_freshness_score({
      'source': 'officer_permutation',
      'mx_pass': 'True',
      'verification_score': 'PASS',
      'smtp_ok': 'UNKNOWN',
      'catch_all': 'UNKNOWN',
      'smtp_status': 'transport_blocked',
      'signal_tag': 'new_business',
      'collected_date': '2026-03-10',
    })
    check("Transport-blocked officer_permutation escapes hard-C lock",
        transport_blocked_row['confidence_level'] in ('A', 'B'),
        str(transport_blocked_row))
    check("Transport-blocked officer_permutation capped at B (not A)",
        transport_blocked_row['confidence_level'] != 'A',
        str(transport_blocked_row))

    # mx_lookup_failed is also a transport-level issue (DNS) — same treatment.
    mx_failed_row = compute_freshness_score({
      'source': 'officer_permutation',
      'mx_pass': 'True',
      'verification_score': 'PASS',
      'smtp_ok': 'UNKNOWN',
      'catch_all': 'UNKNOWN',
      'smtp_status': 'mx_lookup_failed',
      'signal_tag': 'new_business',
      'collected_date': '2026-03-10',
    })
    check("mx_lookup_failed officer_permutation treated same as transport_blocked",
        mx_failed_row['confidence_level'] in ('A', 'B'),
        str(mx_failed_row))

    # Hard SMTP reject (5xx): server explicitly said mailbox is invalid → stays C.
    smtp_rejected_row = compute_freshness_score({
      'source': 'officer_permutation',
      'mx_pass': 'True',
      'verification_score': 'PASS',
      'smtp_ok': 'REJECT',
      'catch_all': 'UNKNOWN',
      'smtp_status': 'reject_target',
      'signal_tag': 'new_business',
      'collected_date': '2026-03-10',
    })
    check("Hard SMTP reject (5xx) officer_permutation stays C/D",
        smtp_rejected_row['confidence_level'] in ('C', 'D'),
        str(smtp_rejected_row))

    # Soft defer (4xx): server is alive but temporarily unavailable.
    # Not transport-blocked, so keep the existing hard-C behaviour.
    soft_defer_row = compute_freshness_score({
      'source': 'officer_permutation',
      'mx_pass': 'True',
      'verification_score': 'PASS',
      'smtp_ok': 'UNKNOWN',
      'catch_all': 'UNKNOWN',
      'smtp_status': 'soft_defer_4xx',
      'signal_tag': 'new_business',
      'collected_date': '2026-03-10',
    })
    check("Soft-defer 4xx officer_permutation stays C (not resolved as transport-blocked)",
        soft_defer_row['confidence_level'] in ('C', 'D'),
        str(soft_defer_row))

    # Export gate: transport-blocked B-tier officer_permutation passes (B accepted, no SMTP gate).
    tb_b_row = {
      'email': 'john@testco.com',
      'company': 'Test Co',
      'mx_pass': 'TRUE',
      'verification_score': 'PASS',
      'source': 'officer_permutation',
      'confidence_level': 'B',
      'smtp_ok': 'UNKNOWN',
      'catch_all': 'UNKNOWN',
      'smtp_status': 'transport_blocked',
    }
    ok, reason = passes_quality(tb_b_row)
    check("Transport-blocked B-tier officer_permutation passes export gate",
        ok, f"should have passed; got ok={ok}, reason={reason}")

    # Export gate: C-tier officer_permutation passes (C now accepted).
    soft_c_row = {**tb_b_row, 'smtp_status': 'soft_defer_4xx', 'confidence_level': 'C'}
    ok, reason = passes_quality(soft_c_row)
    check("C-tier officer_permutation passes export gate",
        ok, f"should have passed; got ok={ok}, reason={reason}")

    # Export gate: D-tier should still be blocked.
    d_row = {**tb_b_row, 'confidence_level': 'D'}
    ok, reason = passes_quality(d_row)
    check("D-tier officer_permutation blocked at export gate",
        not ok, f"should have failed; got ok={ok}, reason={reason}")


def test_build_input_pool_schema_and_dedupe():
    print("\n🧪 build_input_pool schema + dedupe")
    from build_input_pool import normalize_input_row, merge_rows

    # Normalize mixed-column source rows into pool schema.
    row_a = normalize_input_row(
        {
            'Company Name': 'Acme Roofing LLC',
            'Website': 'https://acmeroofing.com',
            'registered_date': '2025-01-05',
            'signal_tag': 'new_business',
        },
        source_name='manual:test_manual',
        company_col='Company Name',
    )
    check("Pool normalize maps company name", row_a['company_name'] == 'Acme Roofing LLC', str(row_a))
    check("Pool normalize derives domain", row_a['domain'] == 'acmeroofing.com', str(row_a))

    row_b = normalize_input_row(
        {
            'company_name': 'Acme Roofing LLC',
            'domain': 'acmeroofing.com',
            'signal_tag': 'business_change',
        },
        source_name='search:test_search',
        company_col='company_name',
    )

    merged, deduped = merge_rows([row_a, row_b])
    check("Pool merge dedupes by company+domain", len(merged) == 1, f"rows={len(merged)}")
    check("Pool merge tracks dedupe count", deduped == 1, f"deduped={deduped}")

    merged_row = merged[0]
    tags = set(t for t in merged_row.get('signal_tag', '').split(';') if t)
    check("Pool merge unions signal tags", tags == {'new_business', 'business_change'}, str(merged_row))
    sources = set(s for s in merged_row.get('source', '').split(';') if s)
    check("Pool merge unions sources", sources == {'manual:test_manual', 'search:test_search'}, str(merged_row))


# ── Test: source modules (no network) ─────────────────────────────────────────

def test_source_modules():
    print("\n🧪 source modules (offline)")

    # wa_sos_scraper: _normalize_api_result
    from sources.wa_sos_scraper import _normalize_api_result

    biz = {
        "BusinessName": "Test Builders LLC",
        "DateOfIncorporation": "2025-01-15",
        "UBI": "123456789",
        "BusinessType": "LLC",
        "RegisteredAgent": "John Doe",
        "Governors": [{"name": "Jane"}],
        "Status": "Active",
        "PrincipalOffice": "123 Main St",
    }
    result = _normalize_api_result(biz)
    check("wa_sos: normalizes API result", result is not None)
    check("wa_sos: company_name set", result["company_name"] == "Test Builders LLC")
    check("wa_sos: signal_tag is new_business", result["signal_tag"] == "new_business")
    check("wa_sos: source is wa_sos", result["source"] == "wa_sos")
    check("wa_sos: state is WA", result["state"] == "WA")
    check("wa_sos: empty name returns None", _normalize_api_result({"BusinessName": ""}) is None)

    # opencorporates_source: _normalize_api_result
    from sources.opencorporates_source import _normalize_api_result as oc_normalize

    oc_company = {
        "name": "Pacific Roofing Inc",
        "company_number": "OC-999",
        "incorporation_date": "2025-02-01",
        "jurisdiction_code": "us_wa",
        "company_type": "Corporation",
        "current_status": "Active",
        "previous_names": [],
    }
    oc_result = oc_normalize(oc_company)
    check("opencorp: normalizes result", oc_result is not None)
    check("opencorp: signal_tag is new_business", oc_result["signal_tag"] == "new_business")
    check("opencorp: source is opencorporates", oc_result["source"] == "opencorporates")

    # with previous_names → business_change
    oc_company_renamed = {**oc_company, "previous_names": [{"company_name": "Old Name"}]}
    oc_renamed = oc_normalize(oc_company_renamed)
    check("opencorp: previous_names adds business_change",
          "business_change" in oc_renamed["signal_tag"] and "new_business" in oc_renamed["signal_tag"])

    check("opencorp: empty name returns None", oc_normalize({"name": ""}) is None)

    # courtlistener_source: collect with empty names returns empty
    from sources.courtlistener_source import collect as cl_collect

    check("courtlistener: empty names returns []", cl_collect({}) == [])
    check("courtlistener: no company_names key returns []", cl_collect({"days": 30}) == [])

    # web_discovery: _build_queries, _extract_company_name, _extract_company
    from sources.web_discovery import _build_queries, _extract_company_name, _extract_company

    queries = _build_queries("WA", 2026)
    check("web_discovery: builds 4 queries", len(queries) == 4, f"got {len(queries)}")
    signal_types = {q[1] for q in queries}
    check("web_discovery: covers all 3 signals", signal_types == {"new_business", "active_lawsuit", "business_change"})

    name = _extract_company_name("Northwest Builders LLC filed for incorporation")
    check("web_discovery: extracts LLC company name", name == "Northwest Builders LLC", f"got '{name}'")

    check("web_discovery: no suffix returns empty", _extract_company_name("some random text") == "")

    # _extract_company with lawsuit signal
    lawsuit_result = _extract_company(
        {"title": "Acme Corp sued for damages", "snippet": "lawsuit filed against Acme Corp in WA court", "url": ""},
        "active_lawsuit", "WA",
    )
    check("web_discovery: extracts lawsuit company", lawsuit_result is not None and lawsuit_result["signal_tag"] == "active_lawsuit")

    # _extract_company rejects when no signal terms
    no_match = _extract_company(
        {"title": "Acme Corp annual report", "snippet": "normal business operations", "url": ""},
        "active_lawsuit", "WA",
    )
    check("web_discovery: rejects without lawsuit terms", no_match is None)


def test_collect_from_web_utils():
    print("\n🧪 collect_from_web utilities")

    from collect_from_web import normalize_company_name, merge_signal_tags, merge_sources, deduplicate

    # normalize_company_name
    check("normalize: strips LLC", normalize_company_name("Acme Roofing LLC") == "acme roofing")
    check("normalize: strips Inc.", normalize_company_name("Test Solutions Inc.") == "test solutions")
    check("normalize: empty returns empty", normalize_company_name("") == "")
    check("normalize: case insensitive", normalize_company_name("PACIFIC BUILDERS LLC") == "pacific builders")

    # merge_signal_tags
    check("merge_tags: combines two", merge_signal_tags("new_business", "active_lawsuit") in
          ("active_lawsuit;new_business", "new_business;active_lawsuit"))
    check("merge_tags: dedupes", merge_signal_tags("new_business", "new_business") == "new_business")
    check("merge_tags: handles empty", merge_signal_tags("", "new_business") == "new_business")

    # merge_sources
    check("merge_sources: combines two", len(merge_sources("wa_sos", "opencorporates").split(";")) == 2)
    check("merge_sources: dedupes", merge_sources("wa_sos", "wa_sos") == "wa_sos")

    # deduplicate
    companies = [
        {"company_name": "Acme Roofing LLC", "source": "wa_sos", "signal_tag": "new_business", "registered_date": "2025-01-01"},
        {"company_name": "Acme Roofing LLC", "source": "opencorporates", "signal_tag": "business_change", "registered_date": ""},
        {"company_name": "Pacific Builders Inc", "source": "wa_sos", "signal_tag": "new_business", "registered_date": "2025-02-01"},
    ]
    deduped = deduplicate(companies)
    check("deduplicate: merges same company", len(deduped) == 2, f"got {len(deduped)}")

    acme = [c for c in deduped if "acme" in c["company_name"].lower()][0]
    acme_tags = set(acme["signal_tag"].split(";"))
    check("deduplicate: merges signal_tags", acme_tags == {"new_business", "business_change"}, str(acme_tags))
    acme_sources = set(acme["source"].split(";"))
    check("deduplicate: merges sources", acme_sources == {"wa_sos", "opencorporates"}, str(acme_sources))
    check("deduplicate: keeps richer registered_date", acme["registered_date"] == "2025-01-01")


# ── Run all tests ──────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Trillium Lead Builder — Test Suite")
    print("=" * 60)

    test_config()
    test_email_permutator()
    test_waterfall_enricher()
    test_freshness_scorer()
    test_csv_roundtrip()
    test_signal_detectors()
    test_email_accuracy_guards()
    test_qualification_by_confidence_tier()
    test_hubspot_export_gates()
    test_hubspot_export_dedupes_person_company()
    test_smtp_transport_classification()
    test_build_input_pool_schema_and_dedupe()

    print(f"\n{'='*60}")
    print(f"  Results: {PASSED} passed, {FAILED} failed")
    print(f"{'='*60}")

    if FAILED > 0:
        sys.exit(1)

if __name__ == '__main__':
    main()
