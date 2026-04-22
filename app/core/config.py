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

# Pick a model available on OpenRouter, e.g.:
# "openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet", "google/gemini-1.5-pro", etc.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()

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
