"""freshness_scorer.py

Score each contact record on data freshness and verification confidence.

Inspired by Apollo's approach:
  • Apollo refreshes 150M contacts/month, scores freshness by last-verified date
  • Their 7-step verification yields A/B/C/D tiers
  • They weight recent signals higher (job change = instant refresh)

Our open-source version scores based on:
  1. Signal recency     — how recently was a buying signal detected?
  2. Domain age         — fresh domains = new business = hot lead
  3. Collection date    — how old is the data we gathered?
  4. Verification depth — MX-only vs MX+SMTP vs MX+SMTP+RCPT
  5. Source quality     — team page > theHarvester > dork > permutation

The output is a confidence_level (A/B/C/D) per VERIFICATION_LEVELS in
trillium_config.py. Only A+B contacts go into HubSpot by default.

Usage:
  python freshness_scorer.py --input contacts_verified.csv --output contacts_scored.csv
"""
import argparse
import csv
import math
from datetime import datetime, timedelta

from trillium_config import (
    FRESHNESS_HALF_LIFE_DAYS,
    SIGNALS,
    VERIFICATION_LEVELS,
    get_signal_priority,
)

# ── Source quality weights ─────────────────────────────────────────────────────
# How much we trust each data source (0.0 – 1.0)
# Higher = more reliable = likely to be a real, active email

SOURCE_QUALITY = {
    'hunter.io': 0.90,        # professional tool, verified
    'team_page': 0.85,        # scraped from company's own site
    'theHarvester': 0.70,     # aggregates many search engines
    'officer_permutation': 0.45,  # guessed — unverified
    'google_dork': 0.40,      # found in wild — could be stale
    'manual': 1.0,            # human-added
}


# ── Time decay ─────────────────────────────────────────────────────────────────

def time_decay(days_old: float, half_life: float = FRESHNESS_HALF_LIFE_DAYS) -> float:
    """
    Exponential decay: data loses half its freshness value every `half_life` days.
    Returns a multiplier between 0.0 and 1.0.
    """
    if days_old <= 0:
        return 1.0
    return math.pow(0.5, days_old / half_life)


def days_since(date_str: str) -> float:
    """Parse a date string and return days since then."""
    if not date_str:
        return 365.0  # assume stale if no date
    for fmt in ['%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%m/%d/%Y', '%d-%m-%Y']:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return max(0, (datetime.now() - dt).days)
        except ValueError:
            continue
    return 365.0


# ── Verification depth scoring ─────────────────────────────────────────────────

def verification_score(mx_pass: bool, smtp_ok: str = '', catch_all: str = '') -> float:
    """
    Score based on how deeply the email was verified.
    MX only = 0.3, MX + SMTP accept = 0.7, MX + SMTP + not catch-all = 1.0
    """
    score = 0.0
    smtp_norm = str(smtp_ok or '').strip().upper()
    catch_norm = str(catch_all or '').strip().upper()
    if mx_pass:
        score = 0.3
        if smtp_norm in ('TRUE', 'ACCEPT'):
            score = 0.7
            if catch_norm == 'FALSE':
                score = 1.0  # best: SMTP accepted AND domain is NOT catch-all
            elif catch_norm == 'TRUE':
                score = 0.5  # catch-all dampens confidence
    return score


# ── Signal freshness ───────────────────────────────────────────────────────────

def signal_freshness_score(signal_tag: str, signal_date: str = '') -> float:
    """
    Combine signal priority with recency.
    A hot signal (lawsuit, new business) detected recently gets near 1.0.
    """
    priority = get_signal_priority(signal_tag)
    if priority == 0:
        return 0.0
    # Normalize priority to 0-1 range (max priority is 100)
    norm_priority = priority / 100.0
    # Apply time decay
    age = days_since(signal_date)
    decay = time_decay(age)
    return round(norm_priority * decay, 3)


# ── Composite score ────────────────────────────────────────────────────────────

def compute_freshness_score(row: dict) -> dict:
    """
    Compute a composite freshness/confidence score for a contact record.
    
    Returns updated dict with:
      freshness_score: 0.0 – 1.0
      confidence_level: A / B / C / D
      score_breakdown: human-readable explanation
    """
    # Component 1: Source quality (25% weight)
    source = row.get('source', '').lower()
    src_score = SOURCE_QUALITY.get(source, 0.5)  # default to 0.5, not 0.3

    # Guessed officer permutations should never count as strong leads unless SMTP
    # explicitly accepted the mailbox and the domain is not catch-all.
    smtp_value = str(row.get('smtp_ok', '')).upper()
    catch_all_value = str(row.get('catch_all', '')).upper()
    if source == 'officer_permutation' and not (smtp_value == 'ACCEPT' and catch_all_value == 'FALSE'):
        src_score = min(src_score, 0.30)
    
    # Component 2: Verification depth (25% weight)
    mx_pass = str(row.get('mx_pass', '')).lower() in ('true', '1', 'yes')
    vscore = str(row.get('verification_score', '')).upper()
    # Also count 'PASS' in verification_score as MX pass
    if 'PASS' in vscore:
        mx_pass = True
    v_score = verification_score(
        mx_pass,
        smtp_ok=str(row.get('smtp_ok', '')),
        catch_all=str(row.get('catch_all', ''))
    )
    
    # Component 3: Data freshness / collection date (25% weight)
    collection_date = row.get('collected_date', '') or row.get('date', '')
    collection_age = days_since(collection_date)
    freshness = time_decay(collection_age)
    
    # Component 4: Signal strength (25% weight)
    signal_tag = row.get('signal_tag', '') or row.get('Signal Tag', '')
    signal_date = row.get('signal_date', '') or row.get('collected_date', '') or row.get('date', '')
    # If multiple signals, use the best one
    if signal_tag:
        signals = [s.strip() for s in signal_tag.split(';') if s.strip()]
        sig_score = max(signal_freshness_score(s, signal_date) for s in signals) if signals else 0.0
    else:
        sig_score = 0.0
    
    # Weighted composite — equal weights since all matter for Trillium's use case
    composite = (
        0.25 * src_score +
        0.25 * v_score +
        0.25 * freshness +
        0.25 * sig_score
    )
    
    # Bonus: if record has a signal AND MX passes, bump score (these are the leads we want)
    if sig_score > 0 and mx_pass:
        composite = min(1.0, composite + 0.15)
    
    composite = round(composite, 3)
    
    # Map to confidence level
    confidence_level = 'D'
    for level in ['A', 'B', 'C', 'D']:
        if composite >= VERIFICATION_LEVELS[level]['min_score']:
            confidence_level = level
            break

    if source == 'officer_permutation' and not (smtp_value == 'ACCEPT' and catch_all_value == 'FALSE'):
        smtp_status_val = str(row.get('smtp_status', '')).lower()
        _transport_blocked = smtp_status_val in ('transport_blocked', 'mx_lookup_failed')
        if _transport_blocked:
            # TCP-level failure prevented SMTP verification — this is NOT a rejection.
            # The probe never reached the SMTP layer so we have no evidence the mailbox
            # is invalid.  Allow up to B-tier (composite must earn it via MX presence +
            # strong buying signal + fresh domain data).  Cap at B since we cannot
            # confirm the mailbox directly without a successful SMTP exchange.
            if confidence_level == 'A':
                confidence_level = 'B'
        elif confidence_level in ('A', 'B'):
            # SMTP was reachable but not confirmed (REJECT, soft_defer_4xx,
            # not_attempted, or empty smtp_ok).  Treat as unverified — hard cap at C.
            confidence_level = 'C'
    
    # Build breakdown
    breakdown = (
        f"src={src_score:.2f}({source or 'unknown'}) "
        f"ver={v_score:.2f}(mx={'✓' if mx_pass else '✗'}) "
        f"fresh={freshness:.2f}({int(collection_age)}d) "
        f"sig={sig_score:.2f}({signal_tag or 'none'})"
    )
    
    return {
        **row,
        'freshness_score': composite,
        'confidence_level': confidence_level,
        'score_breakdown': breakdown,
    }


# ── Batch scoring ──────────────────────────────────────────────────────────────

def score_file(input_csv: str, output_csv: str, min_level: str = 'C'):
    """
    Score every record in a CSV and write results.
    Also writes a separate file with only records meeting min_level threshold.
    """
    with open(input_csv, newline='', encoding='utf-8') as fin:
        reader = csv.DictReader(fin)
        rows = list(reader)
    
    scored = []
    level_counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
    
    for row in rows:
        result = compute_freshness_score(row)
        scored.append(result)
        level_counts[result['confidence_level']] = level_counts.get(result['confidence_level'], 0) + 1
    
    # Sort by score descending (best leads first)
    scored.sort(key=lambda r: r['freshness_score'], reverse=True)
    
    # Always create the full scored output, even when empty.
    if scored:
        fieldnames = list(scored[0].keys())
    else:
        fieldnames = ['email', 'first_name', 'last_name', 'company', 'title',
                      'source', 'freshness_score', 'confidence_level', 'score_breakdown']
    with open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scored)
    
    # Write filtered file (only records meeting min_level)
    # Filter by final confidence_level, not raw score, to respect demotions (e.g., officer_permutation → C)
    tier_order = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
    min_tier = tier_order.get(min_level, 3)
    qualified = [r for r in scored if tier_order.get(r.get('confidence_level', 'D'), 3) <= min_tier]
    filtered_path = output_csv.replace('.csv', f'_qualified.csv')
    # Always create the qualified file (even if empty) so downstream steps don't fail
    if qualified:
        fieldnames = list(qualified[0].keys())
    elif scored:
        fieldnames = list(scored[0].keys())
    else:
        fieldnames = ['email', 'first_name', 'last_name', 'company', 'title',
                      'source', 'freshness_score', 'confidence_level', 'score_breakdown']
    with open(filtered_path, 'w', newline='', encoding='utf-8') as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(qualified)
    
    # Summary
    print(f"\n📊 Freshness scoring complete:")
    print(f"  Total records: {len(scored)}")
    for level in ['A', 'B', 'C', 'D']:
        label = VERIFICATION_LEVELS.get(level, {}).get('label', level)
        print(f"  {level} ({label}): {level_counts.get(level, 0)}")
    print(f"\n  Qualified (≥{min_level}): {len(qualified)} → {filtered_path}")
    print(f"  Full scored output → {output_csv}")
    
    return scored


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Freshness & confidence scorer')
    parser.add_argument('--input', '-i', required=True, help='CSV with contacts to score')
    parser.add_argument('--output', '-o', default='contacts_scored.csv', help='Output CSV')
    parser.add_argument('--min-level', default='C', choices=['A', 'B', 'C', 'D'],
                        help='Minimum confidence level for qualified output (default: C)')
    args = parser.parse_args()
    
    score_file(args.input, args.output, min_level=args.min_level)

if __name__ == '__main__':
    main()
