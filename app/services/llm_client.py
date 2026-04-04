from __future__ import annotations
from openai import OpenAI

from app.core.config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_SITE_URL,
    OPENROUTER_APP_NAME,
)

def get_llm_client() -> OpenAI:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("Missing OPENROUTER_API_KEY env var")

    # OpenAI SDK + custom base_url is supported. :contentReference[oaicite:2]{index=2}
    return OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            # Recommended by OpenRouter (optional but good practice). :contentReference[oaicite:3]{index=3}
            "HTTP-Referer": OPENROUTER_SITE_URL,
            "X-Title": OPENROUTER_APP_NAME,
        },
    )