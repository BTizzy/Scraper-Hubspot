# Trillium Hiring — Lead Builder Pipeline v2

**The best open-source lead builder for [trilliumhiring.com](https://trilliumhiring.com)**

Build verified, HubSpot-ready contact lists for Seattle metro (King County) using only free tools, public records, and zero-cost APIs. Designed for 5-30 employee companies with active buying signals.

---

## Architecture

Inspired by how **Apollo.io** builds their 265M-contact database:

```
WA SOS Export (.csv)
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 1: Company Enrichment (OpenCorporates)                │
│  Step 2: Headcount Estimation (team page spider)            │
│  Step 3: Lawsuit Detection (CourtListener API)              │
│  Step 4: Rebrand Detection (OC prev names + website scan)   │
├─────────────────────────────────────────────────────────────┤
│  Step 5: WATERFALL CONTACT ENRICHMENT                       │
│    Source 1: theHarvester (30+ search engines)               │
│    Source 2: Team page email scraper                         │
│    Source 3: Officer name → email permutation                │
│    Source 4: Hunter.io (25 free/month)                       │
│    Source 5: DuckDuckGo dork search                          │
├─────────────────────────────────────────────────────────────┤
│  Step 6: Email Verification (MX + SMTP + catch-all)         │
│  Step 7: Freshness Scoring (A/B/C/D confidence tiers)       │
│  Step 8: HubSpot CSV Builder (quality-gated)                │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
hubspot_import.csv  ←  Ready to import into HubSpot
```

### How it compares to Apollo

| Capability | Apollo (paid) | This pipeline (free) |
|---|---|---|
| Data sources | 2M contributors + web crawl + 3rd party | 5-source waterfall (theHarvester, team pages, officer records, Hunter free, dorks) |
| Email verification | 7-step, 91% accuracy | MX + SMTP RCPT + catch-all detection |
| Freshness scoring | Real-time updates, 150M/month refresh | Exponential decay model + signal recency |
| Email permutation | Pattern-based + ML prediction | 10 B2B patterns + name inference from team pages |
| Confidence tiers | Verified / Semi-verified / Guessed | A / B / C / D with configurable thresholds |
| Buying signals | Intent data ($$) | Lawsuits, rebrands, new formations, headcount |
| Cost | $49-119/mo per user | $0 (only free APIs) |

---

## Setup

```bash
# 1. Clone and navigate
cd Scraper-Hubspot/scripts

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

### Optional: API keys

Set these environment variables for enhanced discovery:

```bash
# Hunter.io (25 free searches/month — https://hunter.io/api)
export HUNTER_API_KEY=your_key_here
```

---

## Quick Start

### One-command pipeline

```bash
# Basic (no API keys needed):
python run_pipeline.py --sos sos_export.csv

# Full power (SMTP verification + Hunter.io):
python run_pipeline.py --sos sos_export.csv --smtp --hunter-key YOUR_KEY

# Fast mode (skip slow sources, A+B leads only):
python run_pipeline.py --sos sos_export.csv --skip-theharvester --skip-dorks --min-level B

# Dry run (see plan without executing):
python run_pipeline.py --sos sos_export.csv --dry-run
```

### Individual scripts

```bash
# Enrich companies from WA SOS export
python collect_companies.py --input sos_export.csv --output companies_enriched.csv

# Estimate headcount from team pages
python estimate_headcount.py --input companies_enriched.csv --output companies_sized.csv

# Detect lawsuit signals
python find_lawsuits.py --input companies_sized.csv --output companies_lawsuits.csv

# Detect rebrands
python find_rebrands.py --input companies_lawsuits.csv --output companies_rebrands.csv

# Waterfall contact discovery (5 sources)
python waterfall_enricher.py --input companies_rebrands.csv --output contacts_raw.csv

# Verify emails (MX + optional SMTP)
python verify_emails.py --input contacts_raw.csv --output contacts_verified.csv --smtp

# Score freshness and confidence
python freshness_scorer.py --input contacts_verified.csv --output contacts_scored.csv --min-level B

# Build HubSpot CSV
python build_csv.py --input contacts_scored_qualified.csv --output hubspot_import.csv
```

### Email permutation (standalone)

```bash
# Single person
python email_permutator.py --first Jane --last Doe --domain seattlestudio.com

# Batch from CSV
python email_permutator.py --input officers.csv --output permuted_emails.csv --verify
```

---

## Input Format

**WA SOS Export** (`sos_export.csv`):
- Source: [WA Secretary of State CCFS](https://ccfs.sos.wa.gov) → Advanced Search → New Entity → County: King
- Required columns: `company_name`, `registered_date`
- Optional columns: `website`, `domain`, `officers` (JSON array)

---

## Output Files

| File | Description |
|---|---|
| `output/companies_enriched.csv` | WA SOS + OpenCorporates data |
| `output/companies_sized.csv` | + headcount estimate (5-30 filter) |
| `output/companies_lawsuits.csv` | + CourtListener lawsuit signals |
| `output/companies_rebrands.csv` | + rebrand detection signals |
| `output/contacts_raw.csv` | All discovered emails (5 sources) |
| `output/contacts_verified.csv` | + MX/SMTP verification scores |
| `output/contacts_scored.csv` | + freshness/confidence scores (A-D) |
| `output/contacts_scored_qualified.csv` | Only records meeting min confidence |
| `output/hubspot_import.csv` | **HubSpot-ready import file** |
| `output/rejects.csv` | Records that failed quality gates |

---

## File Reference

| Script | Purpose | Key Technique |
|---|---|---|
| `trillium_config.py` | ICP configuration (single source of truth) | Target cities, titles, signals, patterns, thresholds |
| `collect_companies.py` | Company enrichment | OpenCorporates API (WA jurisdiction) |
| `estimate_headcount.py` | Team size estimation | Team page spider (10 URL paths) |
| `find_lawsuits.py` | Litigation signal detection | CourtListener REST API |
| `find_rebrands.py` | Rebrand detection | OC previous names + website keyword scan |
| `waterfall_enricher.py` | **Multi-source contact discovery** | 5-source waterfall with dedup (theHarvester + h8mail inspired) |
| `email_permutator.py` | **Email pattern generation** | 10 B2B patterns + MX + SMTP verify + scoring |
| `verify_emails.py` | Email verification | MX + WHOIS age + SMTP RCPT + catch-all |
| `freshness_scorer.py` | **Confidence scoring** | Exponential decay + source quality + signal weight |
| `build_csv.py` | HubSpot CSV output | Quality gates + exact HubSpot headers |
| `run_pipeline.py` | **Pipeline orchestrator** | 8-step sequential runner with summary dashboard |

---

## Configuration

Edit `trillium_config.py` to customize:

- **Geography**: `TARGET_CITIES` — Seattle metro area municipalities
- **Company size**: `MIN_EMPLOYEES` / `MAX_EMPLOYEES` (default: 5-30)
- **Decision-maker titles**: `DM_TITLES` — owner, CEO, founder, HR director, etc.
- **Buying signals**: `SIGNALS` dict with priority weights (lawsuit=100, new_business=90, etc.)
- **Email patterns**: `EMAIL_PATTERNS` — 10 common B2B patterns
- **Freshness decay**: `FRESHNESS_HALF_LIFE_DAYS` — data loses half its value every N days (default: 90)
- **Confidence thresholds**: `VERIFICATION_LEVELS` — A/B/C/D tier definitions

---

## Design Principles

1. **Waterfall, not shotgun** — Sources are tried in priority order. Each adds what previous ones missed. (Inspired by theHarvester's 30+ module architecture)
2. **Score everything** — Every record gets a composite freshness score based on source quality, verification depth, signal strength, and data age. (Inspired by Apollo's 7-step verification)
3. **Fresh data wins** — Exponential decay means a 90-day-old lead is worth half as much. Signals like lawsuits and new filings push records to the top. (Inspired by Apollo's 150M/month refresh cycle)
4. **Zero cost** — Every tool and API used is free tier. Hunter.io is optional (25 free/month). No scrapy, no proxies, no paid databases.
5. **HubSpot-native** — Output CSV uses exact HubSpot import headers. Import → done.

---

## License

CC0 — use however you like.
