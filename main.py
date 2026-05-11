"""
Main orchestration script for lead generation automation
"""

import argparse
import sys
import time
from pathlib import Path
import yaml
from typing import Dict, List

from utils.logger import setup_logger
from utils.rate_limiter import RateLimiter
from modules.company_discovery import CompanyDiscovery
from modules.contact_finder import ContactFinder
from modules.email_generator import EmailGenerator
from modules.email_verifier import EmailVerifier
from modules.enrichment import DataEnrichment
from modules.deduplication import Deduplicator
from modules.csv_writer import CSVWriter

logger = setup_logger(__name__)


class LeadGenerationPipeline:
    """Main pipeline orchestrating the lead generation process"""
    
    def __init__(self, config_path: str = "config.yml"):
        """Initialize the pipeline with configuration"""
        self.config = self._load_config(config_path)
        self.rate_limiter = RateLimiter(self.config)
        self.checkpoint_file = Path(self.config['checkpointing']['checkpoint_file'])
        
        # Initialize modules
        self.company_discovery = CompanyDiscovery(self.config, self.rate_limiter)
        self.contact_finder = ContactFinder(self.config, self.rate_limiter)
        self.email_generator = EmailGenerator(self.config)
        self.email_verifier = EmailVerifier(self.config, self.rate_limiter)
        self.enrichment = DataEnrichment(self.config, self.rate_limiter)
        self.deduplicator = Deduplicator(self.config)
        self.csv_writer = CSVWriter(self.config)
        
        self.stats = {
            'companies_found': 0,
            'contacts_identified': 0,
            'emails_generated': 0,
            'emails_verified': 0,
            'leads_enriched': 0,
            'leads_exported': 0,
            'duplicates_removed': 0
        }
    
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML file"""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            logger.info(f"Configuration loaded from {config_path}")
            return config
        except FileNotFoundError:
            logger.error(f"Config file not found: {config_path}")
            logger.info("Please copy config.example.yml to config.yml and configure it")
            sys.exit(1)
    
    def load_checkpoint(self) -> Dict:
        """Load checkpoint data for resume capability"""
        if not self.config['checkpointing']['enabled']:
            return {}
        
        if self.checkpoint_file.exists():
            import json
            with open(self.checkpoint_file, 'r') as f:
                checkpoint = json.load(f)
            logger.info(f"Resuming from checkpoint: {checkpoint.get('leads_processed', 0)} leads already processed")
            return checkpoint
        return {}
    
    def save_checkpoint(self, data: Dict):
        """Save checkpoint data"""
        if not self.config['checkpointing']['enabled']:
            return
        
        import json
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.checkpoint_file, 'w') as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Checkpoint saved: {data.get('leads_processed', 0)} leads processed")
    
    def run(self, dry_run: bool = False, limit: int = None, resume: bool = False):
        """
        Execute the full lead generation pipeline
        
        Args:
            dry_run: If True, don't write to Google Sheets
            limit: Maximum number of leads to generate
            resume: If True, resume from checkpoint
        """
        logger.info("=" * 80)
        logger.info("LEAD GENERATION AUTOMATION - STARTING")
        logger.info("=" * 80)
        
        checkpoint = self.load_checkpoint() if resume else {}
        processed_companies = set(checkpoint.get('processed_companies', []))
        existing_leads = checkpoint.get('leads', [])
        
        try:
            # Stage 1: Enhanced Company Discovery with Comet Automation
            logger.info("\n📊 STAGE 1: ENHANCED COMPANY DISCOVERY (COMET + PERPLEXITY)")
            logger.info("-" * 80)
            companies = self.company_discovery.find_companies(
                industries=self.config['target_profile']['industries'],
                size_range=(
                    self.config['target_profile']['company_size']['min_employees'],
                    self.config['target_profile']['company_size']['max_employees']
                ),
                locations=self.config['target_profile']['locations']
            )
            
            # Filter out already processed companies
            companies = [c for c in companies if c['domain'] not in processed_companies]
            
            if limit:
                companies = companies[:limit]
            
            self.stats['companies_found'] = len(companies)
            logger.info(f"✅ Found {len(companies)} target companies")
            
            all_leads = existing_leads.copy()
            
            # Process each company
            for idx, company in enumerate(companies, 1):
                logger.info(f"\n{'=' * 80}")
                logger.info(f"Processing Company {idx}/{len(companies)}: {company['name']}")
                logger.info(f"{'=' * 80}")
                
                # Stage 2: Enhanced Contact Discovery with Comet Automation
                logger.info("\n👤 STAGE 2: ENHANCED CONTACT DISCOVERY (COMET + PERPLEXITY)")
                contacts = self.contact_finder.find_contacts_with_comet(
                    company=company,
                    target_titles=self.config['target_profile']['job_titles']
                )
                self.stats['contacts_identified'] += len(contacts)
                logger.info(f"✅ Found {len(contacts)} decision makers")
                
                # Stage 3: Email Generation
                logger.info("\n📧 STAGE 3: EMAIL GENERATION")
                for contact in contacts:
                    emails = self.email_generator.generate_email_permutations(
                        first_name=contact['first_name'],
                        last_name=contact['last_name'],
                        domain=company['domain']
                    )
                    contact['email_candidates'] = emails
                    self.stats['emails_generated'] += len(emails)
                    logger.info(f"Generated {len(emails)} email candidates for {contact['first_name']} {contact['last_name']}")
                
                # Stage 4: Email Verification
                logger.info("\n✅ STAGE 4: EMAIL VERIFICATION")
                verified_contacts = []
                for contact in contacts:
                    best_email = None
                    best_score = 0
                    
                    for email in contact['email_candidates']:
                        verification = self.email_verifier.verify_email(email)
                        
                        if verification['is_valid'] and verification['confidence'] > best_score:
                            best_email = email
                            best_score = verification['confidence']
                            contact['verification_details'] = verification
                        
                        self.stats['emails_verified'] += 1
                    
                    if best_email:
                        contact['email'] = best_email
                        contact['confidence_score'] = best_score
                        verified_contacts.append(contact)
                        logger.info(f"✅ Verified: {best_email} (confidence: {best_score}%)")
                    else:
                        logger.warning(f"❌ No valid email found for {contact['first_name']} {contact['last_name']}")
                
                # Stage 5: Data Enrichment
                if verified_contacts:
                    logger.info("\n🔍 STAGE 5: DATA ENRICHMENT")
                    for contact in verified_contacts:
                        lead = {**company, **contact}
                        enriched_lead = self.enrichment.enrich_lead(lead)
                        all_leads.append(enriched_lead)
                        self.stats['leads_enriched'] += 1
                        logger.info(f"✅ Enriched lead: {enriched_lead['first_name']} {enriched_lead['last_name']}")
                
                # Mark company as processed
                processed_companies.add(company['domain'])
                
                # Save checkpoint periodically
                if idx % self.config['checkpointing']['save_interval'] == 0:
                    self.save_checkpoint({
                        'leads_processed': len(all_leads),
                        'processed_companies': list(processed_companies),
                        'leads': all_leads,
                        'stats': self.stats
                    })
            
            # Stage 6: Deduplication
            logger.info("\n\n🔄 STAGE 6: DEDUPLICATION")
            logger.info("-" * 80)
            unique_leads, duplicates = self.deduplicator.deduplicate(all_leads)
            self.stats['duplicates_removed'] = len(duplicates)
            logger.info(f"✅ Removed {len(duplicates)} duplicates, {len(unique_leads)} unique leads remain")
            
            # Stage 7: Export to CSV
            logger.info("\n\n📤 STAGE 7: EXPORT TO CSV")
            logger.info("-" * 80)
            
            if dry_run:
                logger.info("🔸 DRY RUN MODE - Not writing to CSV")
                logger.info(f"Would export {len(unique_leads)} leads")
                self._print_sample_leads(unique_leads[:5])
            else:
                # Filter by confidence threshold
                min_confidence = self.config['output']['min_confidence_to_export']
                export_leads = [
                    lead for lead in unique_leads 
                    if lead.get('confidence_score', 0) >= min_confidence
                ]
                
                logger.info(f"Exporting {len(export_leads)} leads (confidence >= {min_confidence}%)")
                self.csv_writer.write_leads(export_leads)
                self.stats['leads_exported'] = len(export_leads)
                logger.info(f"✅ Successfully exported {len(export_leads)} leads to CSV")
            
            # Print final statistics
            self._print_final_stats()
            
            # Clear checkpoint on successful completion
            if self.checkpoint_file.exists():
                self.checkpoint_file.unlink()
                logger.info("✅ Checkpoint cleared")
            
            logger.info("\n" + "=" * 80)
            logger.info("✅ LEAD GENERATION COMPLETED SUCCESSFULLY")
            logger.info("=" * 80)
            
        except KeyboardInterrupt:
            logger.warning("\n\n⚠️  Process interrupted by user")
            self.save_checkpoint({
                'leads_processed': len(all_leads) if 'all_leads' in locals() else 0,
                'processed_companies': list(processed_companies),
                'leads': all_leads if 'all_leads' in locals() else [],
                'stats': self.stats
            })
            logger.info("💾 Progress saved. Run with --resume to continue")
            sys.exit(0)
        except Exception as e:
            logger.error(f"❌ Pipeline failed: {str(e)}", exc_info=True)
            sys.exit(1)
    
    def _print_sample_leads(self, leads: List[Dict]):
        """Print sample leads for dry run"""
        logger.info("\nSample Leads Preview:")
        logger.info("-" * 80)
        for lead in leads:
            logger.info(f"  • {lead.get('first_name', 'N/A')} {lead.get('last_name', 'N/A')}")
            logger.info(f"    Email: {lead.get('email', 'N/A')}")
            logger.info(f"    Company: {lead.get('company', 'N/A')}")
            logger.info(f"    Title: {lead.get('title', 'N/A')}")
            logger.info(f"    Confidence: {lead.get('confidence_score', 0)}%")
            logger.info("")
    
    def _print_final_stats(self):
        """Print final execution statistics"""
        logger.info("\n\n" + "=" * 80)
        logger.info("📊 FINAL STATISTICS")
        logger.info("=" * 80)
        logger.info(f"  Companies Found:       {self.stats['companies_found']}")
        logger.info(f"  Contacts Identified:   {self.stats['contacts_identified']}")
        logger.info(f"  Emails Generated:      {self.stats['emails_generated']}")
        logger.info(f"  Emails Verified:       {self.stats['emails_verified']}")
        logger.info(f"  Leads Enriched:        {self.stats['leads_enriched']}")
        logger.info(f"  Duplicates Removed:    {self.stats['duplicates_removed']}")
        logger.info(f"  Leads Exported:        {self.stats['leads_exported']}")
        
        if self.stats['emails_verified'] > 0:
            success_rate = (self.stats['leads_enriched'] / self.stats['contacts_identified']) * 100
            logger.info(f"  Success Rate:          {success_rate:.1f}%")
        
        logger.info("=" * 80)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Automated Lead Generation System for Professional Services Firms"
    )
    parser.add_argument(
        '--config',
        default='config.yml',
        help='Path to configuration file (default: config.yml)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help="Run pipeline without writing to CSV"
    )
    parser.add_argument(
        '--limit',
        type=int,
        help='Maximum number of companies to process'
    )
    parser.add_argument(
        '--resume',
        action='store_true',
        help='Resume from last checkpoint'
    )
    
    args = parser.parse_args()
    
    # Initialize and run pipeline
    pipeline = LeadGenerationPipeline(config_path=args.config)
    pipeline.run(
        dry_run=args.dry_run,
        limit=args.limit,
        resume=args.resume
    )


if __name__ == "__main__":
    main()
