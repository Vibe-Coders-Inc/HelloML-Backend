# api/crud/website_extract.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import httpx
import re
import json
import os
import asyncio
from urllib.parse import urljoin, urlparse
from ..auth import get_current_user, AuthenticatedUser

router = APIRouter(prefix="/extract", tags=["Website Extraction"])

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

# Subpages most likely to contain business info
PRIORITY_PATHS = [
    '/contact', '/contact-us', '/about', '/about-us',
    '/services', '/our-services', '/locations', '/location',
    '/hours', '/faq', '/team', '/staff',
]


class ExtractRequest(BaseModel):
    url: str


class ExtractedInfo(BaseModel):
    business_name: Optional[str] = None
    business_email: Optional[str] = None
    phone_number: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    services: Optional[list[str]] = None
    hours: Optional[str] = None
    pages_scanned: Optional[int] = None


def clean_text(text: str) -> str:
    """Remove excess whitespace and newlines."""
    return re.sub(r'\s+', ' ', text).strip()


def strip_html(html: str) -> str:
    """Remove scripts, styles, and HTML tags to get text."""
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    return clean_text(text)


def extract_emails(text: str) -> list[str]:
    """Find email addresses in text."""
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(pattern, text)
    ignore = {'example.com', 'domain.com', 'email.com', 'sentry.io', 'w3.org',
              'wixpress.com', 'googleapis.com', 'schema.org', 'facebook.com',
              'twitter.com', 'instagram.com', 'yoursite.com', 'yourdomain.com'}
    return list(dict.fromkeys(e for e in emails if not any(d in e.lower() for d in ignore)))


def extract_phones(text: str) -> list[str]:
    """Find US phone numbers."""
    patterns = [
        r'\+?1[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}',
        r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}',
    ]
    phones = []
    for p in patterns:
        phones.extend(re.findall(p, text))
    seen = set()
    result = []
    for ph in phones:
        digits = re.sub(r'\D', '', ph)
        if len(digits) >= 10 and digits not in seen:
            seen.add(digits)
            result.append(ph)
    return result


def extract_jsonld(html: str) -> dict:
    """Extract business-relevant JSON-LD structured data."""
    business_types = {
        'LocalBusiness', 'Organization', 'Corporation', 'ProfessionalService',
        'Store', 'Restaurant', 'MedicalBusiness', 'LegalService',
        'FinancialService', 'RealEstateAgent', 'AutoDealer', 'HealthAndBeautyBusiness',
        'HomeAndConstructionBusiness', 'SportsActivityLocation', 'EntertainmentBusiness',
    }
    matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    for match in matches:
        try:
            parsed = json.loads(match)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict):
                    item_type = item.get('@type', '')
                    if isinstance(item_type, list):
                        item_type = item_type[0] if item_type else ''
                    if item_type in business_types:
                        return item
                    # Check @graph
                    for g in item.get('@graph', []):
                        if isinstance(g, dict):
                            gt = g.get('@type', '')
                            if isinstance(gt, list):
                                gt = gt[0] if gt else ''
                            if gt in business_types:
                                return g
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def find_subpage_urls(html: str, base_url: str) -> list[str]:
    """Find internal links that likely contain business info."""
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower()

    # Find all href links
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)

    # Normalize and filter
    candidates = set()
    for href in hrefs:
        # Skip anchors, javascript, mailto, tel
        if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Must be same domain
        if parsed.netloc.lower() != base_domain:
            continue

        # Clean path
        path = parsed.path.rstrip('/').lower()
        if not path or path == parsed_base.path.rstrip('/').lower():
            continue

        # Skip asset paths
        if any(path.endswith(ext) for ext in ('.jpg', '.png', '.gif', '.svg', '.css', '.js', '.pdf', '.zip')):
            continue

        candidates.add(full_url.split('?')[0].split('#')[0])

    # Prioritize known business-info paths
    priority = []
    other = []
    for url in candidates:
        path = urlparse(url).path.rstrip('/').lower()
        if any(path == p or path.endswith(p) for p in PRIORITY_PATHS):
            priority.append(url)
        else:
            other.append(url)

    # Return priority pages first, then up to a few others
    return priority[:8] + other[:4]


async def fetch_page(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Fetch a page and return (url, html). Returns empty string on failure."""
    try:
        resp = await client.get(url, headers=HEADERS, timeout=10.0)
        resp.raise_for_status()
        return (url, resp.text)
    except Exception:
        return (url, '')


def extract_with_gpt(combined_text: str, url: str, pages_scanned: int) -> dict:
    """Use GPT-4o-mini to extract structured business info from multi-page text."""
    import openai

    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # Use more text since we have multiple pages
    truncated = combined_text[:12000]

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": """You are extracting business information from website content (scraped from multiple pages).
Return JSON with these fields (null if not found):
{
  "business_name": "official business name",
  "business_email": "primary contact email (not personal emails or noreply)",
  "phone_number": "main phone number with area code",
  "address": "full street address including city, state, zip",
  "description": "2-3 sentence description of what the business does, their specialties, and who they serve",
  "services": ["specific service 1", "specific service 2", ...],
  "hours": "business hours (e.g. Mon-Fri 8am-5pm, Sat 9am-1pm)"
}
Be thorough. Extract ALL services mentioned. For the description, be specific about what makes this business unique.
Only include information explicitly stated on the pages. Do not guess or infer."""
            },
            {
                "role": "user",
                "content": f"Website: {url} ({pages_scanned} pages scanned)\n\nCombined content:\n{truncated}"
            }
        ],
    )

    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, IndexError):
        return {}


@router.post("/website", summary="Extract business info from website")
async def extract_from_website(
    req: ExtractRequest,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Scrapes a website and its subpages to extract business information."""
    url = req.url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    all_text_parts = []
    all_emails = []
    all_phones = []
    jsonld_data = {}
    pages_scanned = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # 1. Fetch homepage first
        try:
            resp = await client.get(url, headers=HEADERS, timeout=15.0)
            resp.raise_for_status()
            homepage_html = resp.text
            pages_scanned = 1
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=400, detail=f"Could not fetch website: HTTP {e.response.status_code}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not fetch website: {str(e)}")

        # Extract JSON-LD from homepage
        jsonld_data = extract_jsonld(homepage_html)

        # Get text from homepage
        homepage_text = strip_html(homepage_html)
        all_text_parts.append(f"=== HOME PAGE ({url}) ===\n{homepage_text}")
        all_emails.extend(extract_emails(homepage_text))
        all_phones.extend(extract_phones(homepage_text))

        # Also check mailto: links directly from HTML
        mailto_matches = re.findall(r'mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', homepage_html, re.IGNORECASE)
        all_emails.extend(mailto_matches)

        # 2. Find and fetch subpages in parallel
        subpage_urls = find_subpage_urls(homepage_html, url)

        if subpage_urls:
            tasks = [fetch_page(client, sub_url) for sub_url in subpage_urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    continue
                sub_url, sub_html = result
                if not sub_html:
                    continue

                pages_scanned += 1
                sub_text = strip_html(sub_html)
                path = urlparse(sub_url).path
                all_text_parts.append(f"=== {path} ===\n{sub_text}")
                all_emails.extend(extract_emails(sub_text))
                all_phones.extend(extract_phones(sub_text))

                # Also check mailto: in subpage HTML
                mailto_matches = re.findall(r'mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', sub_html, re.IGNORECASE)
                all_emails.extend(mailto_matches)

                # Check for JSON-LD on subpages too
                if not jsonld_data:
                    jsonld_data = extract_jsonld(sub_html)

    # Dedupe emails and phones
    all_emails = list(dict.fromkeys(all_emails))
    all_phones = list(dict.fromkeys(all_phones))

    # Combine all text
    combined_text = '\n\n'.join(all_text_parts)

    # Use GPT for intelligent extraction from combined text
    gpt_result = {}
    if os.getenv("OPENAI_API_KEY"):
        try:
            gpt_result = extract_with_gpt(combined_text, url, pages_scanned)
        except Exception as e:
            print(f"[Website Extract] GPT extraction failed: {e}")

    # Merge results: JSON-LD > GPT > regex
    result = ExtractedInfo()

    result.business_name = jsonld_data.get('name') or gpt_result.get('business_name')
    result.business_email = (
        jsonld_data.get('email') or
        gpt_result.get('business_email') or
        (all_emails[0] if all_emails else None)
    )
    result.phone_number = (
        jsonld_data.get('telephone') or
        gpt_result.get('phone_number') or
        (all_phones[0] if all_phones else None)
    )

    # Address from JSON-LD
    jsonld_addr = jsonld_data.get('address', {})
    if isinstance(jsonld_addr, dict) and jsonld_addr:
        addr_parts = [
            jsonld_addr.get('streetAddress', ''),
            jsonld_addr.get('addressLocality', ''),
            jsonld_addr.get('addressRegion', ''),
            jsonld_addr.get('postalCode', ''),
        ]
        jsonld_address = ', '.join(p for p in addr_parts if p)
    else:
        jsonld_address = None

    result.address = jsonld_address or gpt_result.get('address')
    result.description = gpt_result.get('description') or jsonld_data.get('description')
    result.services = gpt_result.get('services')
    result.hours = gpt_result.get('hours')
    result.pages_scanned = pages_scanned

    return result
