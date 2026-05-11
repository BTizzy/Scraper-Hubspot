"""hosted_readiness.py

Evaluate whether a GitHub-hosted discovery run is strong enough to support the
free production promise for Trillium's Apollo replacement path.

Inputs:
  - run_contract_report.json
  - daily_kpi_report.json

Outputs:
  - hosted_readiness_report.json
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from trillium_config import get_hosted_launch_targets


def load_json(path: str) -> dict[str, Any]:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def compute_readiness(contract_report: dict[str, Any], daily_kpi_report: dict[str, Any], targets: dict[str, Any] | None = None) -> dict[str, Any]:
    targets = dict(targets or get_hosted_launch_targets())

    input_profile = contract_report.get('input_profile', {}) or {}
    funnel = daily_kpi_report.get('funnel', {}) or {}
    evaluation = contract_report.get('evaluation', {}) or contract_report.get('contract_evaluation', {}) or {}
    alerts = list(contract_report.get('top_of_funnel_alerts', []) or daily_kpi_report.get('top_of_funnel_alerts', []) or [])

    candidate_companies = safe_int(input_profile.get('rows', 0))
    scored_contacts = safe_int((funnel.get('counts', {}) or {}).get('scored', 0))
    provisional_contacts = safe_int(evaluation.get('provisional_contacts', 0))
    unique_companies = safe_int(evaluation.get('provisional_unique_companies', evaluation.get('unique_companies', 0)))
    pipeline_duration = safe_float(contract_report.get('pipeline_duration_seconds', daily_kpi_report.get('pipeline_duration_seconds', 0.0)))

    source_mix = dict((funnel.get('source_mix_scored') or funnel.get('source_mix_raw') or {}))
    dominant_source = ''
    dominant_share = 0.0
    source_attribution_total = sum(safe_int(v) for v in source_mix.values())
    if source_attribution_total <= 0:
        source_attribution_total = scored_contacts

    if source_attribution_total > 0 and source_mix:
        dominant_source, dominant_count = max(source_mix.items(), key=lambda kv: safe_int(kv[1]))
        dominant_share = safe_int(dominant_count) / source_attribution_total

    officer_share = 0.0
    if source_attribution_total > 0:
        officer_share = safe_int(source_mix.get('officer_permutation', 0)) / source_attribution_total

    distinct_sources = len([k for k, v in source_mix.items() if k and safe_int(v) > 0 and k != '(empty)'])
    quality_lead_rate = (provisional_contacts / candidate_companies) if candidate_companies > 0 else 0.0
    projected_daily_quality_leads = round(quality_lead_rate * safe_int(targets.get('assumed_daily_company_capacity', 0)), 2)
    projected_weekly_quality_leads = round(projected_daily_quality_leads * 7, 2)

    checks = {
        'pipeline_completed': not bool(contract_report.get('pipeline_failed', daily_kpi_report.get('pipeline_failed', False))),
        'runtime_within_budget': pipeline_duration <= safe_float(targets.get('max_benchmark_runtime_seconds', 0)),
        'quality_lead_rate_ok': quality_lead_rate >= safe_float(targets.get('min_quality_lead_rate', 0)),
        'source_diversity_ok': distinct_sources >= safe_int(targets.get('min_distinct_sources', 0)),
        'single_source_dominance_ok': dominant_share <= safe_float(targets.get('max_single_source_dominance', 1.0)),
        'officer_share_ok': officer_share <= safe_float(targets.get('max_officer_permutation_share', 1.0)),
        'weekly_projection_ok': projected_weekly_quality_leads >= safe_float(targets.get('target_weekly_quality_leads', 0)),
        'unique_companies_ok': unique_companies >= safe_int((contract_report.get('contract', {}) or {}).get('min_unique_companies_per_run', 0)),
    }

    deficits = []
    if not checks['pipeline_completed']:
        deficits.append('pipeline_failed')
    if not checks['runtime_within_budget']:
        deficits.append(f"runtime_seconds={pipeline_duration:.1f} > budget={safe_float(targets.get('max_benchmark_runtime_seconds', 0)):.1f}")
    if not checks['quality_lead_rate_ok']:
        deficits.append(f"quality_lead_rate={quality_lead_rate:.3f} < required={safe_float(targets.get('min_quality_lead_rate', 0)):.3f}")
    if not checks['source_diversity_ok']:
        deficits.append(f"distinct_sources={distinct_sources} < required={safe_int(targets.get('min_distinct_sources', 0))}")
    if not checks['single_source_dominance_ok']:
        deficits.append(f"dominant_source={dominant_source}:{dominant_share:.2f} > max={safe_float(targets.get('max_single_source_dominance', 1.0)):.2f}")
    if not checks['officer_share_ok']:
        deficits.append(f"officer_permutation_share={officer_share:.2f} > max={safe_float(targets.get('max_officer_permutation_share', 1.0)):.2f}")
    if not checks['weekly_projection_ok']:
        deficits.append(f"projected_weekly_quality_leads={projected_weekly_quality_leads:.1f} < target={safe_float(targets.get('target_weekly_quality_leads', 0)):.1f}")
    if not checks['unique_companies_ok']:
        deficits.append(f"unique_companies={unique_companies} below contract minimum")
    if alerts:
        deficits.extend([f'alert:{a}' for a in alerts])

    return {
        'ready': all(checks.values()) and not alerts,
        'targets': targets,
        'checks': checks,
        'metrics': {
            'candidate_companies': candidate_companies,
            'scored_contacts': scored_contacts,
            'provisional_contacts': provisional_contacts,
            'unique_companies': unique_companies,
            'pipeline_duration_seconds': round(pipeline_duration, 3),
            'distinct_sources': distinct_sources,
            'dominant_source': dominant_source,
            'dominant_source_share': round(dominant_share, 4),
            'officer_permutation_share': round(officer_share, 4),
            'source_attribution_total': source_attribution_total,
            'quality_lead_rate': round(quality_lead_rate, 4),
            'projected_daily_quality_leads': projected_daily_quality_leads,
            'projected_weekly_quality_leads': projected_weekly_quality_leads,
        },
        'alerts': alerts,
        'deficits': deficits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Evaluate GitHub-hosted readiness from pipeline artifacts')
    parser.add_argument('--contract-report', required=True, help='Path to run_contract_report.json')
    parser.add_argument('--daily-kpi-report', required=True, help='Path to daily_kpi_report.json')
    parser.add_argument('--output', required=True, help='Path to write hosted_readiness_report.json')
    args = parser.parse_args()

    contract_report = load_json(args.contract_report)
    daily_kpi_report = load_json(args.daily_kpi_report)
    report = compute_readiness(contract_report, daily_kpi_report)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, sort_keys=True)

    print(f"Hosted readiness: {'READY' if report['ready'] else 'NOT_READY'}")
    print(f"Projected weekly quality leads: {report['metrics']['projected_weekly_quality_leads']}")
    if report['deficits']:
        for deficit in report['deficits']:
            print(f"  - {deficit}")
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())