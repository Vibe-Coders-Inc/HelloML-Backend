# api/rag.py

EMBED_MODEL = "text-embedding-3-small"   # 1536 dims

def chunk_text(text, chunk_size=1000, overlap=200):
    """
    Breaks text into overlapping chunks so embeddings preserve context.
    Example:
      text="ABCDEFGHIJ", chunk_size=5, overlap=2
      -> ["ABCDE", "CDEFG", "EFGHI", "GHIJ"]

    enforce 0 ≤ overlap < chunk_size
    """
    text = text.strip()
    if not text:
        return []

    # never let overlap >= chunk_size
    if overlap >= chunk_size:
        overlap = max(0, chunk_size - 1)
    
    chunks = []
    n = len(text)
    start = 0

    while start < n:
        end = min(start + chunk_size, n) # make sure you dont go out of bounds
        chunks.append(text[start:end])
        if end == n:
            break
        # adjust start with some overlap (easier context transitions)
        start = end - overlap
    
    return chunks

def embed_texts(client, texts):
    """
    Takes a list of strings, returns list of embeddings (each is a list of 1536 floats).
    Uses OpenAI's embedding endpoint to convert text -> vector representation.
    """
    if not texts:
        return []

    try:
        response = client.embeddings.create(model=EMBED_MODEL, input=texts)
    except Exception as e:
        raise RuntimeError(f"Embedding request failed: {e}") from e

    embeddings = []
    for item in response.data:
        embeddings.append(item.embedding)

    return embeddings

# sb = supabase client, ai = openai client
def upsert_document_text(sb, ai, agent_id, filename, text,
                              storage_url="", file_type="text/plain"):
    """
    Adds a new document and its vectorized chunks to the database.

    Steps:
      1. Insert a document record (linked to an agent).
      2. Split the text into overlapping chunks.
      3. Create embeddings for each chunk.
      4. Insert chunks + embeddings into 'document_chunk'.

    Returns:
      {"document_id": int, "chunks": int}
    """

    # Insert document (or update if unique constraint exists)
    # Check if document already exists for this agent + filename
    existing_doc = sb.table("document").select("id").eq("agent_id", agent_id).eq("filename", filename).execute()

    if existing_doc.data:
        # Update existing document
        doc_id = existing_doc.data[0]["id"]
        sb.table("document").update({
            "storage_url": storage_url,
            "file_type": file_type,
            "updated_at": "now()"
        }).eq("id", doc_id).execute()
    else:
        # Insert new document
        doc_res = sb.table("document").insert({
            "agent_id": agent_id,
            "filename": filename,
            "storage_url": storage_url,
            "file_type": file_type
        }).execute()

        if not doc_res.data:
            raise RuntimeError("Failed to insert document row.")
        doc_id = doc_res.data[0]["id"]

    # Split text into smaller chunks
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("No text found in document to embed.")

    # Remove any prior chunks for this document (so we truly upsert/replace)
    sb.table("document_chunk").delete().eq("document_id", doc_id).execute()

    # Generate embeddings in batches
    BATCH_SIZE = 64
    to_insert = []

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i:i + BATCH_SIZE]
        embeddings = embed_texts(ai, batch)
        if len(embeddings) != len(batch):
            raise RuntimeError("Embedding API returned unexpected number of vectors.")

        for j, (chunk, emb) in enumerate(zip(batch, embeddings)):
            to_insert.append({
                "document_id": doc_id,
                "chunk_index": i + j,
                "chunk_text": chunk,
                "embedding": emb  # Supabase auto-casts Python list -> pgvector
            })

    # Insert chunk embeddings into the table
    if to_insert:
        sb.table("document_chunk").insert(to_insert).execute()

    return {"document_id": doc_id, "chunks": len(to_insert)}


def semantic_search(sb, ai, agent_id, query, k=8, min_similarity=0.70):
    """
    Finds the most semantically similar chunks for a given query.

    Steps:
      1. Embed the query text.
      2. Call Postgres RPC 'match_document_chunks' for that agent.
      3. Return the most relevant chunks.
    """
    # Create query embedding
    q_emb_list = embed_texts(ai, [query]) # pass in query as a list of 1 string (customer stt)
                                          # q_emb_list is a list of size 1

    if not q_emb_list:
        return []
    q_emb = q_emb_list[0] # grab the embedding, full 1536-dim vector

    # Call Postgres RPC
    res = sb.rpc(
        "match_document_chunks",
        {
            "p_agent_id": agent_id,
            "p_query_embedding": q_emb,
            "p_match_count": k,
            "p_min_sim": min_similarity
        }
    ).execute()

    return res.data or []