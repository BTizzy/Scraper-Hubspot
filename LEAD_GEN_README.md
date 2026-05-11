# Lead Generation Automation System

## Overview
This system automatically discovers, verifies, and enriches contact data for professional services firms (10-50 employees) using **100% free and open-source tools**. It's optimized to run locally with minimal power consumption.

## Target Customer Profile
- **Industries**: Law firms, CPA/accounting, consulting, financial advisory
- **Company Size**: 10-50 employees (no HR staff)
- **Decision Makers**: Managing Partners, HR Directors/Managers, Operations Managers, Practice Administrators

## Architecture

### 5-Stage Pipeline
1. **Company Discovery** - Find target companies using BuiltWith, LinkedIn, Google
2. **Contact Identification** - Extract decision maker names from company websites/LinkedIn
3. **Email Generation** - Create email permutations based on common patterns
4. **Email Verification** - Multi-layer verification (syntax, DNS, SMTP, disposable detection)
5. **Data Enrichment & Export** - Enrich data and populate Google Sheets

## Key Features
- ✅ **100% Free**: Uses only open-source tools and free API tiers
### Using Manual Company Import (Recommended for First Run)

If automated company discovery isn't working yet:

```bash
# 1. Edit companies.csv with your target companies
# (Use companies_template.csv as a reference)

# 2. The system will automatically detect and import it
python main.py --limit 10
```

This is the **easiest way to get started** while you're setting up automated discovery.

### Verify Installation

Before your first run:

```bash
python verify_installation.py
```

This checks that everything is configured correctly.
- ✅ **Intensive Verification**: Multi-stage email validation to avoid bad data
- ✅ **Low Resource**: Designed for local execution with minimal power usage
- ✅ **Rate Limited**: Respectful scraping with delays and backoff
- ✅ **Waterfall Enrichment**: Queries multiple sources for 80%+ accuracy
- ✅ **Deduplication**: Fuzzy matching to avoid duplicates

## Installation

### Prerequisites
- Python 3.8+
- pip package manager

### Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Configure settings
cp config.example.yml config.yml
# Edit config.yml with your Google Sheets credentials and targeting criteria
```

### Google Sheets API Setup
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (free)
3. Enable Google Sheets API
4. Create Service Account credentials
5. Download JSON key and save as `credentials.json`
6. Share your Google Sheet with the service account email

## Usage

### Basic Run
```bash
python main.py
```

### Advanced Options
```bash
# Dry run (don't write to sheets)
python main.py --dry-run

# Specify number of leads to find
python main.py --limit 50

# Resume from checkpoint
python main.py --resume
```

## Email Verification Strategy

### Multi-Layer Verification Process
1. **Syntax Validation**: RFC 5322 compliant email format
2. **Disposable Detection**: Check against 55,734+ disposable email providers
3. **DNS/MX Validation**: Verify domain has mail exchange records
4. **SMTP Verification**: Ping mail server without sending email
5. **Catch-All Detection**: Identify domains that accept all emails
6. **Role-Based Detection**: Flag generic emails (info@, admin@)

### Libraries Used
- `check-if-email-exists` (Rust-based, most reliable)
- `deep-email-validator` (TypeScript/Node.js backup)
- `mailchecker` (55K+ disposable email list)
- Custom SMTP verification layer

## Rate Limiting & Ethical Scraping
- 0.5-2 second delays between requests
- Exponential backoff on rate limit errors
- Respects robots.txt
- User-agent rotation
- Daily quota monitoring

## Data Quality Metrics
The system tracks:
- Email deliverability rate (target: >95%)
- Data completeness percentage
- Deduplication rate
- Enrichment match rate (target: 80%+)
- Source reliability scores

## Files Structure
```
lead-generation-automation/
├── main.py                 # Main orchestration script
├── config.yml             # Configuration file
├── requirements.txt       # Python dependencies
├── credentials.json       # Google Sheets credentials (gitignored)
├── modules/
│   ├── company_discovery.py   # Find target companies
│   ├── contact_finder.py      # Extract decision maker names
│   ├── email_generator.py     # Create email permutations
│   ├── email_verifier.py      # Multi-stage verification
│   ├── enrichment.py          # Waterfall enrichment
│   ├── deduplication.py       # Fuzzy matching dedup
│   └── sheets_writer.py       # Google Sheets integration
├── utils/
│   ├── rate_limiter.py        # Rate limiting utilities
│   ├── logger.py              # Logging configuration
│   └── validators.py          # Data validation helpers
└── data/
    ├── checkpoints/           # Resume capability
    └── logs/                  # Execution logs
```

## Known Limitations & Weaknesses

### 1. LinkedIn Access Limitations
- **Issue**: LinkedIn has strict rate limits and may require login
- **Mitigation**: Use public company pages, Google search operators, alternative directories
- **Manual Fallback**: For high-value leads, manual LinkedIn searches recommended

### 2. Email Verification Accuracy
- **Issue**: SMTP verification can be blocked by some mail servers
- **Risk**: ~5-10% false negatives (valid emails marked invalid)
- **Mitigation**: Use catch-all detection, multiple verification layers, confidence scoring

### 3. Data Freshness
- **Issue**: Contact data degrades ~30% annually
- **Mitigation**: Run weekly updates, track last verified date

### 4. Free API Quotas
- **Issue**: Limited to ~100-200 verifications/day across free tiers
- **Mitigation**: Batch processing, checkpoint/resume, prioritize high-value leads

### 5. Company Size Detection
- **Issue**: Hard to accurately determine 10-50 employee count from public data
- **Mitigation**: Use LinkedIn company page, BuiltWith technographics, manual verification for uncertain cases

### 6. Compliance Considerations
- **Issue**: GDPR/CCPA requirements for B2B data
- **Mitigation**: Document all sources, implement opt-out, use only public data
- **Recommendation**: Add legal review before large-scale outreach

## Performance Expectations
- **Speed**: ~10-20 leads/hour (due to rate limiting and verification)
- **Accuracy**: 80-90% email deliverability
- **Completeness**: 85-95% of required fields filled
- **CPU Usage**: <5% (mainly I/O bound, waiting on network)
- **Memory**: <200MB

## Troubleshooting

### High Bounce Rate
- Increase verification strictness in config
- Check if SMTP verification is working
- Verify email pattern detection logic

### Low Match Rate
- Add more data sources to waterfall
- Adjust search criteria (too narrow?)
- Check if target company websites are accessible

### Rate Limiting Errors
- Increase delay between requests
- Check if IP is blocked (use different network)
- Reduce daily quota

## Future Enhancements
- [ ] Add phone number enrichment (Kaspr free tier: 5/month)
- [ ] Integrate with Apollo.io free tier
- [ ] Add email pattern learning (ML-based prediction)
- [ ] Build simple web UI for monitoring
- [ ] Add Slack/email notifications

## License
MIT License - Free for commercial use

## Support
For issues, please check logs in `data/logs/` and review the configuration settings.
