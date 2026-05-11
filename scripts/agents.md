#Ralph Loop*

Before you go about answering any prompt

1. Ensure you understand the full state model

2. Research across github and the internet is encouraged. 

3. Save history of chat and version history here so it is visible for the next agent

4. If you want to tell the next agent anything leave it below

5. Always ensure the next agent has the same memory you do entering a task

6. If you learn something new add it as a skill file you can link it here so its easy for other agents to find

#Agent Notes

## 2026-03-10 Domain Discovery Implementation (in progress)

- Updated `collect_companies.py` domain discovery logic with these additions:
	- `normalize_domain()` to safely parse domains from URL-like strings.
	- `domain_has_dns()` using `socket.getaddrinfo` for fast DNS presence checks.
	- `try_domain_variants()` to test expanded TLDs:
		- classic (`.com`, `.net`, `.org`, `.co`, `.us`, `.biz`)
		- modern (`.io`, `.dev`, `.app`, `.tech`, `.software`, `.company`, `.business`)
		- international/trending (`.co.uk`, `.com.au`, `.ca`, `.de`, `.ch`, `.eu`, `.ai`, `.cloud`, `.digital`, `.online`, `.site`, `.space`)
	- Improved DuckDuckGo parsing with better domain normalization and broader skip-domain filtering.
	- Added LinkedIn company slug fallback (`linkedin.com/company/<slug>` -> `<slug>.com` guess).
	- Enhanced `discover_website()` flow:
		1. DDG result if DNS/MX/HTTP valid
		2. cleaned slug TLD variants
		3. hyphenated word variants
		4. short-name variant (first + last token)

- Validation completed:
	- `py_compile` passed for `collect_companies.py`.
	- Local smoke tests for helper functions passed.

- Pending:
	- Run networked coverage benchmark on `test_data/sos_realistic.csv` (previous attempts were cancelled by execution tool).

## 2026-03-10 Day1 vs Day2 Benchmark (completed)

- Ran direct benchmark on `test_data/sos_realistic.csv` comparing:
	- Day 1 baseline logic (old discovery flow approximation)
	- Day 2 current logic (`discover_website` after implementation)
- Results:
	- Day 1 coverage: `2/5` (40.0%)
	- Day 2 coverage: `5/5` (100.0%)
	- Delta: `+3` domains
- Per-company outcome:
	- Emerald City Roofing LLC: `<none>` -> `emeraldcityroofing.com`
	- Pacific Northwest Electric Inc: `pacificnorthwestelectric.com` -> `pacificnorthwestelectric.com`
	- Cascade Building Services LLC: `<none>` -> `cascadebuilding.com`
	- Sound Transit Staffing Group LLC: `<none>` -> `soundstaffing.com`
	- Puget Sound Plumbing Co: `pugetsoundplumbing.com` -> `pugetsoundplumbing.com`
- Benchmark artifact written: `output_realistic_eval/domain_day1_day2.json`

## 2026-03-10 Full Realistic Pipeline Run (day-2 snapshot)

- Executed:
	- `python run_pipeline.py --sos test_data/sos_realistic.csv --skip-theharvester --skip-dorks --min-level C --output-dir output_day2_eval`
- High-level outputs:
	- Companies enriched: `5`
	- Domains discovered: `5`
	- Contacts raw: `30` (officer permutation)
	- Contacts scored: `30` (all `C` tier)
	- HubSpot import: `0` rows (A/B quality gates intentionally strict)
- Contract gates failed due no A/B contacts and no lawsuit/rebrand positives in sample.

## 2026-03-10 Phase 1 Implementation (funnel diagnostics + SMTP observability)

- Implemented in `run_pipeline.py`:
	- Added end-to-end funnel summarization:
		- counts: raw/verified/scored/rejected
		- source mix: raw + scored
		- confidence mix
		- SMTP metrics: attempted/accepted/rejected/unknown/not_attempted + status counts
		- reject reason counts
	- HubSpot step now writes rejects into run output dir:
		- `--rejects <output_dir>/rejects.csv`
	- Added `daily_kpi_report.json` artifact for every run (even with contract gates disabled), including:
		- input profile
		- signal diagnostics
		- funnel diagnostics
		- contract evaluation (if enabled)

- Implemented in `verify_emails.py`:
	- Added SMTP retry/backoff in `smtp_check()`.
	- Added explicit SMTP observability fields in output:
		- `smtp_status`
		- `smtp_attempted`
	- Status values include examples like:
		- `accept_not_catchall`, `accept_catchall`, `reject_target`, `probe_failed`, `probe_exception`, `not_attempted`.

- Runtime bug fixed:
	- Corrected helper-function scope in `run_pipeline.py` (`summarize_funnel` and `value_counts` were accidentally nested).

- Validation run:
	- Command:
		- `python run_pipeline.py --sos test_data/sos_realistic.csv --skip-theharvester --skip-dorks --smtp --min-level C --disable-contract-gates --output-dir output_phase1_impl_fix`
	- Result:
		- `daily_kpi_report.json` produced successfully.
		- Funnel clearly shows current primary blocker:
			- `source_mix_raw`: 100% `officer_permutation`
			- `smtp.accepted`: 0
			- `smtp.attempt_status`: `probe_exception` for all rows
			- `reject_reasons`: 100% `Low confidence (C)`

- Next implementation priority:
	- Phase 2 source-yield improvements in `waterfall_enricher.py` (team-page and explicit-email discovery) to reduce dependency on officer permutations.

## 2026-03-10 Phase 2 Implementation (team-page source yield)

- Implemented in `waterfall_enricher.py`:
	- Added deeper candidate path discovery from homepage links (`discover_candidate_paths`) using hints like team/staff/leadership/about/contact/careers.
	- Added robust per-domain email extraction helper (`extract_emails_from_text`) supporting:
		- direct emails (`name@domain`)
		- common obfuscations (`name [at] domain`, `name(at)domain`, `name at domain`)
	- Upgraded `source_team_page()` to:
		- probe homepage and dynamically append discovered team/contact paths
		- extract `mailto:` and text/obfuscated emails
		- infer person context from nearby elements where possible
		- capture lightweight title hints from context
		- reduce crawl delay and avoid duplicate URL fetches

- Validation:
	- File diagnostics (`get_errors`) returned no issues for updated `waterfall_enricher.py`.

- Environment limitation encountered for week harness execution:
	- Long-running and even short Python execution calls were cancelled by the execution tool during attempted 7-day harness runs.
	- As a result, only previously completed real-run KPI artifacts are currently available for day-level metrics unless execution tools are restored.

	## 2026-03-10 SMTP Implementation Fix (started + validated)

	- Fixed concrete SMTP bug in `verify_emails.py`:
		- `smtp_check()` had inconsistent return arity (sometimes 2 values, sometimes 3), which could force broad `probe_exception` behavior.
		- Now always returns canonical triple: `(smtp_ok, catch_all, smtp_status)`.

	- Normalized SMTP semantics across pipeline:
		- `smtp_ok`: `ACCEPT` | `REJECT` | `UNKNOWN`
		- `catch_all`: `TRUE` | `FALSE` | `UNKNOWN`
		- Added/used diagnostic statuses such as: `mx_lookup_failed`, `probe_failed`, `reject_target`, `accept_not_catchall`, `accept_catchall`.

	- Updated scoring/export compatibility:
		- `freshness_scorer.py`: verification scoring now robust to canonical values while preserving backward compatibility with legacy `TRUE/FALSE`.
		- `build_csv.py`: officer-permutation quality gate accepts either canonical `ACCEPT` or legacy `TRUE` as SMTP-accepted values.

	- Validation:
		- `py_compile` passed for `verify_emails.py`, `freshness_scorer.py`, `build_csv.py`.
		- `test_pipeline.py` passed (`62 passed, 0 failed`).
		- Realistic pipeline rerun with SMTP:
			- command: `python run_pipeline.py --sos test_data/sos_realistic.csv --skip-theharvester --skip-dorks --smtp --min-level B --disable-contract-gates --output-dir output_smtp_fix_check`
			- `daily_kpi_report.json` shows SMTP states now cleanly classified as `mx_lookup_failed` (not generic `probe_exception`).
			- Confidence remained all `C` due zero SMTP accepts; current blocker is SMTP methodology/network reality, not tuple-shape bug.

	## 2026-03-27 GitHub Launch Hardening (completed)

	- Completed requested implementation items 1-3:
		1. Retry/backoff hardening for top-of-funnel signal collectors.
		2. GitHub workflow default input upgraded from 5-company sample to 30-company sample.
		3. Added explicit top-of-funnel source-collapse alerts in pipeline diagnostics.

	- File updates:
		- `find_lawsuits.py`
			- Added `get_with_retry()` with exponential backoff.
			- CourtListener and DDG queries now retry transient errors (`429`, `5xx`, network exceptions).
		- `find_rebrands.py`
			- Added `get_with_retry()` and applied to OpenCorporates API, HTML fallback, and website scans.
			- HTTPS→HTTP fallback now also uses retry path.
		- `run_pipeline.py`
			- Added `compute_top_of_funnel_alerts()`.
			- Emits alerts in console and writes `top_of_funnel_alerts` into `run_contract_report.json` and `daily_kpi_report.json`.
			- Alert keys include: `no_contacts_discovered`, `source_diversity_low_single_source`, `source_dominance_high:*`, `officer_permutation_only`.
		- `.github/workflows/lead-pipeline.yml`
			- Default `sos_path` now `test_data/sos_30companies.csv` (relative to `scripts/`).
			- Added shell-safe fallback for schedule/manual runs when inputs are empty.
		- `README.md` and `scripts/README.md`
			- Updated mode docs, latest run snapshots, workflow defaults, and top-of-funnel alert behavior.

	- Validation:
		- `python test_pipeline.py` passed.
		- `python run_pipeline.py --sos test_data/sos_realistic.csv --mode hosted_discovery --soft-report --output-dir output_hosted_smoke` passed.
		- `python -m py_compile run_pipeline.py daily_contract_runner.py build_csv.py` passed.

	- Current known constraints after hardening:
		- Hosted discovery still depends on network availability and free-source response quality.
		- Strict verify still requires SMTP-capable self-hosted runner for mailbox-level validity.