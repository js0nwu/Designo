# adk_utils.py
import re
import base64
import asyncio
import uuid
import io
import os # Import os to modify environment variable
import json # Import json for parsing
import logging # Use logging instead of print for consistency
import copy
import requests

# --- ADK Imports ---
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.adk.agents import Agent # Import Agent for type hinting
from google.genai import types as google_genai_types # For Content/Part

# --- Local Imports ---
from config import APP_NAME


# --- ADK Session Service (Single instance for the application) ---
session_service = InMemorySessionService()
logging.info("ADK InMemorySessionService initialized.")

# --- Helper Function to Validate SVG (remains the same) ---
def is_valid_svg(svg_string):
    """
    Validates whether the input string is a plausible SVG content.
    """
    if not svg_string or not isinstance(svg_string, str):
        return False

    # Remove markdown-style code block indicators
    svg_clean = re.sub(r'^\s*```(?:svg|xml)?\s*', '', svg_string.strip(), flags=re.IGNORECASE)
    svg_clean = re.sub(r'\s*```\s*$', '', svg_clean, flags=re.IGNORECASE)

    svg_clean_lower = svg_clean.lower()

    # Check presence of basic opening and closing SVG tags
    has_svg_start = '<svg' in svg_clean_lower
    has_svg_end = '</svg>' in svg_clean_lower
    ends_with_gt = svg_clean.strip().endswith('>')
    starts_with_lt = svg_clean.strip().startswith('<')

    if has_svg_start and has_svg_end and ends_with_gt and starts_with_lt:
        return svg_clean.strip()
    else:
        return False


# Helper to parse JSON output, robust to markdown code blocks
def _parse_json_output(text: str) -> dict | None:
    """Attempts to parse a string as a JSON object, stripping markdown code blocks if present."""
    if not text or not isinstance(text, str):
        return None
    try:
        cleaned_text = text.strip()
        # Check for and strip markdown code block wrappers
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
        elif cleaned_text.startswith("```"):
            cleaned_text = cleaned_text[3:]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
        
        cleaned_text = cleaned_text.strip()
        return json.loads(cleaned_text)
    except json.JSONDecodeError:
        # logging.debug(f"JSON parsing failed for text: '{text[:50]}...'")
        return None
    except Exception as e:
        logging.error(f"Unexpected error during JSON parsing: {e}")
        return None

def _agent_with_model_override(agent_to_run: Agent, model_override: str | None) -> Agent:
    if not model_override:
        return agent_to_run

    model_override = model_override.strip()
    if not model_override or getattr(agent_to_run, "model", None) == model_override:
        return agent_to_run

    try:
        if hasattr(agent_to_run, "model_copy"):
            return agent_to_run.model_copy(update={"model": model_override})
        if hasattr(agent_to_run, "copy"):
            return agent_to_run.copy(update={"model": model_override})
    except Exception as clone_err:
        logging.warning(
            f"Could not clone agent '{agent_to_run.name}' with model override '{model_override}': {clone_err}. Falling back to shallow copy."
        )

    cloned_agent = copy.copy(agent_to_run)
    setattr(cloned_agent, "model", model_override)
    return cloned_agent

def _extract_content_for_openai(user_content: google_genai_types.Content) -> str | list:
    content_items = []

    for part in getattr(user_content, "parts", []) or []:
        text = getattr(part, "text", None)
        if text:
            content_items.append({"type": "text", "text": str(text)})
            continue

        inline_data = getattr(part, "inline_data", None)
        if inline_data:
            mime_type = getattr(inline_data, "mime_type", "application/octet-stream")
            data = getattr(inline_data, "data", None)
            if data:
                if isinstance(data, str):
                    encoded = data
                else:
                    encoded = base64.b64encode(data).decode("utf-8")
                content_items.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"}
                })

    if not content_items:
        return ""
    if len(content_items) == 1 and content_items[0]["type"] == "text":
        return content_items[0]["text"]
    return content_items

def _openai_chat_completion_request(base_url: str, api_key: str, payload: dict) -> str:
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.post(endpoint, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("OpenAI-compatible endpoint returned no choices.")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content if isinstance(item, dict))
    return str(content or "")

def _coerce_decision_agent_response(agent_to_run: Agent, final_response_text: str, user_id: str) -> dict | str:
    if agent_to_run.name != "intent_router_agent_v1" or not final_response_text or final_response_text.startswith(("AGENT_ERROR:", "ADK_RUNTIME_ERROR:")):
        return final_response_text

    parsed_json = _parse_json_output(final_response_text)
    if parsed_json:
        if "mode" in parsed_json and "modified_prompt" in parsed_json:
            valid_modes = ["create", "modify", "answer"]
            mode_val = parsed_json["mode"]
            if isinstance(mode_val, str) and mode_val.lower() in valid_modes:
                return parsed_json
            logging.warning(f"UID {user_id}: Decision agent returned invalid mode '{mode_val}'. Returning raw text.")
        else:
            logging.warning(f"UID {user_id}: Decision agent output missing 'mode' or 'modified_prompt' keys. JSON: {parsed_json}")
    else:
        logging.warning(f"UID {user_id}: Decision agent did not return valid JSON. Raw: {final_response_text[:50]}...")

    return final_response_text


# --- ADK Interaction Runner ---

async def run_adk_interaction(agent_to_run: Agent, user_content: google_genai_types.Content, session_service_instance: InMemorySessionService, user_id: str = "figma_user", api_key: str | None = None, model_override: str | None = None) -> dict | str | None:
    """
    Runs a single ADK agent interaction.
    """
    final_response_text = None
    session_id = f"session_{uuid.uuid4()}"
    original_api_key_env = os.environ.get("GOOGLE_API_KEY")
    agent_for_run = _agent_with_model_override(agent_to_run, model_override)

    try:
        session_service_instance.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

        if api_key:
            os.environ["GOOGLE_API_KEY"] = api_key

        runner = Runner(
            agent=agent_for_run,
            app_name=APP_NAME,
            session_service=session_service_instance
        )

        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_content
        ):
            # Handle final response
            if event.is_final_response():
                if event.content and event.content.parts:
                    parts_text = []
                    for part in event.content.parts:
                        # Defensive check: ensure part has 'text' attribute and is not None
                        if hasattr(part, 'text') and part.text:
                            parts_text.append(str(part.text))
                    final_response_text = "".join(parts_text)

                # Check for escalation on final response
                if event.actions and event.actions.escalate:
                    error_msg = f"Agent escalated: {event.error_message or 'No specific message.'}"
                    logging.warning(f"UID {user_id}: {error_msg}")
                    final_response_text = f"AGENT_ERROR: {error_msg}"
                break

            # Handle explicit escalation before final response
            elif event.actions and event.actions.escalate:
                 error_msg = f"Agent escalated before final response: {event.error_message or 'No specific message.'}"
                 logging.warning(f"UID {user_id}: {error_msg}")
                 final_response_text = f"AGENT_ERROR: {error_msg}"
                 break

    except Exception as e:
         # This catches internal ADK errors like 'str' object has no attribute 'get'
         err_str = str(e)
         logging.error(f"Exception during ADK run_async for agent '{agent_for_run.name}' for UID '{user_id}': {err_str}")
         final_response_text = f"ADK_RUNTIME_ERROR: {err_str}" 
    finally:
         if api_key:
             if original_api_key_env is None:
                 del os.environ["GOOGLE_API_KEY"]
             else:
                 os.environ["GOOGLE_API_KEY"] = original_api_key_env

         try:
             if session_service_instance.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id):
                 session_service_instance.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
         except Exception as delete_err:
             logging.warning(f"Failed to delete temporary session '{session_id}': {delete_err}")

    return _coerce_decision_agent_response(agent_for_run, final_response_text, user_id)

async def run_openai_compatible_interaction(agent_to_run: Agent, user_content: google_genai_types.Content, openai_settings: dict, user_id: str = "figma_user") -> dict | str | None:
    base_url = (openai_settings.get("base_url") or "").strip()
    api_key = (openai_settings.get("api_key") or "").strip()
    model = (openai_settings.get("model") or "").strip()

    if not base_url:
        return "ADK_RUNTIME_ERROR: OpenAI-compatible base URL is missing."
    if not model:
        return "ADK_RUNTIME_ERROR: OpenAI-compatible model name is missing."

    instruction = getattr(agent_to_run, "instruction", "") or ""
    tool_note = "\n\nWhen running through this OpenAI-compatible chat-completions path, external tools and ADK planners are unavailable. Produce the final answer directly without calling tools."
    messages = [
        {"role": "system", "content": f"{instruction}{tool_note}"},
        {"role": "user", "content": _extract_content_for_openai(user_content)}
    ]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 8192,
    }

    logging.info(f"UID {user_id}: Running OpenAI-compatible agent: {agent_to_run.name} (Model: {model}, Base URL: {base_url})")
    try:
        final_response_text = await asyncio.to_thread(
            _openai_chat_completion_request, base_url, api_key, payload
        )
    except Exception as e:
        err_str = str(e)
        logging.error(f"Exception during OpenAI-compatible run for agent '{agent_to_run.name}' for UID '{user_id}': {err_str}")
        final_response_text = f"ADK_RUNTIME_ERROR: {err_str}"

    return _coerce_decision_agent_response(agent_to_run, final_response_text, user_id)


__all__ = [
    "session_service",
    "is_valid_svg",
    "run_adk_interaction",
    "run_openai_compatible_interaction",
]
