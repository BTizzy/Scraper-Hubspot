"""Tests for the lead generation pipeline."""
import pytest


def test_import():
    """Verify the package imports correctly."""
    import scraper_hubspot
    assert scraper_hubspot is not None
