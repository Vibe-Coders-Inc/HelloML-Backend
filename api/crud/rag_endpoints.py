# api/crud/rag_endpoints.py

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from pydantic import BaseModel
from typing import Optional
from openai import OpenAI
from PyPDF2 import PdfReader
from ..database import get_service_client
from ..rag import upsert_document_text, semantic_search
from ..auth import get_current_user, AuthenticatedUser
import io
import os
import re
import asyncio
import httpx
from urllib.parse import urljoin, urlparse

router = APIRouter(prefix="/rag", tags=["RAG"])

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise ValueError("OPENAI_API_KEY not found in environment variables")

ai = OpenAI(api_key=api_key)


class TextDocumentIn(BaseModel):
    agent_id: int
    filename: str
    text: str
    file_type: Optional[str] = "text/plain"
    storage_url: Optional[str] = None


class SearchRequest(BaseModel):
    agent_id: int
    query: str
    k: Optional[int] = 10
    min_similarity: Optional[float] = 0.7


@router.post("/documents/text", summary="Upsert document table with plain text")
async def create_text_document(
    body: TextDocumentIn,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Upload a text document for RAG - user must own the agent"""
    try:
        db = current_user.get_db()
        service_db = get_service_client()

        # Verify ownership via RLS
        agent = db.table('agent').select('id').eq('id', body.agent_id).execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found or access denied")

        print(f"[RAG] Uploading text document '{body.filename}' for agent {body.agent_id}")

        # Use service client for upsert (writes to document and document_chunk)
        result = upsert_document_text(
            service_db,
            ai,
            agent_id=body.agent_id,
            filename=body.filename,
            text=body.text,
            file_type=body.file_type or "text/plain",
            storage_url=body.storage_url or ""
        )

        print(f"[RAG] Successfully created document {result['document_id']} with {result['chunks']} chunks")
        return {
            "success": True,
            "document_id": result["document_id"],
            "chunks_created": result["chunks"],
            "filename": body.filename
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/documents/pdf", summary="Upsert document table with pdf file")
async def create_pdf_document(
    agent_id: int = Form(...),
    file: UploadFile = File(...),
    filename: Optional[str] = Form(None),
    storage_url: Optional[str] = Form(None),
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Upload a PDF document for RAG - user must own the agent"""
    try:
        db = current_user.get_db()
        service_db = get_service_client()

        # Verify ownership via RLS
        agent = db.table('agent').select('id').eq('id', agent_id).execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found or access denied")

        print(f"[RAG] Uploading PDF document for agent {agent_id}")

        if "pdf" not in (file.content_type or "").lower():
            raise HTTPException(status_code=400, detail="Only PDF files are supported.")

        pdf_bytes = await file.read()
        reader = PdfReader(io.BytesIO(pdf_bytes))
        full_text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()

        if not full_text:
            raise HTTPException(status_code=400, detail="No text could be extracted from the PDF.")

        final_name = filename or file.filename or "uploaded.pdf"
        print(f"[RAG] Processing PDF '{final_name}' with {len(full_text)} characters")

        # Use service client for upsert
        result = upsert_document_text(
            service_db,
            ai,
            agent_id=agent_id,
            filename=final_name,
            text=full_text,
            file_type="application/pdf",
            storage_url=storage_url or ""
        )

        print(f"[RAG] Successfully created document {result['document_id']} with {result['chunks']} chunks")
        return {
            "success": True,
            "document_id": result["document_id"],
            "chunks_created": result["chunks"],
            "filename": final_name
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/documents/{agent_id}", summary="List all documents for an agent")
async def list_documents(
    agent_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """List all documents for an agent - user must own the agent"""
    try:
        db = current_user.get_db()

        # RLS will filter
        result = db.table("document") \
            .select("id, filename, file_type, storage_url, uploaded_at") \
            .eq("agent_id", agent_id) \
            .order("uploaded_at", desc=True) \
            .execute()

        return result.data or []

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/documents/{document_id}", summary="Delete a document")
async def delete_document(
    document_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Delete a document and its chunks - user must own the document's agent"""
    try:
        db = current_user.get_db()
        service_db = get_service_client()

        # Verify ownership via RLS
        existing = db.table("document").select("id").eq("id", document_id).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Document not found")

        # Use service client for delete (cascades to chunks)
        service_db.table("document").delete().eq("id", document_id).execute()

        return {"success": True, "deleted_id": document_id}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class WebsiteIndexRequest(BaseModel):
    agent_id: int
    url: str


CRAWL_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

PRIORITY_PATHS = [
    '/contact', '/contact-us', '/about', '/about-us',
    '/services', '/our-services', '/pricing', '/prices',
    '/faq', '/faqs', '/help', '/support',
    '/locations', '/location', '/hours',
    '/team', '/staff', '/menu', '/products',
]


def _strip_html(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<nav[^>]*>.*?</nav>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<footer[^>]*>.*?</footer>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _find_internal_links(html: str, base_url: str, max_links: int = 20) -> list[str]:
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower()
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)

    candidates = set()
    for href in hrefs:
        if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        full_url = urljoin(base_url, href).split('?')[0].split('#')[0]
        parsed = urlparse(full_url)
        if parsed.netloc.lower() != base_domain:
            continue
        path = parsed.path.rstrip('/').lower()
        if not path or path == parsed_base.path.rstrip('/').lower():
            continue
        if any(path.endswith(ext) for ext in ('.jpg', '.png', '.gif', '.svg', '.css', '.js', '.pdf', '.zip', '.xml')):
            continue
        candidates.add(full_url)

    # Sort: priority paths first
    priority = []
    other = []
    for url in candidates:
        path = urlparse(url).path.rstrip('/').lower()
        if any(path == p or path.endswith(p) for p in PRIORITY_PATHS):
            priority.append(url)
        else:
            other.append(url)

    return (priority + other)[:max_links]


async def _fetch_page(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    try:
        resp = await client.get(url, headers=CRAWL_HEADERS, timeout=10.0)
        resp.raise_for_status()
        return (url, resp.text)
    except Exception:
        return (url, '')


@router.post("/documents/website", summary="Crawl website and index for RAG")
async def index_website(
    body: WebsiteIndexRequest,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Crawl a website and its subpages, then index all content into the agent's knowledge base."""
    try:
        db = current_user.get_db()
        service_db = get_service_client()

        # Verify ownership
        agent = db.table('agent').select('id').eq('id', body.agent_id).execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found or access denied")

        url = body.url.strip()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        print(f"[RAG-Website] Starting crawl of {url} for agent {body.agent_id}")

        all_pages = []

        async with httpx.AsyncClient(follow_redirects=True) as client:
            # Fetch homepage
            try:
                resp = await client.get(url, headers=CRAWL_HEADERS, timeout=15.0)
                resp.raise_for_status()
                homepage_html = resp.text
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Could not fetch website: {str(e)}")

            homepage_text = _strip_html(homepage_html)
            all_pages.append({"url": url, "path": "/", "text": homepage_text})

            # Find and fetch subpages
            subpage_urls = _find_internal_links(homepage_html, url)
            if subpage_urls:
                tasks = [_fetch_page(client, sub_url) for sub_url in subpage_urls]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        continue
                    sub_url, sub_html = result
                    if not sub_html:
                        continue
                    sub_text = _strip_html(sub_html)
                    if len(sub_text) < 50:  # Skip near-empty pages
                        continue
                    path = urlparse(sub_url).path or sub_url
                    all_pages.append({"url": sub_url, "path": path, "text": sub_text})

        print(f"[RAG-Website] Crawled {len(all_pages)} pages from {url}")

        if not all_pages:
            raise HTTPException(status_code=400, detail="No content found on website")

        # Combine all pages into a single document with page markers
        combined_parts = []
        for page in all_pages:
            combined_parts.append(f"\n--- Page: {page['path']} ({page['url']}) ---\n{page['text']}")

        combined_text = "\n".join(combined_parts)
        domain = urlparse(url).netloc
        filename = f"website-{domain}"

        print(f"[RAG-Website] Indexing {len(combined_text)} chars from {len(all_pages)} pages as '{filename}'")

        # Upsert into RAG (replaces any existing website document for this agent)
        result = upsert_document_text(
            service_db,
            ai,
            agent_id=body.agent_id,
            filename=filename,
            text=combined_text,
            file_type="text/html",
            storage_url=url
        )

        print(f"[RAG-Website] Created document {result['document_id']} with {result['chunks']} chunks")

        return {
            "success": True,
            "document_id": result["document_id"],
            "chunks_created": result["chunks"],
            "pages_crawled": len(all_pages),
            "filename": filename
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/semantic-search", summary="Semantic search over agent's documents")
async def perform_semantic_search(
    body: SearchRequest,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Run RAG semantic search for an agent - user must own the agent"""
    try:
        db = current_user.get_db()
        service_db = get_service_client()

        # Verify ownership via RLS
        agent = db.table('agent').select('id').eq('id', body.agent_id).execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found or access denied")

        # Use service client for search (reads embeddings)
        matches = semantic_search(
            sb=service_db,
            ai=ai,
            agent_id=body.agent_id,
            query=body.query,
            k=body.k,
            min_similarity=body.min_similarity
        )

        return {"success": True, "matches": matches}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
