# config.py
import os
import warnings
import logging
from dotenv import load_dotenv

# --- Configuration ---
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # Server's default API key
OPENAI_COMPAT_BASE_URL = os.getenv("OPENAI_COMPAT_BASE_URL", "").strip()
OPENAI_COMPAT_API_KEY = os.getenv("OPENAI_COMPAT_API_KEY", "").strip()
OPENAI_COMPAT_MODEL = os.getenv("OPENAI_COMPAT_MODEL", "gpt-4o").strip()
try:
    NUM_PROJECTS = int(os.getenv("NUM_PROJECTS") or 6)
except ValueError:
    NUM_PROJECTS = 6
POOLED_GOOGLE_API_KEYS = [
    os.getenv(f"GOOGLE_API_KEY_{i}") for i in range(NUM_PROJECTS)
    if os.getenv(f"GOOGLE_API_KEY_{i}")
]

# Configure ADK environment variables
if os.getenv("GOOGLE_GENAI_USE_VERTEXAI") is None:
     os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    if not os.getenv("GOOGLE_CLOUD_PROJECT") or not os.getenv("GOOGLE_CLOUD_LOCATION"):
        raise ValueError("GOOGLE_GENAI_USE_VERTEXAI=True requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION in .env file")


print(f"Google API Key set: {'Yes' if GOOGLE_API_KEY else 'No'} (Server Default)")
print(f"Pooled Google API Keys set: {len(POOLED_GOOGLE_API_KEYS)}")
print(f"OpenAI-compatible endpoint set: {'Yes' if OPENAI_COMPAT_BASE_URL else 'No'}")
print(f"OpenAI-compatible API key set: {'Yes' if OPENAI_COMPAT_API_KEY else 'No'}")
print("ADK Environment Configured.")


# --- ADK Shared Configuration ---
APP_NAME = "figma_ai_assistant"
AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-flash-latest")
AGENT_MODEL_TOOL = os.getenv("AGENT_MODEL_TOOL", "gemini-2.5-flash-preview-05-20")
AGENT_MODEL_PRO = os.getenv("AGENT_MODEL_PRO", "gemini-2.5-pro")
DECISION_MODEL = os.getenv("DECISION_MODEL", "gemini-flash-lite-latest")


# Export configuration variables
__all__ = [
    "GOOGLE_API_KEY",
    "POOLED_GOOGLE_API_KEYS",
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_API_KEY",
    "OPENAI_COMPAT_MODEL",
    "APP_NAME",
    "AGENT_MODEL",
    "AGENT_MODEL_TOOL",
    "AGENT_MODEL_PRO",
    "DECISION_MODEL"
]
