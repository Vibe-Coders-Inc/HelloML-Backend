# api/crud/rag_endpoints.py

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional
from openai import OpenAI
from PyPDF2 import PdfReader
from ..database import supabase
from ..rag import upsert_document_text, semantic_search
import io
import os

router = APIRouter(prefix="/rag", tags=["RAG"])

# Initilize OpenAI client
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

def ensure_agent_exists(db, agent_id: int):
    row = db.table("agent").select("id").eq("id", agent_id).single().execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="Agent not found")

@router.post("/documents/text", summary="Upsert document table with plain text")
async def create_text_document(body: TextDocumentIn):
    try:
        print(f"[RAG] Uploading text document '{body.filename}' for agent {body.agent_id}")
        db = supabase()
        ensure_agent_exists(db, body.agent_id)

        result = upsert_document_text(
            db,
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
):
    try:
        print(f"[RAG] Uploading PDF document for agent {agent_id}")
        if "pdf" not in (file.content_type or "").lower():
            raise HTTPException(status_code=400, detail="Only PDF files are supported.")
        db = supabase()
        ensure_agent_exists(db, agent_id)

        pdf_bytes = await file.read()
        reader = PdfReader(io.BytesIO(pdf_bytes))
        full_text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        if not full_text:
            raise HTTPException(status_code=400, detail="No text could be extracted from the PDF.")

        final_name = filename or file.filename or "uploaded.pdf"
        print(f"[RAG] Processing PDF '{final_name}' with {len(full_text)} characters")
        result = upsert_document_text(
            db,
            ai,
            agent_id=agent_id,
            filename=final_name,
            text=full_text,
            file_type="application/pdf",
            storage_url=""
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
async def list_documents(agent_id: int):
    try:
        db = supabase()
        result = db.table("document") \
            .select("id, filename, file_type, uploaded_at") \
            .eq("agent_id", agent_id) \
            .order("uploaded_at", desc=True) \
            .execute()

        return result.data or []

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/documents/{document_id}", summary="Delete a document")
async def delete_document(document_id: int):
    """Delete a document and its chunks (cascade)."""
    try:
        db = supabase()
        existing = db.table("document").select("id").eq("id", document_id).single().execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Document not found")

        db.table("document").delete().eq("id", document_id).execute()
        return {"success": True, "deleted_id": document_id}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/semantic-search", summary="Semantic search over agent's documents")
async def perform_semantic_search(body: SearchRequest):
    """Run RAG semantic search for an agent."""
    try:
        db = supabase()

        matches = semantic_search(
            sb=db,
            ai=ai,
            agent_id=body.agent_id,
            query=body.query,
            k=body.k,
            min_similarity=body.min_similarity
        )

        return {"success": True, "matches": matches}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))