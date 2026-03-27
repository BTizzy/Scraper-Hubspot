"""trillium_config.py

Trillium Hiring Services — ICP configuration and signal definitions.

This is the single source of truth for:
  - Target geography
  - Company size range
  - Decision-maker titles
  - Buying signals and their priority weights
  - Industry verticals that matter for staffing/recruiting services
  - Freshness decay parameters

Designed for trilliumhiring.com B2B lead generation in the Seattle metro.
"""

# ── Geography ──────────────────────────────────────────────────────────────────
TARGET_CITIES = [
    "seattle", "bellevue", "redmond", "kirkland", "renton",
    "kent", "federal way", "tacoma", "bothell", "issaquah",
    "woodinville", "mercer island", "sammamish", "burien", "tukwila",
]
TARGET_COUNTY = "King"
TARGET_STATE = "WA"
TARGET_STATE_FULL = "Washington"

# ── Company Size ───────────────────────────────────────────────────────────────
MIN_EMPLOYEES = 5
MAX_EMPLOYEES = 25

# ── Decision-Maker Titles ──────────────────────────────────────────────────────
# Used for LinkedIn/team-page filtering and email prioritization
DM_TITLES = [
    "owner", "founder", "co-founder", "ceo", "president",
    "managing partner", "general manager", "gm", "principal",
    "managing director", "chief executive", "partner",
    "hr director", "hr manager", "human resources",
    "director of operations", "operations manager", "vp operations",
    "office manager", "controller", "cfo", "chief financial",
]

# ── Industry Verticals (relevant to Trillium staffing services) ────────────────
# These are used for search queries and relevance scoring
VERTICALS = [
    "staffing", "recruiting", "construction", "manufacturing",
    "logistics", "warehouse", "light industrial", "hospitality",
    "janitorial", "facility services", "landscaping", "hvac",
    "plumbing", "electrical", "general contractor",
    "food production", "packaging", "distribution",
    "property management", "cleaning services",
]

# ── Buying Signals ─────────────────────────────────────────────────────────────
# priority: higher = hotter lead. Used for scoring and sort order.
SIGNALS = {
    "active_lawsuit": {
        "priority": 100,
        "description": "Active civil litigation — urgent HR/compliance pain",
        "sources": ["courtlistener_v4", "duckduckgo_courtlistener_dork", "courtlistener_source"],
    },
    "new_business": {
        "priority": 90,
        "description": "New entity filing within last 18 months — building tech stack",
        "sources": ["oregon_sos", "sos_scraper", "opencorporates_api", "web_discovery", "courtlistener"],
    },
    "business_change": {
        "priority": 85,
        "description": "Business transfer, sale, rename, rebrand, or DBA filing — new vendor selection window",
        "sources": ["opencorporates_api", "wa_sos", "web_discovery"],
    },
}

# ── Freshness Decay ────────────────────────────────────────────────────────────
# Apollo refreshes 150M contacts/month. We can't match that, but we can score
# how stale our data is and prioritize re-verification.
FRESHNESS_HALF_LIFE_DAYS = 90   # confidence drops 50% after 90 days
MAX_DATA_AGE_DAYS = 365         # reject records older than this without re-check

# ── Email Verification Thresholds ──────────────────────────────────────────────
# Recalibrated for the data our free pipeline actually produces (MX-only mode):
#   - A great lead: MX-verified email from team page + signal + fresh data → ~0.60+
#   - A good lead: MX-verified email from any source + a signal → ~0.40+
#   - A usable lead: MX-verified officer permutation + signal → ~0.25+
#   - A risky lead: permuted email, no verification, no signal → <0.25
VERIFICATION_LEVELS = {
    "A": {"label": "Verified", "min_score": 0.55, "description": "MX verified + signal + fresh data"},
    "B": {"label": "Likely Valid", "min_score": 0.38, "description": "MX valid + source confidence"},
    "C": {"label": "Unverified", "min_score": 0.20, "description": "Email found but low confidence"},
    "D": {"label": "Risky", "min_score": 0.0, "description": "Permuted/unverified — test send first"},
}

# ── Email Permutation Patterns ─────────────────────────────────────────────────
# Common B2B email conventions. Used by email_permutator.py
# {first} = first name, {last} = last name, {fi} = first initial, {li} = last initial
EMAIL_PATTERNS = [
    "{first}@{domain}",
    "{first}.{last}@{domain}",
    "{first}{last}@{domain}",
    "{fi}{last}@{domain}",
    "{first}_{last}@{domain}",
    "{first}{li}@{domain}",
    "{fi}.{last}@{domain}",
    "{last}@{domain}",
    "{first}.{li}@{domain}",
    "{fi}{li}@{domain}",
]

# ── Generic / Role-Based Email Blocklist ───────────────────────────────────────
GENERIC_LOCAL_PARTS = [
    "info", "hello", "contact", "admin", "support", "office",
    "team", "sales", "noreply", "mail", "billing", "hr",
    "jobs", "careers", "press", "media", "marketing",
    "webmaster", "postmaster", "abuse", "security",
    "reception", "frontdesk", "general", "enquiries",
    "inquiries", "feedback", "help", "service", "accounts",
]

# ── Fake First Names Blocklist ─────────────────────────────────────────────────
FAKE_FIRST_NAMES = [
    "team", "admin", "sales", "the", "our", "company",
    "staff", "office", "service", "support", "manager",
]

# ── Hunter.io / Skrapp.io free tier limits ─────────────────────────────────────
HUNTER_FREE_SEARCHES = 25       # per month
SKRAPP_FREE_CREDITS = 100       # per month

# ── Daily Run Contract (hard gates) ───────────────────────────────────────────
# Operational SLO for production list generation.
# Relaxed to match realistic yield from free-tier enrichment sources.
# With ~20 input companies, expect ~10-15 contacts at B/C level.
DAILY_RUN_CONTRACT = {
    "enabled": True,
    "hard_fail": False,
    "min_contacts_per_run": 5,
    "min_unique_companies_per_run": 3,
    "count_confidence_levels": ["A", "B", "C"],
    "required_signals": [
        "new_business",
    ],
    "min_unique_companies_per_signal": 1,
}

# ── Execution Modes ───────────────────────────────────────────────────────────
# hosted_discovery: optimized for GitHub-hosted runners where SMTP probing is
# often transport-blocked. Produces provisional diagnostics and artifacts.
# strict_verify: requires mailbox-level SMTP acceptance for strict eligibility.
EXECUTION_MODES = {
    "hosted_discovery": {
        "use_smtp": False,
        "contract_hard_fail": False,
        "build_hubspot_export": False,
    },
    "strict_verify": {
        "use_smtp": True,
        "contract_hard_fail": True,
        "build_hubspot_export": True,
    },
}


def get_daily_run_contract() -> dict:
    """Return a defensive copy of the daily run gate contract."""
    return {
        **DAILY_RUN_CONTRACT,
        "count_confidence_levels": list(DAILY_RUN_CONTRACT.get("count_confidence_levels", [])),
        "required_signals": list(DAILY_RUN_CONTRACT.get("required_signals", [])),
    }


def get_execution_mode_config(mode: str) -> dict:
    """Return mode config, defaulting to strict_verify for unknown values."""
    key = (mode or "strict_verify").strip().lower()
    selected = EXECUTION_MODES.get(key, EXECUTION_MODES["strict_verify"])
    return dict(selected)

def get_signal_priority(tag: str) -> int:
    """Return priority weight for a signal tag, or 0 if unknown."""
    return SIGNALS.get(tag, {}).get("priority", 0)

def rank_signals(tags: list[str]) -> list[str]:
    """Sort signal tags by priority descending."""
    return sorted(tags, key=get_signal_priority, reverse=True)
