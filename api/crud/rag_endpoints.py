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
