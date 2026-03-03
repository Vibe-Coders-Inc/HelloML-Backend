# api/crud/website_extract.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import httpx
import re
import json
import os
from ..auth import get_current_user, AuthenticatedUser

router = APIRouter(prefix="/extract", tags=["Website Extraction"])


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


def clean_text(text: str) -> str:
    """Remove excess whitespace and newlines."""
    return re.sub(r'\s+', ' ', text).strip()


def extract_emails(text: str) -> list[str]:
    """Find email addresses in text."""
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(pattern, text)
    # Filter out common false positives
    ignore = {'example.com', 'domain.com', 'email.com', 'sentry.io', 'w3.org'}
    return [e for e in emails if not any(d in e for d in ignore)]


def extract_phones(text: str) -> list[str]:
    """Find US phone numbers."""
    patterns = [
        r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}',
        r'\+?1[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}',
    ]
    phones = []
    for p in patterns:
        phones.extend(re.findall(p, text))
    # Dedupe and filter short matches
    seen = set()
    result = []
    for ph in phones:
        digits = re.sub(r'\D', '', ph)
        if len(digits) >= 10 and digits not in seen:
            seen.add(digits)
            result.append(ph)
    return result


def extract_with_gpt(html_text: str, url: str) -> dict:
    """Use GPT-4o-mini to extract structured business info from page text."""
    import openai

    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # Truncate to avoid token limits
    truncated = html_text[:8000]

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": """Extract business information from the website text. Return JSON with these fields (null if not found):
{
  "business_name": "string",
  "business_email": "string",
  "phone_number": "string",
  "address": "full street address string",
  "description": "1-2 sentence description of what the business does",
  "services": ["list", "of", "services"],
  "hours": "business hours if found"
}
Be precise. Only include information explicitly stated on the page."""
            },
            {
                "role": "user",
                "content": f"Website URL: {url}\n\nPage content:\n{truncated}"
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
    """Scrapes a website URL and extracts business information using regex + GPT."""
    url = req.url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch website: HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch website: {str(e)}")

    # Strip HTML tags for text analysis
    text_content = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text_content = re.sub(r'<style[^>]*>.*?</style>', '', text_content, flags=re.DOTALL | re.IGNORECASE)
    text_content = re.sub(r'<[^>]+>', ' ', text_content)
    text_content = clean_text(text_content)

    # Quick regex extraction
    emails = extract_emails(text_content)
    phones = extract_phones(text_content)

    # Also check for JSON-LD structured data
    jsonld_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    jsonld_data = {}
    for match in jsonld_matches:
        try:
            parsed = json.loads(match)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get('@type') in ('LocalBusiness', 'Organization', 'Corporation', 'ProfessionalService', 'Store', 'Restaurant'):
                        jsonld_data = item
                        break
            elif isinstance(parsed, dict):
                if parsed.get('@type') in ('LocalBusiness', 'Organization', 'Corporation', 'ProfessionalService', 'Store', 'Restaurant'):
                    jsonld_data = parsed
        except json.JSONDecodeError:
            pass

    # Use GPT for intelligent extraction
    gpt_result = {}
    if os.getenv("OPENAI_API_KEY"):
        try:
            gpt_result = extract_with_gpt(text_content, url)
        except Exception as e:
            print(f"[Website Extract] GPT extraction failed: {e}")

    # Merge results: JSON-LD > GPT > regex
    result = ExtractedInfo()

    # Business name
    result.business_name = (
        jsonld_data.get('name') or
        gpt_result.get('business_name') or
        None
    )

    # Email
    result.business_email = (
        jsonld_data.get('email') or
        gpt_result.get('business_email') or
        (emails[0] if emails else None)
    )

    # Phone
    result.phone_number = (
        jsonld_data.get('telephone') or
        gpt_result.get('phone_number') or
        (phones[0] if phones else None)
    )

    # Address
    jsonld_addr = jsonld_data.get('address', {})
    if isinstance(jsonld_addr, dict):
        addr_parts = [
            jsonld_addr.get('streetAddress', ''),
            jsonld_addr.get('addressLocality', ''),
            jsonld_addr.get('addressRegion', ''),
            jsonld_addr.get('postalCode', ''),
        ]
        jsonld_address = ', '.join(p for p in addr_parts if p)
    else:
        jsonld_address = str(jsonld_addr) if jsonld_addr else None

    result.address = jsonld_address or gpt_result.get('address') or None

    # Description
    result.description = gpt_result.get('description') or jsonld_data.get('description') or None

    # Services
    result.services = gpt_result.get('services') or None

    # Hours
    result.hours = gpt_result.get('hours') or None

    return result
