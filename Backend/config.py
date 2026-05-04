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
try:
    NUM_PROJECTS = int(os.getenv("NUM_PROJECTS") or 6)
except ValueError:
    NUM_PROJECTS = 6
POOLED_GOOGLE_API_KEYS = [
    os.getenv(f"GOOGLE_API_KEY_{i}") for i in range(NUM_PROJECTS)
    if os.getenv(f"GOOGLE_API_KEY_{i}")
]

# Essential API keys
if not GOOGLE_API_KEY and not POOLED_GOOGLE_API_KEYS:
     raise ValueError("Missing GOOGLE_API_KEY or GOOGLE_API_KEY_0 style pooled keys in .env file. At least one Gemini API key is required.")

# Configure ADK environment variables
if os.getenv("GOOGLE_GENAI_USE_VERTEXAI") is None:
     os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    if not os.getenv("GOOGLE_CLOUD_PROJECT") or not os.getenv("GOOGLE_CLOUD_LOCATION"):
        raise ValueError("GOOGLE_GENAI_USE_VERTEXAI=True requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION in .env file")


print(f"Google API Key set: {'Yes' if GOOGLE_API_KEY else 'No'} (Server Default)")
print(f"Pooled Google API Keys set: {len(POOLED_GOOGLE_API_KEYS)}")
print("ADK Environment Configured.")


# --- ADK Shared Configuration ---
APP_NAME = "figma_ai_assistant"
AGENT_MODEL = "gemini-flash-latest"
AGENT_MODEL_TOOL = "gemini-2.5-flash-preview-05-20"
AGENT_MODEL_PRO = "gemini-2.5-pro"
DECISION_MODEL = "gemini-flash-lite-latest"


# Export configuration variables
__all__ = [
    "GOOGLE_API_KEY",
    "POOLED_GOOGLE_API_KEYS",
    "APP_NAME",
    "AGENT_MODEL"
]
