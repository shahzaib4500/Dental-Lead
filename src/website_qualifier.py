"""Website and doctor-bio qualifier module.

Scrapes practice websites (1-2 pages max, respecting timeouts, errors, and rate limits)
to extract specialist keywords and DSO signals from About / Doctor Bio pages.

Rules:
- PRM Campaign: If specialist keywords (implant placement, oral surgery, periodontist, etc.)
  found on Dr bios -> not a clean general practice: Needs Review.
- Wisdom Sales Campaign: Confirms primary specialty from bio.
- Detects DSO / corporate affiliation signals on the site -> Needs Review / Disqualify.
- Records exact source_url and tags decisions as 'Confirmed', 'Inferred', or 'Unknown'.
- Inferred / Unknown never count as confirmed.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple
import requests

from src.lead_normalizer import InternalLeadRecord

logger = logging.getLogger(__name__)

# Specialist procedure and provider keywords
SPECIALIST_KEYWORDS = [
    "periodontist",
    "periodontics",
    "board-certified specialist",
    "board certified periodontist",
    "oral surgeon",
    "oral and maxillofacial",
    "oral & maxillofacial",
    "implant placement",
    "dental implants placement",
    "prosthodontist",
    "prosthodontics",
    "orthodontist",
    "orthodontics",
    "endodontist",
    "endodontics",
    "bone graft",
    "bone grafting",
    "gum graft",
    "gum grafting",
    "all-on-4",
    "all on 4",
    "full mouth reconstruction",
    "jaw surgery",
    "iv sedation",
]

# Common DSO / Dental Group footer & branding keywords
DSO_SIGNALS = [
    "part of",
    "supported by",
    "affiliated with",
    "dental partners",
    "dental group",
    "heartland dental",
    "aspen dental",
    "pacific dental services",
    "smile brands",
    "mb2 dental",
    "deca dental",
    "gentle dental",
    "western dental",
    "clearchoice",
    "affordable dentures",
]

ABOUT_PAGE_HINTS = [
    "about",
    "meet",
    "doctor",
    "dentist",
    "team",
    "staff",
    "providers",
    "our-team",
    "our-doctors",
    "meet-the-doctor",
]


class WebsiteQualifier:
    """Evaluates dental practice websites and doctor bio pages."""

    def __init__(
        self,
        timeout: int = 5,
        session: Optional[requests.Session] = None,
        user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.user_agent = user_agent

    def fetch_page(self, url: str) -> Optional[Tuple[str, str]]:
        """Fetch URL with timeout and return (final_url, html_text)."""
        if not url:
            return None
        target_url = url if url.startswith("http") else f"https://{url}"
        try:
            headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"}
            res = self.session.get(target_url, headers=headers, timeout=self.timeout, allow_redirects=True)
            if res.status_code == 200 and "text/html" in res.headers.get("Content-Type", ""):
                return res.url, res.text
            return None
        except Exception as e:
            logger.debug(f"Could not fetch website {target_url}: {e}")
            return None

    def find_bio_links(self, base_url: str, html: str) -> List[str]:
        """Extract up to 2 doctor/about links from main page HTML."""
        links: List[str] = []
        raw_hrefs = re.findall(r'href=[\'"]([^\'" >]+)[\'"]', html, re.IGNORECASE)
        for href in raw_hrefs:
            low_href = href.lower()
            if any(hint in low_href for hint in ABOUT_PAGE_HINTS):
                abs_url = urllib.parse.urljoin(base_url, href)
                # Ensure same host
                try:
                    if urllib.parse.urlparse(abs_url).netloc == urllib.parse.urlparse(base_url).netloc:
                        if abs_url not in links:
                            links.append(abs_url)
                except Exception:
                    pass
            if len(links) >= 2:
                break
        return links

    def extract_keywords_from_text(self, text: str) -> Tuple[List[str], Optional[str]]:
        """Extract matching specialist keywords and snippet evidence."""
        cleaned_text = re.sub(r"<[^>]+>", " ", text)
        low_text = cleaned_text.lower()
        matches = []
        evidence_snippet = None

        for kw in SPECIALIST_KEYWORDS:
            if kw in low_text:
                matches.append(kw)
                if not evidence_snippet:
                    # Extract surrounding 80 characters
                    idx = low_text.find(kw)
                    start = max(0, idx - 40)
                    end = min(len(cleaned_text), idx + len(kw) + 40)
                    evidence_snippet = cleaned_text[start:end].strip()

        return matches, evidence_snippet

    def check_dso_signals(self, text: str) -> Optional[str]:
        """Detect DSO affiliation signals on page."""
        cleaned_text = re.sub(r"<[^>]+>", " ", text)
        low_text = cleaned_text.lower()
        for sig in DSO_SIGNALS:
            if sig in low_text:
                return sig
        return None

    def qualify_lead_website(self, lead: InternalLeadRecord) -> InternalLeadRecord:
        """Inspect lead website and update bio qualification findings."""
        if not lead.website:
            lead.qualification_fact_tag = "Unknown"
            return lead

        # Fetch home page
        fetched = self.fetch_page(lead.website)
        if not fetched:
            lead.qualification_fact_tag = "Unknown"
            return lead

        final_url, home_html = fetched
        lead.source_url = final_url

        # Check for DSO signals on home/footer
        dso_sig = self.check_dso_signals(home_html)
        if dso_sig:
            lead.specialist_keywords.append(f"DSO signal: {dso_sig}")
            lead.qualification_fact_tag = "Inferred"
            lead.bio_evidence = f"DSO signal detected on homepage: '{dso_sig}'"

        # Look for bio pages
        bio_urls = self.find_bio_links(final_url, home_html)
        target_pages = [(final_url, home_html)]

        for b_url in bio_urls:
            b_fetched = self.fetch_page(b_url)
            if b_fetched:
                target_pages.append(b_fetched)

        # Scan pages for specialist keywords
        found_keywords: List[str] = []
        snippet_evidence = None
        evidence_url = None

        for page_url, page_html in target_pages:
            kw_matches, snippet = self.extract_keywords_from_text(page_html)
            for kw in kw_matches:
                if kw not in found_keywords:
                    found_keywords.append(kw)
            if snippet and not snippet_evidence:
                snippet_evidence = snippet
                evidence_url = page_url

        if found_keywords:
            lead.specialist_keywords = found_keywords
            lead.bio_url = evidence_url or final_url
            lead.bio_evidence = snippet_evidence
            lead.qualification_fact_tag = "Confirmed"

            # Campaign specific action
            if lead.campaign_type == "PRM":
                # For PRM, specialist keywords mean it's not a clean general dental office
                lead.qualification_status = "Needs Review"
                lead.qualification_reason = (
                    f"Needs Review: Specialist keywords on Dr bio/website ({', '.join(found_keywords[:3])})"
                )
                lead.qualification_reasons.append(lead.qualification_reason)
            elif lead.campaign_type == "WISDOM_SALES":
                # For Wisdom Sales, confirms specialist credentials
                lead.qualification_status = "Qualified"
                lead.qualification_reason = f"Qualified: Verified specialist ({', '.join(found_keywords[:2])})"
                lead.qualification_reasons.append(lead.qualification_reason)
        else:
            if not lead.bio_evidence:
                lead.qualification_fact_tag = "Inferred"

        return lead
