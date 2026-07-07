import os

from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# OpenRouter OpenAI-compatible endpoint
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()

# Optional but recommended by OpenRouter for attribution/limits/analytics
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "http://localhost:7860").strip()
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "tmobile-bill-manager").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:7860").strip().rstrip("/")

# Pick a model available on OpenRouter, e.g. "openai/gpt-4o-mini",
# "anthropic/claude-sonnet-4.6", "google/gemini-2.5-pro", etc. Model slugs get
# retired/renamed over time - verify against https://openrouter.ai/api/v1/models
# if a model starts 404ing with "No endpoints found".
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()

# --- Bill Import v2 (RAG pipeline, opt-in) -------------------------------
# Embeddings also go through OpenRouter's OpenAI-compatible /embeddings
# endpoint, reusing OPENROUTER_API_KEY/OPENROUTER_BASE_URL - no new credential.
OPENROUTER_EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small").strip()

# How far back (in months) to look when retrieving historical precedent
# outcome facts for a plan.
VECTOR_RETRIEVAL_LOOKBACK_MONTHS = int(os.getenv("VECTOR_RETRIEVAL_LOOKBACK_MONTHS", "24"))

# Safety-net rate limit on new v2 bill-import job submissions per plan per
# hour. Content-hash caching already makes exact repeat-uploads free; this
# just guards against a runaway bug/loop. Bump temporarily via env var for a
# large historical-backfill session.
BILL_IMPORT_MAX_JOBS_PER_HOUR_PER_PLAN = int(os.getenv("BILL_IMPORT_MAX_JOBS_PER_HOUR_PER_PLAN", "10"))

# How long to keep BillImportJob.cleaned_text (a few KB of text, not a PDF).
# 0 = keep forever.
BILL_TEXT_RETENTION_DAYS = int(os.getenv("BILL_TEXT_RETENTION_DAYS", "0"))

TWILIO_ACCOUNT_SID = (
    os.getenv("TWILIO_ACCOUNT_SID")
    or os.getenv("TWILIO_SID")
    or ""
).strip()
TWILIO_AUTH_TOKEN = (
    os.getenv("TWILIO_AUTH_TOKEN")
    or os.getenv("TWILIO_TOKEN")
    or ""
).strip()
TWILIO_SMS_FROM = (
    os.getenv("TWILIO_SMS_FROM")
    or os.getenv("TWILIO_PHONE_NUMBER")
    or ""
).strip()
TWILIO_WHATSAPP_FROM = (
    os.getenv("TWILIO_WHATSAPP_FROM")
    or os.getenv("TWILIO_WHATSAPP_NUMBER")
    or ""
).strip()
TWILIO_STATUS_CALLBACK_URL = os.getenv("TWILIO_STATUS_CALLBACK_URL", "").strip()

# Whether enough credentials are present to attempt each Twilio channel. Used
# to gracefully hide SMS/WhatsApp in the UI (and skip them when sending)
# instead of failing at send time when Twilio access is unavailable/rejected.
TWILIO_SMS_CONFIGURED = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_SMS_FROM)
TWILIO_WHATSAPP_CONFIGURED = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM)
TWILIO_CONFIGURED = TWILIO_SMS_CONFIGURED or TWILIO_WHATSAPP_CONFIGURED
