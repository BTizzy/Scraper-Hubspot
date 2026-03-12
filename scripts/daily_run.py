"""daily_run.py

Daily automation wrapper for run_pipeline.py with cross-day novelty controls.

What this script adds:
  1) Company-level de-dup across days (skip companies already processed)
  2) Contact-level de-dup across days (skip emails already exported)
  3) Date-stamped output folders for auditable daily runs
  4) Persistent state files under --state-dir

Usage:
  python daily_run.py --sos sos_export.csv
  python daily_run.py --sos sos_export.csv --smtp --min-level B
  python daily_run.py --collect-from-web --days 90 --min-level C
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory


def normalize_company_key(row: dict) -> str:
    company = (row.get('company_name') or row.get('Company') or row.get('name') or '').strip().lower()
    domain = (row.get('domain') or row.get('website') or '').strip().lower()
    return f"{company}|{domain}" if company else ''


def read_csv_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def load_seen_companies(path: Path) -> set[str]:
    if not path.exists():
        return set()
    seen = set()
    with path.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row.get('company_key') or '').strip().lower()
            if key:
                seen.add(key)
    return seen


def append_seen_companies(path: Path, keys: set[str]) -> None:
    if not keys:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['company_key', 'recorded_at'])
        if not exists:
            writer.writeheader()
        now = datetime.now().isoformat()
        for key in sorted(keys):
            writer.writerow({'company_key': key, 'recorded_at': now})


def load_seen_emails(path: Path) -> set[str]:
    if not path.exists():
        return set()
    seen = set()
    with path.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = (row.get('email') or '').strip().lower()
            if email:
                seen.add(email)
    return seen


def append_seen_emails(path: Path, emails: set[str]) -> None:
    if not emails:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['email', 'recorded_at'])
        if not exists:
            writer.writeheader()
        now = datetime.now().isoformat()
        for email in sorted(emails):
            writer.writerow({'email': email, 'recorded_at': now})


def filter_novel_companies(input_sos: Path, seen_companies: set[str]) -> tuple[list[str], list[dict], list[dict]]:
    fieldnames, rows = read_csv_rows(input_sos)
    kept = []
    skipped = []
    for row in rows:
        key = normalize_company_key(row)
        if not key:
            skipped.append(row)
            continue
        if key in seen_companies:
            skipped.append(row)
            continue
        kept.append(row)
    return fieldnames, kept, skipped


def filter_hubspot_novel(hubspot_path: Path, seen_emails: set[str]) -> tuple[list[dict], list[dict]]:
    if not hubspot_path.exists():
        return [], []
    _, rows = read_csv_rows(hubspot_path)
    novel = []
    dupes = []
    for row in rows:
        email = (row.get('Email') or '').strip().lower()
        if email and email in seen_emails:
            dupes.append(row)
        else:
            novel.append(row)
    return novel, dupes


def main() -> int:
    parser = argparse.ArgumentParser(description='Daily wrapper for run_pipeline.py with cross-day de-dup controls')
    parser.add_argument('--sos', default='', help='WA SOS input CSV (or use --collect-from-web)')
    parser.add_argument('--collect-from-web', action='store_true',
                        help='Auto-collect companies from web sources before pipeline')
    parser.add_argument('--collect-days', type=int, default=90,
                        help='Lookback days for web collection (default: 90)')
    parser.add_argument('--collect-state', default='WA',
                        help='Target state for web collection (default: WA)')
    parser.add_argument('--state-dir', default='state', help='Directory for seen companies/emails state')
    parser.add_argument('--output-root', default='daily_output', help='Root folder for date-stamped pipeline outputs')
    parser.add_argument('--run-name', default='', help='Optional suffix for output folder name')
    parser.add_argument('--min-level', default='B', choices=['A', 'B', 'C', 'D'])
    parser.add_argument('--smtp', action='store_true', help='Pass --smtp through to run_pipeline.py')
    parser.add_argument('--hunter-key', default='', help='Pass through Hunter API key')
    parser.add_argument('--skip-theharvester', action='store_true')
    parser.add_argument('--skip-dorks', action='store_true')
    parser.add_argument('--disable-contract-gates', action='store_true')
    args = parser.parse_args()

    scripts_dir = Path(__file__).resolve().parent
    state_dir = (scripts_dir / args.state_dir).resolve()
    output_root = (scripts_dir / args.output_root).resolve()

    # Run web collection if requested
    if args.collect_from_web:
        collected_csv = output_root / 'web_collected.csv'
        collected_csv.parent.mkdir(parents=True, exist_ok=True)
        collect_cmd = [
            sys.executable,
            str(scripts_dir / 'collect_from_web.py'),
            '--output', str(collected_csv),
            '--days', str(args.collect_days),
            '--state', args.collect_state,
        ]
        print("Running web collection...")
        rc = subprocess.run(collect_cmd, cwd=str(scripts_dir)).returncode
        if rc != 0:
            print(f'Web collection failed (exit code {rc})')
            return rc
        if not args.sos:
            args.sos = str(collected_csv)
    elif not args.sos:
        parser.error('--sos is required unless --collect-from-web is used')

    sos_path = Path(args.sos).resolve()

    seen_companies_path = state_dir / 'seen_companies.csv'
    seen_emails_path = state_dir / 'seen_emails.csv'

    seen_companies = load_seen_companies(seen_companies_path)
    seen_emails = load_seen_emails(seen_emails_path)

    if not sos_path.exists():
        print(f"Input SOS file not found: {sos_path}")
        return 1

    fieldnames, candidate_rows, skipped_rows = filter_novel_companies(sos_path, seen_companies)
    if not candidate_rows:
        print('No novel companies to process today (all were already seen).')
        print(f'seen_companies={len(seen_companies)} input_rows={len(skipped_rows)}')
        return 0

    today = datetime.now().strftime('%Y%m%d')
    run_folder = f"{today}_{args.run_name}" if args.run_name else today
    out_dir = output_root / run_folder
    out_dir.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix='daily_sos_') as tmpdir:
        filtered_input = Path(tmpdir) / 'sos_novel.csv'
        write_csv_rows(filtered_input, fieldnames, candidate_rows)

        cmd = [
            sys.executable,
            str(scripts_dir / 'run_pipeline.py'),
            '--sos', str(filtered_input),
            '--output-dir', str(out_dir),
            '--min-level', args.min_level,
        ]
        if args.smtp:
            cmd.append('--smtp')
        if args.hunter_key:
            cmd.extend(['--hunter-key', args.hunter_key])
        if args.skip_theharvester:
            cmd.append('--skip-theharvester')
        if args.skip_dorks:
            cmd.append('--skip-dorks')
        if args.disable_contract_gates:
            cmd.append('--disable-contract-gates')

        print(f"Novel-company input rows: {len(candidate_rows)} (skipped seen: {len(skipped_rows)})")
        print(f"Running pipeline -> {out_dir}")
        rc = subprocess.run(cmd, cwd=str(scripts_dir)).returncode
        if rc != 0:
            print(f'Pipeline failed with exit code {rc}. State files not updated.')
            return rc

    hubspot_path = out_dir / 'hubspot_import.csv'
    novel_rows, duplicate_rows = filter_hubspot_novel(hubspot_path, seen_emails)

    # Keep original as audit trail, and overwrite hubspot_import.csv with only novel rows.
    if hubspot_path.exists():
        backup_path = out_dir / 'hubspot_import_all.csv'
        if not backup_path.exists():
            hubspot_path.replace(backup_path)
        fieldnames_hs, _ = read_csv_rows(backup_path)
        write_csv_rows(hubspot_path, fieldnames_hs, novel_rows)

    # Save duplicate contacts for visibility.
    dupes_path = out_dir / 'duplicate_contacts_skipped.csv'
    if duplicate_rows:
        fieldnames_hs, _ = read_csv_rows(out_dir / 'hubspot_import_all.csv')
        write_csv_rows(dupes_path, fieldnames_hs, duplicate_rows)

    # Update state from successful run.
    new_company_keys = {
        normalize_company_key(r)
        for r in candidate_rows
        if normalize_company_key(r)
    }
    new_emails = {
        (r.get('Email') or '').strip().lower()
        for r in novel_rows
        if (r.get('Email') or '').strip()
    }
    append_seen_companies(seen_companies_path, new_company_keys)
    append_seen_emails(seen_emails_path, new_emails)

    print('\nDaily run complete:')
    print(f'  output_dir={out_dir}')
    print(f'  input_companies_total={len(candidate_rows) + len(skipped_rows)}')
    print(f'  input_companies_novel={len(candidate_rows)}')
    print(f'  input_companies_skipped_seen={len(skipped_rows)}')
    print(f'  hubspot_contacts_novel={len(novel_rows)}')
    print(f'  hubspot_contacts_skipped_seen={len(duplicate_rows)}')
    print(f'  state_seen_companies={seen_companies_path}')
    print(f'  state_seen_emails={seen_emails_path}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
