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
    DM_TITLES, GENERIC_LOCAL_PARTS, TARGET_CITIES,
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
    check("SIGNALS has ≥4 signal types", len(SIGNALS) >= 4, f"got {len(SIGNALS)}")
    check("VERIFICATION_LEVELS has A/B/C/D", set(VERIFICATION_LEVELS.keys()) == {'A', 'B', 'C', 'D'})
    check("FRESHNESS_HALF_LIFE_DAYS is positive", FRESHNESS_HALF_LIFE_DAYS > 0)
    check("get_signal_priority('active_lawsuit') > 0", get_signal_priority('active_lawsuit') > 0)
    check("rank_signals(['rebrand', 'active_lawsuit']) returns lawsuit first",
          rank_signals(['rebrand', 'active_lawsuit'])[0] == 'active_lawsuit')
    check("DM_TITLES contains 'owner'", 'owner' in DM_TITLES)
    check("GENERIC_LOCAL_PARTS contains 'info'", 'info' in GENERIC_LOCAL_PARTS)
    check("TARGET_CITIES contains 'seattle'", 'seattle' in TARGET_CITIES)


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

    # Verification score
    check("MX only → 0.3", verification_score(True) == 0.3)
    check("MX + SMTP accept → 0.7", verification_score(True, smtp_ok='ACCEPT') == 0.7)
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

    print(f"\n{'='*60}")
    print(f"  Results: {PASSED} passed, {FAILED} failed")
    print(f"{'='*60}")

    if FAILED > 0:
        sys.exit(1)

if __name__ == '__main__':
    main()
