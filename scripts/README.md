# Trillium Lead Pipeline

Free, GitHub-hosted lead generation for Trillium's Seattle-area SMB outbound motion.

This repository is being built to replace an Apollo subscription with a workflow that:

- runs on GitHub-hosted runners
- uses free and public data sources
- scores contact quality instead of pretending every row is mailbox-verified
- proves launch readiness before any CRM write-path testing

HubSpot compatibility is intentionally not the focus of this phase. The current goal is simple: produce enough qualified leads, cheaply and repeatably, to support a target of 500 quality leads per week.

## What This Pipeline Does

The pipeline starts from a Washington SOS export, enriches company context, discovers contacts from multiple free sources, verifies what can be verified in a hosted environment, scores quality, and emits readiness artifacts.

Core stages:

1. Company enrichment
2. Headcount estimation
3. Lawsuit detection
4. Rebrand detection
5. Multi-source contact discovery
6. Email verification with MX-first logic and optional SMTP
7. Freshness and confidence scoring
8. Hosted readiness evaluation

The production launch path is `hosted_discovery`, not `strict_verify`.

## Launch Model

Two execution modes exist, but they are not equal:

- `hosted_discovery`: default mode, designed for GitHub-hosted runners, no SMTP requirement, used for launch readiness.
- `strict_verify`: optional internal validation mode, requires SMTP egress, useful for comparison and later CRM hardening.

The product promise for this phase is the hosted path. If the hosted path cannot hit weekly capacity with acceptable quality and source diversity, the product is not ready.

## Free-Path Commands

Setup:

```bash
cd /workspaces/Scraper-Hubspot/scripts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the hosted pipeline locally:

```bash
python run_pipeline.py \
    --sos test_data/sos_direct_evidence.csv \
    --mode hosted_discovery \
    --soft-report \
    --min-level C
```

Run the daily hosted wrapper:

```bash
python daily_run.py \
    --sos test_data/sos_direct_evidence.csv \
    --mode hosted_discovery \
    --min-level C
```

Run the contract-style hosted driver:

```bash
python daily_contract_runner.py \
    --sos test_data/sos_direct_evidence.csv \
    --mode hosted_discovery \
    --batch-size 40 \
    --max-batches 20 \
    --state-dir state \
    --output-root daily_output \
    --run-name contract_day
```

Optional strict verification path:

```bash
python run_pipeline.py \
    --sos test_data/sos_direct_evidence.csv \
    --mode strict_verify \
    --smtp \
    --min-level B
```

## GitHub Actions Path

The workflow lives in [.github/workflows/lead-pipeline.yml](/workspaces/Scraper-Hubspot/.github/workflows/lead-pipeline.yml).

Current intent:

- default benchmark dataset: `test_data/sos_direct_evidence.csv`
- hosted execution is the primary workflow path
- readiness evaluation runs after pipeline completion
- no self-hosted runner is required for launch proof

To build or refresh the benchmark from candidate company rows:

```bash
python build_benchmark_dataset.py \
    --input your_candidate_rows.csv \
    --output test_data/sos_direct_evidence.csv \
    --target-rows 25
```

This is what keeps the system aligned with the "100% free" requirement.

## Output Artifacts

Important outputs from a hosted run:

- `output/contacts_raw.csv`
- `output/contacts_verified.csv`
- `output/contacts_scored.csv`
- `output/contacts_scored_qualified.csv`
- `output/daily_kpi_report.json`
- `output/run_contract_report.json`
- `output/hosted_readiness_report.json`

The most important artifact is `hosted_readiness_report.json`. That file answers whether the hosted-only path is currently fit to launch.

## Readiness Criteria

Hosted readiness is evaluated against config in [trillium_config.py](/workspaces/Scraper-Hubspot/scripts/trillium_config.py), including:

- projected daily and weekly qualified lead volume
- minimum quality lead rate
- source diversity floor
- single-source dominance cap
- officer permutation share cap
- runtime budget

A pipeline run can look healthy in raw row count and still fail readiness if it is overly dependent on a weak source or if projected weekly throughput misses target.

## Data Quality Model

This system uses confidence tiers `A` through `D` plus source provenance and verification signals.

Important distinction:

- hosted quality means the row is acceptable for the free launch path
- strict quality means the row satisfies tighter mailbox-validity expectations with SMTP support

That distinction matters because GitHub-hosted runners cannot be treated as if they have self-hosted SMTP capabilities.

## Source Strategy

The contact acquisition layer is intentionally multi-source. The current architecture is built around free methods such as:

- theHarvester
- team and contact page extraction
- sitewide email scanning
- officer-name permutation fallback
- public search-based discovery

The quality model penalizes weak-source concentration so that volume cannot be faked by flooding the output with permutation-only rows.

## Test Commands

Regression suite:

```bash
python test_pipeline.py
```

The readiness evaluator has dedicated synthetic coverage in [test_pipeline.py](/workspaces/Scraper-Hubspot/scripts/test_pipeline.py).

## Scope For This Phase

In scope now:

- prove the hosted path can generate enough qualified leads
- keep the operating path fully free
- tighten readiness gates and artifact reporting
- improve acquisition quality and source diversity

Not in scope yet:

- HubSpot compatibility sign-off
- paid enrichment dependencies
- requiring self-hosted infrastructure to claim success

## Relevant Files

- [run_pipeline.py](/workspaces/Scraper-Hubspot/scripts/run_pipeline.py)
- [hosted_readiness.py](/workspaces/Scraper-Hubspot/scripts/hosted_readiness.py)
- [daily_run.py](/workspaces/Scraper-Hubspot/scripts/daily_run.py)
- [daily_contract_runner.py](/workspaces/Scraper-Hubspot/scripts/daily_contract_runner.py)
- [trillium_config.py](/workspaces/Scraper-Hubspot/scripts/trillium_config.py)
- [test_pipeline.py](/workspaces/Scraper-Hubspot/scripts/test_pipeline.py)

## Bottom Line

This repo should be judged like a product, not a scraper demo. The bar is not "did it emit a CSV"; the bar is whether the hosted, free path can reliably produce enough quality outbound leads each week to replace Apollo for this use case.
