# Scraper-Hubspot

Lead generation and enrichment pipeline for Trillium Hiring.

It is a multi-step data pipeline that takes WA SOS company exports, enriches company/contact data, scores lead quality, and builds HubSpot-ready CSV imports.

## What This Project Does

- Enriches company records from public sources.
- Detects buying signals (new formation, lawsuits, rebrand indicators).
- Discovers contacts using a waterfall approach.
- Verifies emails (MX, optional SMTP checks).
- Scores records into confidence levels (`A`/`B`/`C`/`D`).
- Outputs HubSpot import CSV plus rejects.

Primary implementation is in `scripts/`.

## Repository Layout

```
Scraper-Hubspot/
├── README.md
└── scripts/
		├── run_pipeline.py
		├── collect_companies.py
		├── estimate_headcount.py
		├── find_lawsuits.py
		├── find_rebrands.py
		├── waterfall_enricher.py
		├── verify_emails.py
		├── freshness_scorer.py
		├── build_csv.py
		├── trillium_config.py
		├── test_pipeline.py
		└── test_data/
```

For script-level usage details, see `scripts/README.md`.

## State Model (End-to-End)

The pipeline is stateful by file stage. Each step produces a CSV consumed by the next step.

1. `sos_input`
- File: WA SOS input CSV (`--sos`)
- Required key field: `company_name`

2. `companies_enriched`
- File: `companies_enriched.csv`
- Produced by: `collect_companies.py`
- Adds fields including:
	`website`, `domain`, `officers`, `opencorp_url`, `company_number`,
	`jurisdiction_code`, `current_status`, `registered_address`, `signal_tag`, `collected_date`

3. `companies_sized`
- File: `companies_sized.csv`
- Produced by: `estimate_headcount.py`
- Adds:
	`headcount_estimate`, `headcount_method`, `headcount_pass`

4. `companies_lawsuits`
- File: `companies_lawsuits.csv`
- Produced by: `find_lawsuits.py`
- Adds:
	`lawsuits_found`, `lawsuits_count`, `lawsuits_sample`
- May append `active_lawsuit` to `signal_tag`

5. `companies_rebrands`
- File: `companies_rebrands.csv`
- Produced by: `find_rebrands.py`
- Adds:
	`rebrand_flag`, `rebrand_reason`, `rebrand_sample`
- May append `rebrand` to `signal_tag`

6. `contacts_raw`
- File: `contacts_raw.csv`
- Produced by: `waterfall_enricher.py`
- Contact schema:
	`email`, `first_name`, `last_name`, `company`, `title`, `source`, `is_dm`,
	`hunter_confidence`, `signal_tag`, `registered_date`, `collected_date`, `domain`, `website`

7. `contacts_verified`
- File: `contacts_verified.csv`
- Produced by: `verify_emails.py`
- Adds:
	`mx_pass`, `reject_reason`, `verification_score`, `domain_age_days`, `smtp_ok`, `catch_all`

8. `contacts_scored`
- File: `contacts_scored.csv`
- Produced by: `freshness_scorer.py`
- Adds:
	`freshness_score`, `confidence_level`, `score_breakdown`

9. `contacts_scored_qualified`
- File: `contacts_scored_qualified.csv`
- Produced by: `freshness_scorer.py`
- Filtered by `--min-level` threshold

10. `hubspot_import`
- File: `hubspot_import.csv`
- Produced by: `build_csv.py`
- HubSpot headers:
	`Email`, `First Name`, `Last Name`, `Company`, `Job Title`, `Phone`,
	`Website`, `City`, `Signal Tag`, `LinkedIn URL`, `Notes`

11. `rejects`
- File: `rejects.csv`
- Produced by: `build_csv.py`
- Contains records filtered by quality gates plus `reject_reason`

## Last Run Snapshot (Executed 2026-03-27)

Recent verification runs were executed in both hosted discovery and strict verify paths.

Hosted discovery command used:

```bash
cd scripts
/home/codespace/.python/current/bin/python run_pipeline.py \
	--sos test_data/sos_realistic.csv \
	--mode hosted_discovery --soft-report \
	--output-dir output_hosted_smoke
```

Observed hosted-mode status:

- Step 1 `Company Enrichment`: `OK`
- Step 2 `Headcount Estimation`: `OK`
- Step 3 `Lawsuit Detection`: `OK` (CourtListener returned HTTP 403 for all lookups)
- Step 4 `Rebrand Detection`: `OK`
- Step 7 `Waterfall Contact Enrichment`: `OK`
- Step 8 `Email Verification`: `OK`
- Step 9 `Freshness Scoring`: `OK`
- HubSpot export: intentionally skipped in hosted mode

Hosted run totals:

- Companies enriched: `5`
- Companies sized: `5`
- Lawsuit rows: `5`
- Rebrand rows: `5`
- Contacts raw: `0`
- Contacts verified: `0`
- Contacts scored: `30` (all `C` in this sample)
- Contacts qualified: `0` at min-level `B`
- Top-of-funnel alert pattern: officer permutation dominance

Artifacts from that run are under `scripts/output_hosted_smoke/`.

Strict verify command used:

```bash
cd scripts
/home/codespace/.python/current/bin/python run_pipeline.py \
	--sos test_data/sos_realistic.csv \
	--mode strict_verify --smtp \
	--output-dir output_day1_impl
```

This path currently exits non-zero when contract gates are not met, as intended.

## Validation Run

The included test suite was also executed:

```bash
cd scripts
/home/codespace/.python/current/bin/python test_pipeline.py
```

Result: `43 passed, 0 failed`.

Current suite status after mode and diagnostics updates: `83 passed, 0 failed`.
