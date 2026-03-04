"""
Tavily web search integration for live agent calls.
Used as a fallback when RAG knowledge base doesn't have the answer.
"""

import os
import httpx

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_SEARCH_URL = "https://api.tavily.com/search"


async def search_web(query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
    """
    Search the web using Tavily API.
    
    Args:
        query: Natural language search query
        max_results: Maximum number of results (1-10)
        search_depth: "basic" (fast, 1 credit) or "advanced" (thorough, 2 credits)
    
    Returns:
        dict with 'found', 'results' list, and 'answer' (Tavily's AI summary)
    """
    if not TAVILY_API_KEY:
        return {"found": False, "error": "Web search not configured"}

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": True,
        "include_raw_content": False,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(TAVILY_SEARCH_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        answer = data.get("answer", "")
        results = data.get("results", [])

        if not results and not answer:
            return {"found": False, "message": "No relevant results found on the web."}

        return {
            "found": True,
            "answer": answer,
            "results": [
                {
                    "title": r.get("title", ""),
                    "content": r.get("content", "")[:500],
                    "url": r.get("url", ""),
                }
                for r in results[:max_results]
            ],
        }

    except httpx.TimeoutException:
        return {"found": False, "error": "Web search timed out"}
    except Exception as e:
        print(f"[WebSearch] Error: {e}", flush=True)
        return {"found": False, "error": str(e)}
