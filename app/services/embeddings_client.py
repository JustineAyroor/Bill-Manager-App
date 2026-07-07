"""
Embeddings client for the Bill Import v2 (RAG) pipeline.

Reuses the exact same OpenRouter credential/base URL as app/services/llm_client.py
(chat completions) - OpenRouter also exposes a real, OpenAI-compatible
POST /embeddings endpoint, so no new API key is required.

This module is intentionally the *only* place that knows how to call an
embedding model. app/services/vectorstore.py always passes precomputed
vectors into Chroma rather than letting Chroma compute its own (which would
pull in a local onnxruntime-based default embedder) - keeping the runtime
footprint down on the target e2-micro VM.
"""

from __future__ import annotations

from typing import List

from langchain_core.embeddings import Embeddings
from openai import OpenAI

from app.core.config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_SITE_URL,
    OPENROUTER_APP_NAME,
    OPENROUTER_EMBEDDING_MODEL,
)


def get_embeddings_client() -> OpenAI:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("Missing OPENROUTER_API_KEY env var")

    return OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": OPENROUTER_SITE_URL,
            "X-Title": OPENROUTER_APP_NAME,
        },
    )


def _accumulate_usage(usage_sink: dict | None, resp) -> None:
    """
    Mutates usage_sink in place to accumulate embedding token counts/cost
    (via `extra_body={"usage": {"include": True}}`) across multiple calls -
    used by the Bill Import v2 worker to report per-job embedding spend.
    """
    if usage_sink is None:
        return
    usage = getattr(resp, "usage", None)
    if not usage:
        return
    tokens = getattr(usage, "total_tokens", None) or 0
    cost = getattr(usage, "cost", None)
    usage_sink["embedding_tokens"] = usage_sink.get("embedding_tokens", 0) + tokens
    if cost is not None:
        usage_sink["embedding_cost_usd"] = usage_sink.get("embedding_cost_usd", 0.0) + cost


def embed_texts(texts: List[str], model: str | None = None, usage_sink: dict | None = None) -> List[List[float]]:
    """Embed a batch of texts via OpenRouter. Returns one vector per input text, same order."""
    texts = [t if isinstance(t, str) else str(t or "") for t in texts]
    if not texts:
        return []

    client = get_embeddings_client()
    embed_model = model or OPENROUTER_EMBEDDING_MODEL
    try:
        resp = client.embeddings.create(model=embed_model, input=texts, extra_body={"usage": {"include": True}})
    except Exception:
        # Usage-accounting passthrough isn't guaranteed to be accepted by
        # every provider - fall back to a plain call rather than failing.
        resp = client.embeddings.create(model=embed_model, input=texts)
    _accumulate_usage(usage_sink, resp)
    # OpenAI SDK returns `data` sorted by `index`; sort defensively anyway.
    ordered = sorted(resp.data, key=lambda d: d.index)
    return [list(d.embedding) for d in ordered]


def embed_text(text: str, model: str | None = None, usage_sink: dict | None = None) -> List[float]:
    vecs = embed_texts([text], model=model, usage_sink=usage_sink)
    return vecs[0] if vecs else []


class LangchainOpenRouterEmbeddings(Embeddings):
    """
    LangChain Embeddings implementation backed by embed_texts()/embed_text()
    above, so langchain-chroma calls OpenRouter instead of OpenAI directly.
    """

    def __init__(self, model: str | None = None):
        self.model = model or OPENROUTER_EMBEDDING_MODEL

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return embed_texts(texts, model=self.model)

    def embed_query(self, text: str) -> List[float]:
        return embed_text(text, model=self.model)
