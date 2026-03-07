"""run_pipeline.py — Trillium Hiring lead-builder pipeline v2

End-to-end orchestrator inspired by Apollo's data pipeline:
  1. Company enrichment     (OpenCorporates + WA SOS)
  2. Headcount estimation   (team page spider)
  3. Buying-signal detection (lawsuits + rebrands)
  4. Waterfall enrichment   (5-source contact discovery)
  5. Email verification     (MX + SMTP + catch-all)
  6. Freshness scoring      (A/B/C/D confidence tiers)
  7. HubSpot CSV builder    (quality-gated, import-ready)

Usage:
  python run_pipeline.py --sos sos_export.csv
  python run_pipeline.py --sos sos_export.csv --smtp --hunter-key YOUR_KEY
  python run_pipeline.py --sos sos_export.csv --skip-theharvester --skip-dorks --min-level B

Flags:
  --sos               WA SOS CSV export (required)
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
import os
import subprocess
import sys
import time
from datetime import datetime

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


# ── Main pipeline ──────────────────────────────────────────────────────────────

def main():
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
    parser.add_argument('--sos', required=True, help='WA SOS CSV export file')
    parser.add_argument('--smtp', action='store_true', help='Enable SMTP RCPT verification')
    parser.add_argument('--hunter-key', default='', help='Hunter.io API key')
    parser.add_argument('--skip-theharvester', action='store_true', help='Skip theHarvester')
    parser.add_argument('--skip-dorks', action='store_true', help='Skip DuckDuckGo dork')
    parser.add_argument('--min-level', default='B', choices=['A', 'B', 'C', 'D'],
                        help='Min confidence level for HubSpot import (default: B)')
    parser.add_argument('--output-dir', default='output', help='Output directory')
    parser.add_argument('--dry-run', action='store_true', help='Print plan only')
    args = parser.parse_args()

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
    hubspot         = os.path.join(out_dir, 'hubspot_import.csv')

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
        'Crawl team pages to estimate 5-30 employee range'))

    # Step 3: Lawsuit signal detection
    steps.append(PipelineStep(3, 'Lawsuit Detection',
        [py, os.path.join(SCRIPTS_DIR, 'find_lawsuits.py'),
         '--input', sized, '--output', lawsuits],
        'CourtListener search for active litigation'))

    # Step 4: Rebrand detection
    steps.append(PipelineStep(4, 'Rebrand Detection',
        [py, os.path.join(SCRIPTS_DIR, 'find_rebrands.py'),
         '--input', lawsuits, '--output', rebrands],
        'OpenCorporates previous names + website keyword scan'))

    # Step 5: Waterfall contact enrichment (NEW)
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

    # Step 7: Freshness scoring (NEW)
    steps.append(PipelineStep(7, 'Freshness Scoring',
        [py, os.path.join(SCRIPTS_DIR, 'freshness_scorer.py'),
         '--input', contacts_verified, '--output', contacts_scored,
         '--min-level', args.min_level],
        f'Score by source quality + verification + recency → filter ≥{args.min_level}'))

    # Step 8: HubSpot CSV builder
    steps.append(PipelineStep(8, 'HubSpot CSV Builder',
        [py, os.path.join(SCRIPTS_DIR, 'build_csv.py'),
         '--input', contacts_scored.replace('.csv', '_qualified.csv'),
         '--output', hubspot],
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
""")

    for step in steps:
        marker = '▶' if not args.dry_run else '○'
        print(f"  {marker} Step {step.number}: {step.name}")
        if step.description:
            print(f"           {step.description}")

    if args.dry_run:
        print("\n  🏁 Dry run — no steps executed.")
        return

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
        ('Rebrand signals', rebrands),
        ('Contacts (raw)', contacts_raw),
        ('Contacts (verified)', contacts_verified),
        ('Contacts (scored)', contacts_scored),
        ('HubSpot import', hubspot),
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

    if not failed:
        print(f"\n  🎯 Ready: {hubspot}")
        print(f"     Import into HubSpot → Contacts → Import → choose file\n")

if __name__ == '__main__':
    main()
