"""run_pipeline.py — Trillium Hiring lead-builder pipeline v2

End-to-end orchestrator inspired by Apollo's data pipeline:
  1. Company enrichment     (OpenCorporates + WA SOS)
  2. Headcount estimation   (team page spider)
  3. Lawsuit detection      (CourtListener)
  4. Business change detection (rebrands, transfers, DBA filings)
  5. Waterfall enrichment   (5-source contact discovery)
  6. Email verification     (MX + SMTP + catch-all)
  7. Freshness scoring      (A/B/C/D confidence tiers)
  8. HubSpot CSV builder    (quality-gated, import-ready)

Usage:
  python run_pipeline.py --sos sos_export.csv
  python run_pipeline.py --sos sos_export.csv --smtp --hunter-key YOUR_KEY
  python run_pipeline.py --sos sos_export.csv --skip-theharvester --skip-dorks --min-level B
  python run_pipeline.py --collect-from-web --min-level C

Flags:
  --sos               WA SOS CSV export (or use --collect-from-web)
  --collect-from-web  Auto-collect companies from web sources before pipeline
  --smtp              Enable SMTP RCPT verification (slow but more accurate)
  --hunter-key        Hunter.io API key (25 free/month)
  --skip-theharvester Skip theHarvester source
  --skip-dorks        Skip DuckDuckGo dork source
  --min-level         Minimum confidence for HubSpot (A/B/C/D, default: B)
  --output-dir        Directory for all output files (default: ./output)
  --dry-run           Print plan without executing
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime

from local_secrets import load_local_env
from trillium_config import get_daily_run_contract

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Step runner ────────────────────────────────────────────────────────────────

class PipelineStep:
    def __init__(self, number, name, cmd, description=''):
        self.number = number
        self.name = name
        self.cmd = cmd
        self.description = description
        self.status = 'pending'
        self.duration = 0.0
        self.output_rows = 0

    def run(self, total_steps):
        header = f"[{self.number}/{total_steps}] {self.name}"
        print(f"\n{'='*60}")
        print(f"  {header}")
        if self.description:
            print(f"  {self.description}")
        print(f"  CMD: {' '.join(self.cmd)}")
        print(f"{'='*60}")
        start = time.time()
        try:
            result = subprocess.run(self.cmd, cwd=SCRIPTS_DIR)
            self.duration = time.time() - start
            if result.returncode != 0:
                self.status = 'FAILED'
                print(f"\n  ❌ {self.name} failed (exit code {result.returncode})")
                return False
            self.status = 'OK'
            return True
        except FileNotFoundError as e:
            self.status = 'SKIP'
            self.duration = time.time() - start
            print(f"\n  ⚠ Command not found: {e}")
            return True  # non-fatal
        except Exception as e:
            self.status = 'ERROR'
            self.duration = time.time() - start
            print(f"\n  ❌ Unexpected error: {e}")
            return False


def count_csv_rows(filepath):
    """Count data rows in a CSV (excluding header)."""
    try:
        with open(filepath, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            return sum(1 for _ in reader)
    except Exception:
        return 0


def summarize_signal_stage(path, signal_name, status_col, error_col=''):
    """Summarize signal enrichment performance from an output CSV."""
    ok_statuses = {
        'ok',
        'ok_no_match',
        'ok_no_keyword',
        'no_domain',
        'match',
        'match_http',
        'match_whois',
        'ok_ddg_fallback',
    }
    summary = {
        'rows': 0,
        'signal_hits': 0,
        'new_business_only': 0,
        'status_counts': {},
        'problem_rows': 0,
    }
    if not os.path.exists(path):
        return summary

    try:
        with open(path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                summary['rows'] += 1
                signal_tag = (row.get('signal_tag', '') or '').strip()
                tags = {t.strip() for t in signal_tag.split(';') if t.strip()}

                if signal_name in tags:
                    summary['signal_hits'] += 1
                if tags == {'new_business'}:
                    summary['new_business_only'] += 1

                status = (row.get(status_col, '') or '').strip() or 'missing_status'
                summary['status_counts'][status] = summary['status_counts'].get(status, 0) + 1

                has_problem = status not in ok_statuses
                if error_col:
                    err_val = (row.get(error_col, '') or '').strip()
                    has_problem = has_problem or bool(err_val)
                if has_problem:
                    summary['problem_rows'] += 1
    except Exception:
        return summary

    return summary


def parse_date_flex(date_str):
    """Parse a date string from common pipeline formats."""
    if not date_str:
        return None
    for fmt in ('%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%m/%d/%Y', '%m-%d-%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(str(date_str).strip(), fmt)
        except ValueError:
            continue
    return None


def preflight_input_profile(sos_path):
    """Summarize input CSV age distribution and row count before running."""
    profile = {
        'rows': 0,
        'with_registered_date': 0,
        'age_buckets': {
            '0_365_days': 0,
            '366_730_days': 0,
            '731_plus_days': 0,
            'unknown': 0,
        },
    }
    if not os.path.exists(sos_path):
        return profile

    now = datetime.now()
    try:
        with open(sos_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                profile['rows'] += 1
                dt = parse_date_flex(row.get('registered_date', ''))
                if dt is None:
                    profile['age_buckets']['unknown'] += 1
                    continue
                profile['with_registered_date'] += 1
                age_days = max(0, (now - dt).days)
                if age_days <= 365:
                    profile['age_buckets']['0_365_days'] += 1
                elif age_days <= 730:
                    profile['age_buckets']['366_730_days'] += 1
                else:
                    profile['age_buckets']['731_plus_days'] += 1
    except Exception:
        return profile

    return profile


def load_csv_rows(path):
    """Read all rows from a CSV file safely."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def value_counts(rows, field, empty_label='(empty)'):
    """Count normalized values for a field in list-of-dict rows."""
    counts = {}
    for row in rows:
        value = (row.get(field, '') or '').strip()
        key = value if value else empty_label
        counts[key] = counts.get(key, 0) + 1
    return counts


def summarize_funnel(raw_rows, verified_rows, scored_rows, reject_rows):
    """Build end-to-end funnel diagnostics for contact quality bottlenecks."""
    funnel = {
        'counts': {
            'raw': len(raw_rows),
            'verified': len(verified_rows),
            'scored': len(scored_rows),
            'rejected': len(reject_rows),
        },
        'source_mix_raw': value_counts(raw_rows, 'source'),
        'source_mix_scored': value_counts(scored_rows, 'source'),
        'confidence_mix': value_counts(scored_rows, 'confidence_level'),
        'smtp': {
            'attempted': 0,
            'accepted': 0,
            'rejected': 0,
            'unknown': 0,
            'not_attempted': 0,
            'attempt_status': {},
        },
        'reject_reasons': value_counts(reject_rows, 'reject_reason'),
    }

    for row in verified_rows:
        attempted = (row.get('smtp_attempted', '') or '').strip().upper()
        smtp_ok = (row.get('smtp_ok', '') or '').strip().upper()
        smtp_status = (row.get('smtp_status', '') or '').strip() or '(empty)'
        funnel['smtp']['attempt_status'][smtp_status] = funnel['smtp']['attempt_status'].get(smtp_status, 0) + 1
        if attempted == 'TRUE':
            funnel['smtp']['attempted'] += 1
            if smtp_ok == 'TRUE' or smtp_ok == 'ACCEPT':
                funnel['smtp']['accepted'] += 1
            elif smtp_ok == 'FALSE' or smtp_ok == 'REJECT':
                funnel['smtp']['rejected'] += 1
            else:
                funnel['smtp']['unknown'] += 1
        else:
            funnel['smtp']['not_attempted'] += 1

    return funnel


def evaluate_run_contract(scored_rows, contract):
    """Evaluate hard run gates using scored contacts."""
    allowed_levels = set(contract.get('count_confidence_levels', ['A', 'B']))
    required_signals = list(contract.get('required_signals', []))
    min_signal_companies = int(contract.get('min_unique_companies_per_signal', 0))
    min_contacts = int(contract.get('min_contacts_per_run', 0))
    min_companies = int(contract.get('min_unique_companies_per_run', 0))

    eligible = [r for r in scored_rows if (r.get('confidence_level', '') or '').strip() in allowed_levels]
    eligible_contacts = len(eligible)
    unique_companies = { (r.get('company', '') or '').strip().lower() for r in eligible if (r.get('company', '') or '').strip() }

    signal_company_map = {s: set() for s in required_signals}
    for row in eligible:
        company = (row.get('company', '') or '').strip()
        if not company:
            continue
        tags = {t.strip() for t in (row.get('signal_tag', '') or '').split(';') if t.strip()}
        for sig in required_signals:
            if sig in tags:
                signal_company_map[sig].add(company.lower())

    deficits = []
    if eligible_contacts < min_contacts:
        deficits.append(f"contacts_ab={eligible_contacts} < required={min_contacts}")
    if len(unique_companies) < min_companies:
        deficits.append(f"unique_companies={len(unique_companies)} < required={min_companies}")

    signal_counts = {}
    for sig, companies in signal_company_map.items():
        count = len(companies)
        signal_counts[sig] = count
        if count < min_signal_companies:
            deficits.append(f"signal={sig} unique_companies={count} < required={min_signal_companies}")

    passed = len(deficits) == 0
    return {
        'passed': passed,
        'eligible_contacts': eligible_contacts,
        'unique_companies': len(unique_companies),
        'allowed_levels': sorted(allowed_levels),
        'signal_company_counts': signal_counts,
        'deficits': deficits,
    }


def write_contract_report(path, payload):
    """Write run contract evaluation report to JSON for auditability."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"  ⚠ Could not write run contract report: {e}")


# ── Main pipeline ──────────────────────────────────────────────────────────────

def main():
    load_local_env()
    parser = argparse.ArgumentParser(
        description='Trillium Hiring — Lead Builder Pipeline v2',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Basic run (MX verification only, no API keys needed):
  python run_pipeline.py --sos sos_export.csv

  # Full run with SMTP and Hunter.io:
  python run_pipeline.py --sos sos_export.csv --smtp --hunter-key abc123

  # Fast mode (skip slow sources, HubSpot-ready A+B leads only):
  python run_pipeline.py --sos sos_export.csv --skip-theharvester --skip-dorks --min-level B
        '''
    )
    parser.add_argument('--sos', default='', help='WA SOS CSV export file (or use --collect-from-web)')
    parser.add_argument('--collect-from-web', action='store_true',
                        help='Auto-collect companies from web sources before pipeline')
    parser.add_argument('--collect-days', type=int, default=90,
                        help='Lookback days for web collection (default: 90)')
    parser.add_argument('--collect-state', default='WA',
                        help='Target state for web collection (default: WA)')
    parser.add_argument('--smtp', action='store_true', help='Enable SMTP RCPT verification')
    parser.add_argument('--hunter-key', default='', help='Hunter.io API key')
    parser.add_argument('--skip-theharvester', action='store_true', help='Skip theHarvester')
    parser.add_argument('--skip-dorks', action='store_true', help='Skip DuckDuckGo dork')
    parser.add_argument('--min-level', default='C', choices=['A', 'B', 'C', 'D'],
                        help='Min confidence level for HubSpot import (default: C)')
    parser.add_argument('--output-dir', default='output', help='Output directory')
    parser.add_argument('--dry-run', action='store_true', help='Print plan only')
    parser.add_argument('--disable-contract-gates', action='store_true',
                        help='Disable hard daily run contract checks')
    args = parser.parse_args()

    # Run web collection if requested
    if args.collect_from_web:
        out_dir = os.path.abspath(args.output_dir)
        os.makedirs(out_dir, exist_ok=True)
        collected_csv = os.path.join(out_dir, 'companies_web_collected.csv')
        collect_cmd = [
            py, os.path.join(SCRIPTS_DIR, 'collect_from_web.py'),
            '--output', collected_csv,
            '--days', str(args.collect_days),
            '--state', args.collect_state,
        ]
        print("  Running web collection...")
        rc = subprocess.run(collect_cmd, cwd=SCRIPTS_DIR).returncode
        if rc != 0:
            print(f"  Web collection failed (exit code {rc})")
            return 1
        if not args.sos:
            args.sos = collected_csv
    elif not args.sos:
        parser.error('--sos is required unless --collect-from-web is used')

    # Resolve paths
    sos = os.path.abspath(args.sos)
    out_dir = os.path.abspath(args.output_dir)
    os.makedirs(out_dir, exist_ok=True)

    enriched        = os.path.join(out_dir, 'companies_enriched.csv')
    sized           = os.path.join(out_dir, 'companies_sized.csv')
    lawsuits        = os.path.join(out_dir, 'companies_lawsuits.csv')
    rebrands        = os.path.join(out_dir, 'companies_rebrands.csv')
    contacts_raw    = os.path.join(out_dir, 'contacts_raw.csv')
    contacts_verified = os.path.join(out_dir, 'contacts_verified.csv')
    contacts_scored = os.path.join(out_dir, 'contacts_scored.csv')
    contract_report = os.path.join(out_dir, 'run_contract_report.json')
    daily_kpi_report = os.path.join(out_dir, 'daily_kpi_report.json')
    hubspot         = os.path.join(out_dir, 'hubspot_import.csv')
    rejects         = os.path.join(out_dir, 'rejects.csv')

    contract = get_daily_run_contract()
    contract_enabled = bool(contract.get('enabled', True)) and not args.disable_contract_gates

    input_profile = preflight_input_profile(sos)

    py = sys.executable  # Use the same Python that runs this script

    # ── Build step list ────────────────────────────────────────────────────────
    steps = []

    # Step 1: Company enrichment
    steps.append(PipelineStep(1, 'Company Enrichment',
        [py, os.path.join(SCRIPTS_DIR, 'collect_companies.py'),
         '--input', sos, '--output', enriched],
        'OpenCorporates lookup for WA SOS companies'))

    # Step 2: Headcount estimation
    steps.append(PipelineStep(2, 'Headcount Estimation',
        [py, os.path.join(SCRIPTS_DIR, 'estimate_headcount.py'),
         '--input', enriched, '--output', sized],
        'Crawl team pages to estimate 5-25 employee range'))

    # Step 3: Lawsuit signal detection
    steps.append(PipelineStep(3, 'Lawsuit Detection',
        [py, os.path.join(SCRIPTS_DIR, 'find_lawsuits.py'),
         '--input', sized, '--output', lawsuits],
        'CourtListener search for active litigation'))

    # Step 4: Business change detection (rebrands, transfers, DBA filings)
    steps.append(PipelineStep(4, 'Business Change Detection',
        [py, os.path.join(SCRIPTS_DIR, 'find_rebrands.py'),
         '--input', lawsuits, '--output', rebrands],
        'Detect rebrands, transfers, sales, DBA filings via website keywords'))

    # Step 5: Waterfall contact enrichment
    waterfall_cmd = [
        py, os.path.join(SCRIPTS_DIR, 'waterfall_enricher.py'),
        '--input', rebrands, '--output', contacts_raw
    ]
    if args.skip_theharvester:
        waterfall_cmd.append('--skip-theharvester')
    if args.skip_dorks:
        waterfall_cmd.append('--skip-dorks')
    if args.hunter_key:
        waterfall_cmd.extend(['--hunter-key', args.hunter_key])
    steps.append(PipelineStep(5, 'Waterfall Contact Enrichment',
        waterfall_cmd,
        '5-source cascade: theHarvester → team pages → officer permutation → Hunter.io → dorks'))

    # Step 6: Email verification
    verify_cmd = [
        py, os.path.join(SCRIPTS_DIR, 'verify_emails.py'),
        '--input', contacts_raw, '--output', contacts_verified
    ]
    if args.smtp:
        verify_cmd.append('--smtp')
    steps.append(PipelineStep(6, 'Email Verification',
        verify_cmd,
        'MX + domain age' + (' + SMTP RCPT + catch-all' if args.smtp else '')))

    # Step 7: Freshness scoring
    steps.append(PipelineStep(7, 'Freshness Scoring',
        [py, os.path.join(SCRIPTS_DIR, 'freshness_scorer.py'),
         '--input', contacts_verified, '--output', contacts_scored,
         '--min-level', args.min_level],
        f'Score by source quality + verification + recency → filter ≥{args.min_level}'))

    # Step 8: HubSpot CSV builder
    steps.append(PipelineStep(8, 'HubSpot CSV Builder',
        [py, os.path.join(SCRIPTS_DIR, 'build_csv.py'),
         '--input', contacts_scored.replace('.csv', '_qualified.csv'),
         '--output', hubspot,
         '--rejects', rejects],
        'Quality gates + exact HubSpot header format'))

    total = len(steps)

    # ── Print plan ─────────────────────────────────────────────────────────────
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  TRILLIUM HIRING — Lead Builder Pipeline v2                  ║
║  {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^56s}  ║
╚══════════════════════════════════════════════════════════════╝

  Input:   {sos}
  Output:  {out_dir}/
  Verify:  MX{' + SMTP' if args.smtp else ''}{' + Hunter.io' if args.hunter_key else ''}
  Filter:  confidence ≥ {args.min_level}
  Steps:   {total}
  Contract gates:  {'ENABLED' if contract_enabled else 'DISABLED'}
""")

    print("  📥 Input profile:")
    print(f"    rows={input_profile['rows']} registered_date_present={input_profile['with_registered_date']}")
    print(f"    age_buckets={input_profile['age_buckets']}")
    young_rows = input_profile['age_buckets'].get('0_365_days', 0) + input_profile['age_buckets'].get('366_730_days', 0)
    if input_profile['rows'] > 0 and young_rows == input_profile['rows']:
        print("    ⚠ all rows are <=730 days old; lawsuit/rebrand density may be low")

    if contract_enabled:
        print("  🎯 Daily run contract:")
        print(
            "    "
            f"min_contacts={contract.get('min_contacts_per_run')} "
            f"min_unique_companies={contract.get('min_unique_companies_per_run')} "
            f"levels={contract.get('count_confidence_levels')}"
        )
        print(
            "    "
            f"required_signals={contract.get('required_signals')} "
            f"min_unique_companies_per_signal={contract.get('min_unique_companies_per_signal')}"
        )

    for step in steps:
        marker = '▶' if not args.dry_run else '○'
        print(f"  {marker} Step {step.number}: {step.name}")
        if step.description:
            print(f"           {step.description}")

    if args.dry_run:
        print("\n  🏁 Dry run — no steps executed.")
        return 0

    # ── Execute ────────────────────────────────────────────────────────────────
    pipeline_start = time.time()
    failed = False

    for step in steps:
        success = step.run(total)
        if not success:
            failed = True
            print(f"\n⛔ Pipeline halted at step {step.number}: {step.name}")
            break

    pipeline_duration = time.time() - pipeline_start

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  PIPELINE {'COMPLETE' if not failed else 'FAILED':^48s}  ║
╚══════════════════════════════════════════════════════════════╝
""")

    for step in steps:
        icon = {'OK': '✅', 'FAILED': '❌', 'SKIP': '⏭', 'ERROR': '💥', 'pending': '⬜'}
        status_icon = icon.get(step.status, '⬜')
        timing = f"{step.duration:.1f}s" if step.duration else ''
        print(f"  {status_icon} Step {step.number}: {step.name:30s} {step.status:8s} {timing}")

    print(f"\n  ⏱ Total time: {pipeline_duration:.1f}s")

    # Output file summary
    outputs = [
        ('Companies enriched', enriched),
        ('Companies sized', sized),
        ('Lawsuit signals', lawsuits),
        ('Business change signals', rebrands),
        ('Contacts (raw)', contacts_raw),
        ('Contacts (verified)', contacts_verified),
        ('Contacts (scored)', contacts_scored),
        ('HubSpot import', hubspot),
        ('Rejects', rejects),
    ]

    print(f"\n  📁 Output files:")
    for label, path in outputs:
        rows = count_csv_rows(path)
        exists = '✓' if os.path.exists(path) else '✗'
        print(f"    {exists} {label:25s} {rows:>5d} rows  {path}")

    # Quick stats on confidence levels
    scored_path = contacts_scored
    if os.path.exists(scored_path):
        with open(scored_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            levels = {}
            for row in reader:
                lvl = row.get('confidence_level', '?')
                levels[lvl] = levels.get(lvl, 0) + 1
        print(f"\n  📊 Confidence distribution:")
        for lvl in ['A', 'B', 'C', 'D']:
            count = levels.get(lvl, 0)
            bar = '█' * min(count, 40)
            print(f"    {lvl}: {count:>4d} {bar}")

    # Signal-stage diagnostics
    lawsuit_diag = summarize_signal_stage(
        lawsuits,
        signal_name='active_lawsuit',
        status_col='lawsuits_query_status',
        error_col='lawsuits_error',
    )
    rebrand_diag = summarize_signal_stage(
        rebrands,
        signal_name='business_change',
        status_col='rebrand_query_status',
        error_col='rebrand_error',
    )

    if lawsuit_diag['rows'] > 0 or rebrand_diag['rows'] > 0:
        print("\n  🔎 Signal diagnostics:")
    if lawsuit_diag['rows'] > 0:
        print(
            "    lawsuits: "
            f"rows={lawsuit_diag['rows']} "
            f"active_lawsuit={lawsuit_diag['signal_hits']} "
            f"problem_rows={lawsuit_diag['problem_rows']}"
        )
        print(f"      statuses={lawsuit_diag['status_counts']}")
        if lawsuit_diag['signal_hits'] == 0 and lawsuit_diag['problem_rows'] > 0:
            print("      ⚠ no active_lawsuit signals and lawsuit API/problem rows were detected")

    if rebrand_diag['rows'] > 0:
        print(
            "    business_change: "
            f"rows={rebrand_diag['rows']} "
            f"business_change={rebrand_diag['signal_hits']} "
            f"problem_rows={rebrand_diag['problem_rows']} "
            f"new_business_only={rebrand_diag['new_business_only']}"
        )
        print(f"      query_statuses={rebrand_diag['status_counts']}")
        if rebrand_diag['signal_hits'] == 0 and rebrand_diag['problem_rows'] > 0:
            print("      ⚠ no business_change signals and API/problem rows were detected")
        if rebrand_diag['new_business_only'] == rebrand_diag['rows'] and rebrand_diag['rows'] > 0:
            print("      ⚠ every record remained new_business-only; check dataset maturity and API availability")

    # Funnel diagnostics for A/B throughput bottlenecks
    raw_rows = load_csv_rows(contacts_raw)
    verified_rows = load_csv_rows(contacts_verified)
    scored_rows = load_csv_rows(contacts_scored)
    reject_rows = load_csv_rows(rejects)
    funnel = summarize_funnel(raw_rows, verified_rows, scored_rows, reject_rows)

    print("\n  🧪 Funnel diagnostics:")
    print(
        "    "
        f"raw={funnel['counts']['raw']} "
        f"verified={funnel['counts']['verified']} "
        f"scored={funnel['counts']['scored']} "
        f"rejected={funnel['counts']['rejected']}"
    )
    print(f"    source_mix_raw={funnel['source_mix_raw']}")
    print(f"    source_mix_scored={funnel['source_mix_scored']}")
    print(f"    confidence_mix={funnel['confidence_mix']}")
    print(
        "    smtp: "
        f"attempted={funnel['smtp']['attempted']} "
        f"accepted={funnel['smtp']['accepted']} "
        f"rejected={funnel['smtp']['rejected']} "
        f"unknown={funnel['smtp']['unknown']} "
        f"not_attempted={funnel['smtp']['not_attempted']}"
    )
    print(f"    smtp_statuses={funnel['smtp']['attempt_status']}")
    print(f"    top_reject_reasons={funnel['reject_reasons']}")

    # Hard run contract evaluation
    contract_failed = False
    contract_eval = {}
    if contract_enabled:
        scored_rows = load_csv_rows(contacts_scored)
        contract_eval = evaluate_run_contract(scored_rows, contract)
        contract_failed = not contract_eval.get('passed', False)

        print("\n  🧭 Run contract evaluation:")
        print(
            "    "
            f"eligible_contacts({contract_eval.get('allowed_levels', [])})={contract_eval.get('eligible_contacts', 0)} "
            f"unique_companies={contract_eval.get('unique_companies', 0)}"
        )
        print(f"    signal_unique_companies={contract_eval.get('signal_company_counts', {})}")
        if contract_failed:
            print("    ❌ CONTRACT FAILED")
            for d in contract_eval.get('deficits', []):
                print(f"      - {d}")
        else:
            print("    ✅ CONTRACT PASSED")

        write_contract_report(
            contract_report,
            {
                'generated_at': datetime.now().isoformat(),
                'input': sos,
                'output_dir': out_dir,
                'contract': contract,
                'input_profile': input_profile,
                'signal_diagnostics': {
                    'lawsuits': lawsuit_diag,
                    'business_change': rebrand_diag,
                },
                'evaluation': contract_eval,
                'pipeline_failed': failed,
                'funnel': funnel,
            },
        )
        print(f"    report={contract_report}")

    # Always write daily KPI report, even when contract gates are disabled.
    write_contract_report(
        daily_kpi_report,
        {
            'generated_at': datetime.now().isoformat(),
            'input': sos,
            'output_dir': out_dir,
            'pipeline_failed': failed,
            'contract_enabled': contract_enabled,
            'contract_failed': contract_failed,
            'input_profile': input_profile,
            'signal_diagnostics': {
                'lawsuits': lawsuit_diag,
                'business_change': rebrand_diag,
            },
            'funnel': funnel,
            'contract_evaluation': contract_eval,
        },
    )
    print(f"  📄 Daily KPI report: {daily_kpi_report}")

    if not failed and not contract_failed:
        print(f"\n  🎯 Ready: {hubspot}")
        print(f"     Import into HubSpot → Contacts → Import → choose file\n")

    if failed or contract_failed:
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
