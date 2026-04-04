import os

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# OpenRouter OpenAI-compatible endpoint
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()

# Optional but recommended by OpenRouter for attribution/limits/analytics
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "http://localhost:7860").strip()
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "tmobile-bill-manager").strip()

# Pick a model available on OpenRouter, e.g.:
# "openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet", "google/gemini-1.5-pro", etc.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()