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
    get_execution_mode_config,
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
    hosted_cfg = get_execution_mode_config('hosted_discovery')
    check("hosted_discovery skips theHarvester by default", hosted_cfg.get('skip_theharvester') is True)
    check("hosted_discovery skips dorks by default", hosted_cfg.get('skip_dorks') is True)

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
        source_officer_permutation, is_plausible_person_name,
        parse_sitemap_xml, rank_candidate_urls, select_wayback_candidates,
        extract_emails_from_bytes, discover_document_urls, extract_emails_from_text,
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

    check("Person-name plausibility accepts real names", is_plausible_person_name('Alice Chen'))
    check("Person-name plausibility rejects service phrases", not is_plausible_person_name('Helpful Tips'))
    check("Person-name plausibility rejects comma-separated branded pairs",
          not is_plausible_person_name('Generac, Kohler'))
    check("Person-name plausibility rejects uppercase marketing copy",
          not is_plausible_person_name('GENERAC AUTOMATIC STANDBY GENERATORS'))

    noisy_officers = [
        {'name': 'Helpful Tips', 'title': ''},
        {'name': 'Generac, Kohler', 'title': ''},
        {'name': 'Alice Chen', 'title': 'CEO'},
    ]
    noisy_perms = source_officer_permutation('example.com', noisy_officers)
    check("Officer permutation skips implausible fallback names", len(noisy_perms) > 0)
    check("Officer permutation keeps only valid-name candidates",
          all('alice' in c.get('email', '') or 'chen' in c.get('email', '') for c in noisy_perms),
          str(noisy_perms[:3]))

    sitemap_xml = """<?xml version='1.0' encoding='UTF-8'?>
    <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
        <url><loc>https://example.com/contact</loc><lastmod>2026-04-01</lastmod></url>
        <url><loc>https://example.com/blog/post</loc><lastmod>2024-01-01</lastmod></url>
    </urlset>"""
    url_entries, child_sitemaps = parse_sitemap_xml(sitemap_xml)
    check("Sitemap parser returns URL entries", len(url_entries) == 2, str(url_entries))
    check("Sitemap parser returns no child sitemaps for urlset", child_sitemaps == [], str(child_sitemaps))

    ranked_urls = rank_candidate_urls(url_entries, domain='example.com', max_urls=2)
    check("Ranked sitemap URLs prefer contact pages", ranked_urls[0]['loc'].endswith('/contact'), str(ranked_urls))

    doc_soup = __import__('bs4').BeautifulSoup(
        '<html><body><a href="/contact-directory.pdf">Directory</a><a href="/blog">Blog</a></body></html>',
        'html.parser',
    )
    document_urls = discover_document_urls('https://example.com', doc_soup)
    check("Document discovery keeps linked PDFs", document_urls == ['https://example.com/contact-directory.pdf'], str(document_urls))

    escaped_email_text = 'Reach us at u003einfo@example.com or jane [at] example.com'
    escaped_email_matches = extract_emails_from_text(escaped_email_text, 'example.com')
    check("Email extraction strips escaped HTML prefixes", 'info@example.com' in escaped_email_matches, str(escaped_email_matches))
    check("Email extraction keeps obfuscated addresses", 'jane@example.com' in escaped_email_matches, str(escaped_email_matches))

    sample_pdf = (
        b'%PDF-1.4\n'
        b'1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n'
        b'2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n'
        b'3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << >> >>\nendobj\n'
        b'4 0 obj\n<< /Length 48 >>\nstream\nBT /F1 12 Tf 72 72 Td (reach us at jane.doe@example.com) Tj ET\nendstream\nendobj\n'
        b'xref\n0 5\n0000000000 65535 f \n0000000010 00000 n \n0000000063 00000 n \n0000000120 00000 n \n0000000224 00000 n \n'
        b'trailer\n<< /Root 1 0 R /Size 5 >>\nstartxref\n321\n%%EOF\n'
    )
    extracted_pdf_emails = extract_emails_from_bytes(sample_pdf, 'example.com')
    check("PDF byte extraction recovers embedded emails", 'jane.doe@example.com' in extracted_pdf_emails, str(extracted_pdf_emails))

    cdx_rows = [
        {'timestamp': '20260401010101', 'original': 'https://example.com/contact', 'statuscode': '200', 'mimetype': 'text/html'},
        {'timestamp': '20210101010101', 'original': 'https://example.com/blog/post', 'statuscode': '200', 'mimetype': 'text/html'},
        {'timestamp': '20260401010101', 'original': 'https://other.com/contact', 'statuscode': '200', 'mimetype': 'text/html'},
        {'timestamp': '20260401010101', 'original': 'https://example.com/logo.png', 'statuscode': '200', 'mimetype': 'image/png'},
    ]
    wayback_candidates = select_wayback_candidates(cdx_rows, domain='example.com', max_urls=2)
    check("Wayback candidate selector filters to target domain", len(wayback_candidates) >= 1, str(wayback_candidates))
    check("Wayback candidate selector prefers contact pages", wayback_candidates[0]['loc'].endswith('/contact'), str(wayback_candidates))

    # is_dm populated by filter_decision_makers (called inside waterfall_enrich after dedup)
    raw_contacts = [
        {'email': 'ceo@acme.com', 'source': 'officer_permutation', 'title': 'CEO'},
        {'email': 'intern@acme.com', 'source': 'officer_permutation', 'title': 'Marketing Intern'},
    ]
    dm_result = filter_decision_makers(deduplicate(raw_contacts))
    check("is_dm populated on every contact after waterfall",
          all('is_dm' in c for c in dm_result), str(dm_result))
    check("CEO is_dm=True", next(c for c in dm_result if 'ceo' in c['email'])['is_dm'] is True)
    check("Intern is_dm=False", next(c for c in dm_result if 'intern' in c['email'])['is_dm'] is False)


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

    rss_items = find_lawsuits.parse_google_news_rss("""<?xml version='1.0'?><rss><channel>
        <item><title>Acme Roofing sued by supplier</title><link>https://example.com/lawsuit</link><description>Complaint filed in civil court</description></item>
    </channel></rss>""")
    check("find_lawsuits parses Google News RSS items", len(rss_items) == 1, str(rss_items))
    check("find_lawsuits RSS parser captures title", rss_items[0]['title'] == 'Acme Roofing sued by supplier', str(rss_items))

    original_v4 = find_lawsuits.query_courtlistener_v4
    original_news = find_lawsuits.query_google_news_lawsuits
    original_ddg = find_lawsuits.query_duckduckgo_lawsuits
    old_ddg_flag = os.environ.pop('ENABLE_DDG_LAWSUIT_FALLBACK', None)
    ddg_calls = []
    try:
        find_lawsuits.query_courtlistener_v4 = lambda name, since: {'results': [], 'status': 'no_api_key', 'http_status': '', 'error': ''}
        find_lawsuits.query_google_news_lawsuits = lambda name: {'results': [], 'status': 'news_no_match', 'http_status': 200, 'error': ''}

        def fake_ddg(name):
            ddg_calls.append(name)
            return {'results': [], 'status': 'ok_no_match', 'http_status': 200, 'error': ''}

        find_lawsuits.query_duckduckgo_lawsuits = fake_ddg
        result = find_lawsuits.query_courtlistener('Example Co', find_lawsuits.datetime.now(find_lawsuits.UTC))
        check("find_lawsuits skips DDG after news no-match by default",
              result.get('status') == 'news_no_match' and not ddg_calls,
              str((result, ddg_calls)))
    finally:
        find_lawsuits.query_courtlistener_v4 = original_v4
        find_lawsuits.query_google_news_lawsuits = original_news
        find_lawsuits.query_duckduckgo_lawsuits = original_ddg
        if old_ddg_flag is not None:
            os.environ['ENABLE_DDG_LAWSUIT_FALLBACK'] = old_ddg_flag

    # find_rebrands should tag as business_change (not rebrand)
    from find_rebrands import extract_row_aliases, KEYWORDS, parse_google_news_rss as parse_rebrand_news_rss
    check("find_rebrands KEYWORDS includes 'sold'", 'sold' in KEYWORDS)
    check("find_rebrands KEYWORDS includes 'acquired'", 'acquired' in KEYWORDS)
    check("find_rebrands KEYWORDS includes 'under new management'", 'under new management' in KEYWORDS)
    check("find_rebrands KEYWORDS includes 'business sale'", 'business sale' in KEYWORDS)
    rebrand_items = parse_rebrand_news_rss("""<?xml version='1.0'?><rss><channel>
        <item><title>Acme Roofing acquired by BiggerCo</title><link>https://example.com/acquired</link><description>Under new management after acquisition</description></item>
    </channel></rss>""")
    check("find_rebrands parses Google News RSS items", len(rebrand_items) == 1, str(rebrand_items))


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


def test_build_benchmark_dataset_helpers():
    print("\n🧪 build_benchmark_dataset.py")

    import build_benchmark_dataset as benchmark_builder

    parsed = benchmark_builder.parse_bing_rss_candidates("""<?xml version='1.0'?><rss><channel>
        <item><title>Acme Roofing - Seattle</title><link>https://www.acmeroofing.com/contact</link></item>
        <item><title>LinkedIn</title><link>https://www.linkedin.com/company/acme-roofing</link></item>
        <item><title>Best Staffing Agencies in Seattle</title><link>https://www.expertise.com/wa/seattle/staffing</link></item>
    </channel></rss>""")
    check("Bing RSS parser keeps first-party domains", len(parsed) == 1, str(parsed))
    check("Bing RSS parser normalizes domains", parsed[0]['domain'] == 'acmeroofing.com', str(parsed))

    built = benchmark_builder.build_candidate_row(parsed[0], 'seattle roofing contractor')
    check("Candidate builder preserves website", built['website'] == 'https://acmeroofing.com', str(built))
    check("Candidate builder tags discovery query", built['candidate_query'] == 'seattle roofing contractor', str(built))
    check("Candidate builder sets a default signal", built['signal_tag'] == 'business_change', str(built))

    original_get = benchmark_builder.requests.get

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text

        def raise_for_status(self):
            return None

    def fake_get(url, timeout, headers):
        if 'roofing' in url:
            return FakeResponse("""<?xml version='1.0'?><rss><channel>
                <item><title>Acme Roofing</title><link>https://www.acmeroofing.com</link></item>
                <item><title>Acme Roofing Contact</title><link>https://acmeroofing.com/contact</link></item>
            </channel></rss>""")
        return FakeResponse("""<?xml version='1.0'?><rss><channel>
            <item><title>Northwest Staffing</title><link>https://www.nwstaffing.com/about</link></item>
        </channel></rss>""")

    try:
        benchmark_builder.requests.get = fake_get
        discovered = benchmark_builder.discover_candidate_rows(
            ['seattle roofing contractor', 'seattle staffing agency'],
            max_candidates=10,
            per_query=2,
        )
    finally:
        benchmark_builder.requests.get = original_get

    check("Candidate discovery dedupes domains across same query", len(discovered) == 2, str(discovered))
    check("Candidate discovery carries domains into rows", {row['domain'] for row in discovered} == {'acmeroofing.com', 'nwstaffing.com'}, str(discovered))


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

    # Officer permutation without SMTP at C-level should fail strict gate.
    unverified_officer = {
        **verified_officer,
        'smtp_ok': '',
        'confidence_level': 'C',
    }
    ok, reason = passes_quality(unverified_officer)
    check("Officer permutation C-level blocked at strict export gate", not ok, f"reason: {reason}")

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

    strict_team_page_without_smtp = {
        **team_page,
        'smtp_ok': 'UNKNOWN',
        'catch_all': 'UNKNOWN',
        'smtp_status': 'not_attempted',
    }
    ok, reason = passes_quality(strict_team_page_without_smtp, mode='strict_verify')
    check("Strict verify blocks direct-source rows without SMTP acceptance", not ok, f"reason: {reason}")


def test_hubspot_export_dedupes_person_company():
    print("\n🧪 HubSpot export dedupes person/company")
    from build_csv import build

    fieldnames = [
        'email', 'first_name', 'last_name', 'company', 'title', 'source',
        'mx_pass', 'verification_score', 'confidence_level', 'smtp_ok', 'catch_all',
            'signal_tag', 'score_breakdown', 'reject_reason'
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
                'score_breakdown': 'test',
                'reject_reason': ''
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
                'score_breakdown': 'test',
                'reject_reason': ''
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

        # Verify reject_reason column is not duplicated when input already has it
        with open(reject_path, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader)
        check("reject_reason column appears exactly once in rejects.csv headers",
              headers.count('reject_reason') == 1,
              f"got headers={headers}")
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

    # Export gate (strict): transport-blocked B-tier officer_permutation is blocked.
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
            'source_count': '2',
    }
    ok, reason = passes_quality(tb_b_row, mode='strict_verify')
    check("Transport-blocked B-tier officer_permutation blocked at strict export gate",
        not ok, f"should have failed; got ok={ok}, reason={reason}")

    # Export gate (hosted): transport-blocked B-tier can pass as provisional.
    ok, reason = passes_quality(tb_b_row, mode='hosted_discovery')
    check("Transport-blocked B-tier officer_permutation allowed in hosted discovery mode",
        ok, f"should have passed; got ok={ok}, reason={reason}")

    # Hosted gate requires corroboration for provisional officer permutations.
    low_consensus_row = {**tb_b_row, 'source_count': '1'}
    ok, reason = passes_quality(low_consensus_row, mode='hosted_discovery')
    check("Low-consensus transport-blocked officer_permutation blocked in hosted discovery mode",
        not ok, f"should have failed; got ok={ok}, reason={reason}")

    # Export gate: SMTP-rejected officer_permutation blocked even if confidence is B.
    reject_b_row = {**tb_b_row, 'smtp_ok': 'REJECT', 'smtp_status': 'reject_target'}
    ok, reason = passes_quality(reject_b_row)
    check("SMTP-rejected officer_permutation blocked at export gate",
        not ok, f"should have failed; got ok={ok}, reason={reason}")

    # Export gate: soft-defer C-tier officer_permutation blocked (confidence gate fires).
    soft_c_row = {**tb_b_row, 'smtp_status': 'soft_defer_4xx', 'confidence_level': 'C'}
    ok, reason = passes_quality(soft_c_row)
    check("C-tier officer_permutation blocked at strict export gate",
        not ok, f"should have failed; got ok={ok}, reason={reason}")

    # Export gate: D-tier should still be blocked.
    d_row = {**tb_b_row, 'confidence_level': 'D'}
    ok, reason = passes_quality(d_row)
    check("D-tier officer_permutation blocked at export gate",
        not ok, f"should have failed; got ok={ok}, reason={reason}")


def test_strict_build_exports_only_smtp_accepted_rows():
    print("\n🧪 strict build requires SMTP acceptance")
    from build_csv import build

    fieldnames = [
        'email', 'first_name', 'last_name', 'company', 'title', 'source',
        'mx_pass', 'verification_score', 'confidence_level', 'smtp_ok', 'catch_all',
        'smtp_status', 'signal_tag', 'score_breakdown'
    ]
    rows = [
        {
            'email': 'accepted@example.com',
            'first_name': 'Accepted',
            'last_name': 'User',
            'company': 'Example Inc',
            'title': 'CEO',
            'source': 'team_page',
            'mx_pass': 'TRUE',
            'verification_score': 'PASS',
            'confidence_level': 'A',
            'smtp_ok': 'ACCEPT',
            'catch_all': 'FALSE',
            'smtp_status': 'accept_not_catchall',
            'signal_tag': 'new_business',
            'score_breakdown': 'accepted',
        },
        {
            'email': 'mxonly@example.com',
            'first_name': 'Mxonly',
            'last_name': 'User',
            'company': 'Example Inc',
            'title': 'CEO',
            'source': 'team_page',
            'mx_pass': 'TRUE',
            'verification_score': 'PASS',
            'confidence_level': 'A',
            'smtp_ok': 'UNKNOWN',
            'catch_all': 'UNKNOWN',
            'smtp_status': 'not_attempted',
            'signal_tag': 'new_business',
            'score_breakdown': 'mx_only',
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

        build(input_path, output_path, reject_path, mode='strict_verify')

        with open(output_path, newline='', encoding='utf-8') as f:
            out_rows = list(csv.DictReader(f))
        with open(reject_path, newline='', encoding='utf-8') as f:
            rej_rows = list(csv.DictReader(f))

        check("Strict build exports only SMTP-accepted rows",
              len(out_rows) == 1 and out_rows[0]['Email'] == 'accepted@example.com',
              str(out_rows))
        check("Strict build rejects MX-only direct rows",
              any(r.get('email') == 'mxonly@example.com' for r in rej_rows),
              str(rej_rows))
    finally:
        os.unlink(input_path)
        os.unlink(output_path)
        os.unlink(reject_path)


def test_verifier_api_fallback():
    print("\n🧪 verify_emails.py")
    from verify_emails import parse_verifier_api_result, score_email

    smtp_ok, catch_all, status = parse_verifier_api_result({
        'deliverable': True,
        'acceptAll': False,
        'mxFound': True,
        'status': 'deliverable',
    })
    check("Verifier API payload parser maps deliverable status", smtp_ok == 'ACCEPT', str((smtp_ok, catch_all, status)))
    check("Verifier API payload parser maps catch-all flag", catch_all == 'FALSE', str((smtp_ok, catch_all, status)))

    def fake_verifier(email, api_url, api_token=''):
        return 'ACCEPT', 'FALSE', 'verifier_api:deliverable'

    score, mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted = score_email(
        'jane.doe@example.com',
        source='officer_permutation',
        smtp=False,
        verifier_api_url='https://verifier.local',
        mx_checker=lambda _: True,
        age_lookup=lambda _: 365,
        verifier_lookup=fake_verifier,
    )
    check("Verifier API can promote officer permutation without direct SMTP",
          score == 'PASS' and smtp_ok == 'ACCEPT' and catch_all == 'FALSE',
          str((score, smtp_ok, catch_all, smtp_status, smtp_attempted)))

    def fake_reject(email, api_url, api_token=''):
        return 'REJECT', 'UNKNOWN', 'verifier_api:undeliverable'

    score, mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted = score_email(
        'alex@example.com',
        source='team_page',
        smtp=False,
        verifier_api_url='https://verifier.local',
        mx_checker=lambda _: True,
        age_lookup=lambda _: 365,
        verifier_lookup=fake_reject,
    )
    check("Verifier API rejection blocks mailbox", score.startswith('REJECT'),
          str((score, smtp_ok, catch_all, smtp_status, smtp_attempted)))


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


def test_collect_companies_circuit_breaker():
    print("\n🧪 collect_companies.py")
    from collect_companies import (
        update_opencorporates_state, OC_FAILURE_STREAK_LIMIT,
        update_duckduckgo_state, DDG_FAILURE_STREAK_LIMIT,
    )

    state = {'disabled': False, 'failure_streak': 0, 'disable_reason': ''}
    state = update_opencorporates_state(state, 'transient_failure', '503')
    check("OpenCorporates circuit increments transient failures", state['failure_streak'] == 1, str(state))

    state = update_opencorporates_state(state, 'success')
    check("OpenCorporates circuit resets streak after success", state['failure_streak'] == 0 and not state['disabled'], str(state))

    for _ in range(OC_FAILURE_STREAK_LIMIT):
        state = update_opencorporates_state(state, 'transient_failure', '503')
    check("OpenCorporates circuit disables after repeated transient failures", state['disabled'], str(state))

    ddg_state = {'disabled': False, 'failure_streak': 0, 'disable_reason': ''}
    for _ in range(DDG_FAILURE_STREAK_LIMIT):
        ddg_state = update_duckduckgo_state(ddg_state, 'transient_failure', 'timeout')
    check("DuckDuckGo circuit disables after repeated transient failures", ddg_state['disabled'], str(ddg_state))


def test_hosted_readiness_evaluator():
    print("\n🧪 hosted_readiness.py")
    from hosted_readiness import compute_readiness

    healthy_contract = {
        'pipeline_failed': False,
        'pipeline_duration_seconds': 540.0,
        'input_profile': {'rows': 80},
        'contract': {'min_unique_companies_per_run': 3},
        'evaluation': {
            'provisional_contacts': 20,
            'unique_companies': 0,
            'provisional_unique_companies': 12,
        },
        'top_of_funnel_alerts': [],
    }
    healthy_kpi = {
        'pipeline_failed': False,
        'pipeline_duration_seconds': 540.0,
        'funnel': {
            'counts': {'scored': 24},
            'source_mix_scored': {'team_page': 10, 'site_scan': 8, 'officer_permutation': 6},
        },
    }
    healthy_targets = {
        'target_weekly_quality_leads': 150,
        'target_daily_quality_leads': 22,
        'min_quality_lead_rate': 0.18,
        'max_single_source_dominance': 0.65,
        'min_distinct_sources': 2,
        'max_officer_permutation_share': 0.45,
        'max_benchmark_runtime_seconds': 900,
        'assumed_daily_company_capacity': 120,
    }
    report = compute_readiness(healthy_contract, healthy_kpi, healthy_targets)
    check("Hosted readiness passes healthy multi-source case", report['ready'], str(report))
    check("Hosted readiness projects weekly leads", report['metrics']['projected_weekly_quality_leads'] >= 150, str(report['metrics']))

    weak_contract = {
        'pipeline_failed': False,
        'pipeline_duration_seconds': 1200.0,
        'input_profile': {'rows': 80},
        'contract': {'min_unique_companies_per_run': 3},
        'evaluation': {
            'provisional_contacts': 8,
            'unique_companies': 0,
            'provisional_unique_companies': 0,
        },
        'top_of_funnel_alerts': ['officer_permutation_only'],
    }
    weak_kpi = {
        'pipeline_failed': False,
        'pipeline_duration_seconds': 1200.0,
        'funnel': {
            'counts': {'scored': 38},
            'source_mix_scored': {'officer_permutation': 38},
        },
    }
    weak_report = compute_readiness(weak_contract, weak_kpi, healthy_targets)
    check("Hosted readiness fails source-collapse case", not weak_report['ready'], str(weak_report))
    check("Hosted readiness deficits mention officer dominance",
          any('officer_permutation_share' in d or 'alert:officer_permutation_only' == d for d in weak_report['deficits']),
          str(weak_report['deficits']))


def test_provenance_aware_source_mix():
    print("\n🧪 provenance-aware source mix")
    from run_pipeline import summarize_funnel, compute_top_of_funnel_alerts

    raw_rows = [
        {'email': 'a@example.com', 'source': 'team_page', 'source_sources': 'site_scan;team_page'},
        {'email': 'b@example.com', 'source': 'officer_permutation', 'source_sources': 'officer_permutation'},
        {'email': 'c@example.com', 'source': 'sitemap_recent', 'source_sources': ''},
    ]
    funnel = summarize_funnel(raw_rows, [], raw_rows, [])

    check("Provenance source mix counts corroborating sources", funnel['source_mix_raw'].get('site_scan') == 1, str(funnel['source_mix_raw']))
    check("Provenance source mix counts canonical team pages too", funnel['source_mix_raw'].get('team_page') == 1, str(funnel['source_mix_raw']))
    check("Fallback source mix keeps rows without source_sources", funnel['source_mix_raw'].get('sitemap_recent') == 1, str(funnel['source_mix_raw']))
    check("Canonical source mix remains available", funnel['source_mix_raw_canonical'].get('team_page') == 1, str(funnel['source_mix_raw_canonical']))
    check("Top-of-funnel alerts use attribution denominator", not compute_top_of_funnel_alerts(funnel), str(compute_top_of_funnel_alerts(funnel)))


def test_mode_aware_run_contract():
    print("\n🧪 mode-aware run contract")
    from run_pipeline import evaluate_run_contract

    rows = [
        {'company': 'Acme', 'confidence_level': 'A', 'mx_pass': 'TRUE', 'smtp_ok': 'not_attempted', 'smtp_status': 'not_attempted', 'catch_all': 'FALSE', 'signal_tag': 'new_business'},
        {'company': 'Beta', 'confidence_level': 'A', 'mx_pass': 'TRUE', 'smtp_ok': 'not_attempted', 'smtp_status': 'not_attempted', 'catch_all': 'FALSE', 'signal_tag': 'new_business'},
        {'company': 'Gamma', 'confidence_level': 'A', 'mx_pass': 'TRUE', 'smtp_ok': 'not_attempted', 'smtp_status': 'not_attempted', 'catch_all': 'FALSE', 'signal_tag': 'new_business'},
    ]
    contract = {
        'count_confidence_levels': ['A', 'B', 'C'],
        'required_signals': ['new_business'],
        'min_unique_companies_per_signal': 1,
        'min_contacts_per_run': 2,
        'min_unique_companies_per_run': 2,
    }

    strict_eval = evaluate_run_contract(rows, contract, mode='strict_verify')
    hosted_eval = evaluate_run_contract(rows, contract, mode='hosted_discovery')

    check("Strict contract stays failed without SMTP acceptance", not strict_eval['passed'], str(strict_eval))
    check("Hosted contract passes on provisional hosted-quality rows", hosted_eval['passed'], str(hosted_eval))
    check("Hosted contract reports hosted_discovery mode", hosted_eval['evaluation_mode'] == 'hosted_discovery', str(hosted_eval))


# ── Test: source modules (no network) ─────────────────────────────────────────

def test_source_modules():
    print("\n🧪 source modules (offline)")

    # sos_scraper: _normalize_oregon_result
    from sources.sos_scraper import _normalize_oregon_result

    biz = {
        "business_name": "Test Builders LLC",
        "registry_date": "2025-01-15T00:00:00.000",
        "registry_number": "123456789",
        "entity_type": "DOMESTIC LIMITED LIABILITY COMPANY",
        "first_name": "John",
        "last_name": "Doe",
        "address_": "123 Main St",
        "city": "Portland",
        "state": "OR",
        "zip_code": "97201",
    }
    result = _normalize_oregon_result(biz)
    check("sos: normalizes Oregon result", result is not None)
    check("sos: company_name set", result["company_name"] == "Test Builders LLC")
    check("sos: signal_tag is new_business", result["signal_tag"] == "new_business")
    check("sos: source is oregon_sos", result["source"] == "oregon_sos")
    check("sos: state is OR", result["state"] == "OR")
    check("sos: registered_date parsed", result["registered_date"] == "2025-01-15")
    check("sos: empty name returns standard dict",
          _normalize_oregon_result({"business_name": "", "registry_number": ""})["company_name"] == "")

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
    test_build_benchmark_dataset_helpers()
    test_qualification_by_confidence_tier()
    test_hubspot_export_gates()
    test_hubspot_export_dedupes_person_company()
    test_smtp_transport_classification()
    test_verifier_api_fallback()
    test_build_input_pool_schema_and_dedupe()
    test_collect_companies_circuit_breaker()
    test_hosted_readiness_evaluator()
    test_provenance_aware_source_mix()
    test_mode_aware_run_contract()
    test_strict_build_exports_only_smtp_accepted_rows()

    print(f"\n{'='*60}")
    print(f"  Results: {PASSED} passed, {FAILED} failed")
    print(f"{'='*60}")

    if FAILED > 0:
        sys.exit(1)

if __name__ == '__main__':
    main()
