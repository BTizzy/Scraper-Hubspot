"""daily_contract_runner.py

Quota-driven daily runner that accumulates contacts across multiple pipeline batches
until production contract targets are met using real active emails.

Real active email criteria:
  - confidence_level in A/B
  - smtp_ok in {ACCEPT, TRUE}
  - catch_all == FALSE
  - mx_pass == TRUE

This avoids one-shot runs that cannot satisfy daily SLO targets.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from trillium_config import get_daily_run_contract

HUBSPOT_HEADERS = [
    "Email", "First Name", "Last Name", "Company", "Job Title",
    "Phone", "Website", "City", "Signal Tag", "LinkedIn URL", "Notes",
]


def normalize_company_key(row: dict) -> str:
    company = (row.get("company_name") or row.get("Company") or row.get("name") or "").strip().lower()
    domain = (row.get("domain") or row.get("website") or "").strip().lower()
    return f"{company}|{domain}" if company else ""


def read_csv_rows(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def split_name(first_name: str, last_name: str, email: str) -> tuple[str, str]:
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if first:
        return first.capitalize(), last.capitalize() if last else ""
    local = (email.split("@", 1)[0] if "@" in email else "").strip()
    for sep in (".", "_", "-"):
        if sep in local:
            parts = [p for p in local.split(sep) if p]
            if parts:
                return parts[0].capitalize(), parts[-1].capitalize() if len(parts) > 1 else ""
    return (local.capitalize(), "") if local else ("", "")


def load_seen_companies(path: Path) -> set[str]:
    _, rows = read_csv_rows(path)
    return {(r.get("company_key") or "").strip().lower() for r in rows if (r.get("company_key") or "").strip()}


def load_seen_emails(path: Path) -> set[str]:
    _, rows = read_csv_rows(path)
    return {(r.get("email") or "").strip().lower() for r in rows if (r.get("email") or "").strip()}


def append_seen_companies(path: Path, keys: set[str]) -> None:
    if not keys:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["company_key", "recorded_at"])
        if not exists:
            writer.writeheader()
        now = datetime.now().isoformat()
        for key in sorted(keys):
            writer.writerow({"company_key": key, "recorded_at": now})


def append_seen_emails(path: Path, emails: set[str]) -> None:
    if not emails:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["email", "recorded_at"])
        if not exists:
            writer.writeheader()
        now = datetime.now().isoformat()
        for email in sorted(emails):
            writer.writerow({"email": email, "recorded_at": now})


def unique_by_email(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        email = (row.get("email") or row.get("Email") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        out.append(row)
    return out


def is_real_active_email(row: dict) -> bool:
    confidence = (row.get("confidence_level") or "").strip().upper()
    smtp_ok = (row.get("smtp_ok") or "").strip().upper()
    catch_all = (row.get("catch_all") or "").strip().upper()
    mx_pass = (row.get("mx_pass") or "").strip().upper()
    return confidence in ("A", "B") and smtp_ok in ("ACCEPT", "TRUE") and catch_all == "FALSE" and mx_pass == "TRUE"


def is_provisional_active_email(row: dict) -> bool:
    """Hosted discovery proxy eligibility when SMTP certainty is unavailable."""
    smtp_ok = (row.get("smtp_ok") or "").strip().upper()
    smtp_status = (row.get("smtp_status") or "").strip().lower()
    mx_pass = (row.get("mx_pass") or "").strip().upper()
    if smtp_ok in ("REJECT", "FALSE"):
        return False
    return mx_pass == "TRUE" and smtp_status in ("transport_blocked", "mx_lookup_failed", "not_attempted", "")


def evaluate_contract(rows: list[dict], contract: dict) -> dict:
    required_signals = list(contract.get("required_signals", []))
    min_signal_companies = int(contract.get("min_unique_companies_per_signal", 0))
    min_contacts = int(contract.get("min_contacts_per_run", 0))
    min_companies = int(contract.get("min_unique_companies_per_run", 0))

    eligible_contacts = len(rows)
    unique_companies = {(r.get("company") or "").strip().lower() for r in rows if (r.get("company") or "").strip()}

    signal_company_map = {s: set() for s in required_signals}
    for row in rows:
        company = (row.get("company") or "").strip().lower()
        if not company:
            continue
        tags = {t.strip() for t in (row.get("signal_tag") or "").split(";") if t.strip()}
        for sig in required_signals:
            if sig in tags:
                signal_company_map[sig].add(company)

    deficits = []
    if eligible_contacts < min_contacts:
        deficits.append(f"real_active_contacts={eligible_contacts} < required={min_contacts}")
    if len(unique_companies) < min_companies:
        deficits.append(f"unique_companies={len(unique_companies)} < required={min_companies}")

    signal_counts = {}
    for sig, companies in signal_company_map.items():
        count = len(companies)
        signal_counts[sig] = count
        if count < min_signal_companies:
            deficits.append(f"signal={sig} unique_companies={count} < required={min_signal_companies}")

    return {
        "passed": len(deficits) == 0,
        "eligible_contacts": eligible_contacts,
        "unique_companies": len(unique_companies),
        "signal_company_counts": signal_counts,
        "deficits": deficits,
    }


def to_hubspot_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        email = (row.get("email") or "").strip()
        if not email:
            continue
        first, last = split_name(row.get("first_name", ""), row.get("last_name", ""), email)
        if not first:
            continue
        out.append({
            "Email": email,
            "First Name": first,
            "Last Name": last,
            "Company": (row.get("company") or "").strip(),
            "Job Title": (row.get("title") or "").strip(),
            "Phone": (row.get("phone") or "").strip(),
            "Website": (row.get("website") or row.get("domain") or "").strip(),
            "City": (row.get("city") or "Seattle").strip() or "Seattle",
            "Signal Tag": (row.get("signal_tag") or "manual_review").strip() or "manual_review",
            "LinkedIn URL": (row.get("linkedin_url") or "").strip(),
            "Notes": (row.get("score_breakdown") or "").strip(),
        })
    return out


def chunk_rows(rows: list[dict], size: int) -> list[list[dict]]:
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Contract-driven daily runner for real active emails")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--sos", help="Input SOS CSV")
    input_group.add_argument("--input-pool", help="Pre-built normalized input pool CSV")
    parser.add_argument("--state-dir", default="state", help="State directory for seen companies/emails")
    parser.add_argument("--output-root", default="daily_output", help="Output root")
    parser.add_argument("--run-name", default="", help="Optional run name suffix")
    parser.add_argument("--batch-size", type=int, default=40, help="Companies per pipeline batch")
    parser.add_argument("--max-batches", type=int, default=20, help="Maximum batch runs in one day")
    parser.add_argument("--target-active-emails", type=int, default=0, help="Override min real active emails target")
    parser.add_argument("--smtp", action="store_true", help="Enable SMTP probe (required for real active emails)")
    parser.add_argument("--mode", choices=["strict_verify", "hosted_discovery"], default="strict_verify",
                        help="strict_verify requires SMTP acceptance; hosted_discovery reports provisional metrics")
    parser.add_argument("--hunter-key", default="", help="Hunter API key")
    parser.add_argument("--skip-theharvester", action="store_true")
    parser.add_argument("--skip-dorks", action="store_true")
    parser.add_argument("--soft-report", action="store_true", help="Always exit 0 even when contract is not met")
    parser.add_argument("--update-state-on-failure", action="store_true", help="Persist seen state even if contract fails")
    args = parser.parse_args()

    if args.mode == "strict_verify" and not args.smtp:
        print("ERROR: --smtp is required for real active email mode.")
        return 2

    scripts_dir = Path(__file__).resolve().parent
    input_path = Path(args.input_pool or args.sos).resolve()
    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return 1

    contract = get_daily_run_contract()
    if args.target_active_emails > 0:
        contract["min_contacts_per_run"] = int(args.target_active_emails)

    state_dir = (scripts_dir / args.state_dir).resolve()
    output_root = (scripts_dir / args.output_root).resolve()
    seen_companies_path = state_dir / "seen_companies.csv"
    seen_emails_path = state_dir / "seen_emails.csv"

    seen_companies = load_seen_companies(seen_companies_path)
    seen_emails = load_seen_emails(seen_emails_path)

    sos_fields, sos_rows = read_csv_rows(input_path)
    candidate_rows = []
    for row in sos_rows:
        key = normalize_company_key(row)
        if not key:
            continue
        if key in seen_companies:
            continue
        candidate_rows.append(row)

    if not candidate_rows:
        print("No novel companies available for daily run.")
        return 0

    today = datetime.now().strftime("%Y%m%d")
    run_folder = f"{today}_{args.run_name}" if args.run_name else today
    out_dir = output_root / run_folder
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Novel company candidates: {len(candidate_rows)}")
    print(f"Contract target: {contract.get('min_contacts_per_run')} real active emails")

    batches = chunk_rows(candidate_rows, max(1, args.batch_size))[: max(1, args.max_batches)]
    all_scored: list[dict] = []
    used_company_keys: set[str] = set()

    for idx, batch in enumerate(batches, start=1):
        batch_dir = out_dir / f"batch_{idx:03d}"
        batch_dir.mkdir(parents=True, exist_ok=True)

        with TemporaryDirectory(prefix="daily_contract_batch_") as tmpdir:
            batch_input = Path(tmpdir) / "sos_batch.csv"
            write_csv_rows(batch_input, sos_fields, batch)

            cmd = [
                sys.executable,
                str(scripts_dir / "run_pipeline.py"),
                "--sos", str(batch_input),
                "--output-dir", str(batch_dir),
                "--min-level", "B",
                "--disable-contract-gates",
                "--mode", args.mode,
            ]
            if args.smtp:
                cmd.append("--smtp")
            if args.hunter_key:
                cmd.extend(["--hunter-key", args.hunter_key])
            if args.skip_theharvester:
                cmd.append("--skip-theharvester")
            if args.skip_dorks:
                cmd.append("--skip-dorks")

            print(f"Running batch {idx}/{len(batches)} with {len(batch)} companies")
            rc = subprocess.run(cmd, cwd=str(scripts_dir)).returncode
            if rc != 0:
                print(f"Batch {idx} failed (exit={rc}); continuing")
                continue

        _, batch_scored = read_csv_rows(batch_dir / "contacts_scored.csv")
        all_scored.extend(batch_scored)
        for row in batch:
            key = normalize_company_key(row)
            if key:
                used_company_keys.add(key)

        # Evaluate rolling progress on real active contacts not seen before.
        dedup_scored = unique_by_email(all_scored)
        active = [r for r in dedup_scored if is_real_active_email(r)]
        provisional = [r for r in dedup_scored if is_provisional_active_email(r)]
        active_novel = [r for r in active if (r.get("email") or "").strip().lower() not in seen_emails]
        contract_eval = evaluate_contract(active_novel, contract)
        contract_eval["provisional_contacts"] = len(provisional)

        print(
            "Progress: "
            f"active={contract_eval['eligible_contacts']} "
            f"provisional={contract_eval['provisional_contacts']} "
            f"companies={contract_eval['unique_companies']} "
            f"signals={contract_eval['signal_company_counts']}"
        )

        if contract_eval["passed"]:
            print("Contract achieved; stopping batch loop.")
            break

    dedup_scored = unique_by_email(all_scored)
    active = [r for r in dedup_scored if is_real_active_email(r)]
    provisional = [r for r in dedup_scored if is_provisional_active_email(r)]
    active_novel = [r for r in active if (r.get("email") or "").strip().lower() not in seen_emails]
    contract_eval = evaluate_contract(active_novel, contract)
    contract_eval["provisional_contacts"] = len(provisional)

    # Persist consolidated outputs
    if dedup_scored:
        write_csv_rows(out_dir / "contacts_scored_consolidated.csv", list(dedup_scored[0].keys()), dedup_scored)
    else:
        write_csv_rows(
            out_dir / "contacts_scored_consolidated.csv",
            ["email", "company", "source", "confidence_level", "smtp_ok", "catch_all", "signal_tag"],
            [],
        )

    if active_novel:
        write_csv_rows(out_dir / "real_active_emails.csv", list(active_novel[0].keys()), active_novel)
    else:
        write_csv_rows(
            out_dir / "real_active_emails.csv",
            ["email", "company", "source", "confidence_level", "smtp_ok", "catch_all", "signal_tag"],
            [],
        )

    hs_rows = to_hubspot_rows(active_novel)
    write_csv_rows(out_dir / "hubspot_import.csv", HUBSPOT_HEADERS, hs_rows)

    summary = {
        "generated_at": datetime.now().isoformat(),
        "input": str(input_path),
        "output_dir": str(out_dir),
        "batch_size": args.batch_size,
        "max_batches": args.max_batches,
        "batches_attempted": len(batches),
        "candidate_companies": len(candidate_rows),
        "used_companies": len(used_company_keys),
        "real_active_contacts": len(active_novel),
        "provisional_contacts": len(provisional),
        "contract": contract,
        "evaluation": contract_eval,
    }
    with (out_dir / "daily_contract_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    should_update_state = contract_eval["passed"] or args.update_state_on_failure
    if should_update_state:
        append_seen_companies(seen_companies_path, used_company_keys)
        append_seen_emails(
            seen_emails_path,
            {(r.get("email") or "").strip().lower() for r in active_novel if (r.get("email") or "").strip()},
        )

    print("\nDaily contract runner complete:")
    print(f"  output_dir={out_dir}")
    print(f"  real_active_contacts={len(active_novel)}")
    print(f"  provisional_contacts={len(provisional)}")
    print(f"  contract_passed={contract_eval['passed']}")
    if contract_eval["deficits"]:
        print("  deficits:")
        for d in contract_eval["deficits"]:
            print(f"    - {d}")
    print(f"  state_updated={should_update_state}")

    if args.soft_report:
        return 0
    return 0 if contract_eval["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
