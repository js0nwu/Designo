# api_handler.py
# !! TO BE GONE THROUGH AUTOMATION TESTING !! #
import asyncio
import os
import uuid
import logging
from random import shuffle
from dotenv import load_dotenv
import datetime # Import datetime
import pytz # For timezone-aware datetimes

from google.genai import types as google_genai_types
from google.adk.agents import Agent

import agents
import adk_utils

load_dotenv()
# --- Configuration for the Project Pool ---
num_projects_str = os.getenv('NUM_PROJECTS')
DEFAULT_NUM_PROJECTS = 6 # As you mentioned you have 6 now
# CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE = 3 # Old value
CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE = 10 # Client's new requirement: 10 requests per minute
DAILY_REQUEST_LIMIT_PER_KEY = 500 # New client requirement: 500 requests per day

if num_projects_str is None:
    logging.warning(f"NUM_PROJECTS environment variable not set. Defaulting to {DEFAULT_NUM_PROJECTS}.")
    NUM_PROJECTS = DEFAULT_NUM_PROJECTS
else:
    try:
        NUM_PROJECTS = int(num_projects_str)
        if NUM_PROJECTS <= 0:
            logging.warning(f"NUM_PROJECTS in .env ('{num_projects_str}') is not positive. Defaulting to {DEFAULT_NUM_PROJECTS}.")
            NUM_PROJECTS = DEFAULT_NUM_PROJECTS
    except ValueError:
        logging.warning(f"NUM_PROJECTS environment variable ('{num_projects_str}') is not a valid integer. Defaulting to {DEFAULT_NUM_PROJECTS}.")
        NUM_PROJECTS = DEFAULT_NUM_PROJECTS

MAX_CONCURRENT_REQUESTS_PER_KEY = 3 # Simultaneous active users per key
API_KEYS = []

for i in range(NUM_PROJECTS):
    key = os.getenv(f"GOOGLE_API_KEY_{i}")
    if not key:
        logging.warning(f"Missing GOOGLE_API_KEY_{i} in .env file. This key will not be part of the pool.")
    else:
        API_KEYS.append(key)

if not API_KEYS:
    logging.error("FATAL: No GOOGLE_API_KEY_i found for the api_handler pool. System will not function for pooled keys.")
    # Consider raising an exception or exiting if no keys are loaded for the pool.

logging.info(f"api_handler: Loaded {len(API_KEYS)} API Keys. Target NUM_PROJECTS: {NUM_PROJECTS}.")
logging.info(f"api_handler: Max concurrent users per key: {MAX_CONCURRENT_REQUESTS_PER_KEY}.")
logging.info(f"api_handler: Max new user sessions initiating per key per minute: {CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE}.")
logging.info(f"api_handler: Max daily requests per key: {DAILY_REQUEST_LIMIT_PER_KEY}.")


PROJECT_POOL = []
if API_KEYS:
    PROJECT_POOL = [
        {
            "api_key": API_KEYS[i],
            "id": f"pooled_project_{i+1}", # 1-based id
            "semaphore": asyncio.Semaphore(MAX_CONCURRENT_REQUESTS_PER_KEY),
            "session_start_timestamps": [], # Stores datetime objects of when sessions started (for per-minute)
            "rate_limit_new_sessions_per_minute": CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE,
            "requests_today_count": 0, # New: For daily rate limit
            "last_daily_reset_date": None # New: For daily rate limit reset
        }
        for i in range(len(API_KEYS))
    ]
    shuffle(PROJECT_POOL) # Shuffle for initial load distribution

available_projects_queue = asyncio.Queue()

async def initialize_project_pool():
    if not PROJECT_POOL:
        logging.warning("api_handler: Project pool is empty (no API keys loaded). Pooled keys unavailable.")
        return

    for project_token in PROJECT_POOL:
        await available_projects_queue.put(project_token)
    logging.info(f"api_handler: Project pool initialized. {available_projects_queue.qsize()} project tokens available in queue.")

    current_vertex_setting = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "True").lower()
    if current_vertex_setting != "false":
        logging.info(f"api_handler: Setting GOOGLE_GENAI_USE_VERTEXAI to 'False'. Was: '{os.getenv('GOOGLE_GENAI_USE_VERTEXAI')}'")
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
    else:
        logging.info(f"api_handler: GOOGLE_GENAI_USE_VERTEXAI is already 'False'.")


async def acquire_project():
    """
    Acquires an available project from the pool that meets concurrency,
    per-minute, and per-day rate limits. Waits if no such project is available.
    """
    if not PROJECT_POOL:
        logging.error("api_handler: Project pool is empty. Cannot acquire project.")
        raise Exception("api_handler: Project pool is not configured or empty.")

    while True: # Loop until a suitable project is acquired
        if available_projects_queue.empty():
            logging.info("acquire_project: Queue is empty, waiting for a project token to be released or become available...")
            await asyncio.sleep(0.1) # Small sleep while waiting for a token to be put back
            continue # Re-check queue after sleep

        # Get a project token from the queue. This does not block indefinitely if others are available.
        project_token = await available_projects_queue.get()
        project_id_log = project_token['id']

        now_utc = datetime.datetime.now(pytz.utc)
        today_utc_date = now_utc.date()

        # --- 1. Daily Rate Limit Check & Reset ---
        # Reset daily count if it's a new day for this specific key
        if project_token["last_daily_reset_date"] is None or project_token["last_daily_reset_date"] < today_utc_date:
            logging.info(f"acquire_project: Resetting daily count for {project_id_log}. Old date: {project_token['last_daily_reset_date']}.")
            project_token["requests_today_count"] = 0
            project_token["last_daily_reset_date"] = today_utc_date # Update to today's date

        if project_token["requests_today_count"] >= DAILY_REQUEST_LIMIT_PER_KEY:
            logging.info(f"acquire_project: Project {project_id_log} is daily rate-limited ({project_token['requests_today_count']}/{DAILY_REQUEST_LIMIT_PER_KEY}). Returning to queue.")
            await available_projects_queue.put(project_token) # Put it back at the end of the queue.
            await asyncio.sleep(0.1) # Small pause before trying another project to prevent tight looping on rate-limited keys
            continue # Try next project

        # --- 2. Per-Minute Rate Limit Check ---
        # Prune old timestamps (older than 60 seconds)
        project_token["session_start_timestamps"] = [
            ts for ts in project_token["session_start_timestamps"]
            if (now_utc - ts).total_seconds() < 60
        ]
        current_sessions_in_minute_window = len(project_token["session_start_timestamps"])

        if current_sessions_in_minute_window >= project_token["rate_limit_new_sessions_per_minute"]:
            logging.info(f"acquire_project: Project {project_id_log} is minute rate-limited ({current_sessions_in_minute_window}/{project_token['rate_limit_new_sessions_per_minute']} in last 60s). Returning to queue.")
            await available_projects_queue.put(project_token) # Put it back.
            await asyncio.sleep(0.1) # Small pause
            continue # Try next project

        # --- 3. Concurrency (Semaphore) Check ---
        try:
            # This will block until a semaphore slot is available for THIS specific project.
            # If we've reached here, all other rate limits passed, so we wait for concurrency.
            await project_token["semaphore"].acquire()

            # If we reach here, all checks passed and semaphore acquired.
            # Increment counts for this successful acquisition.
            project_token["session_start_timestamps"].append(now_utc) # Mark for per-minute
            project_token["requests_today_count"] += 1 # Mark for daily

            logging.info(f"acquire_project: Acquired project {project_id_log}. "
                         f"Concurrency slots taken: {MAX_CONCURRENT_REQUESTS_PER_KEY - project_token['semaphore']._value}/{MAX_CONCURRENT_REQUESTS_PER_KEY}. "
                         f"Minute sessions: {len(project_token['session_start_timestamps'])}/{project_token['rate_limit_new_sessions_per_minute']}. "
                         f"Daily requests: {project_token['requests_today_count']}/{DAILY_REQUEST_LIMIT_PER_KEY}.")
            return project_token # Successfully acquired!

        except Exception as e:
            # This path is generally for unexpected errors during semaphore acquisition
            logging.error(f"api_handler: Unexpected error acquiring semaphore for {project_id_log}: {e}", exc_info=True)
            await available_projects_queue.put(project_token) # Put token back
            await asyncio.sleep(0.1) # Small pause
            continue # Try next project


async def release_project(project_token):
    """Releases a project's concurrency semaphore slot and returns its token to the pool."""
    if project_token:
        try:
            project_token["semaphore"].release()
            logging.info(f"api_handler: Project {project_token['id']} concurrency slot released.")
        except Exception as e:
            logging.error(f"api_handler: Error releasing semaphore for {project_token.get('id', 'UNKNOWN')}: {e}", exc_info=True)
        finally:
            # Always try to put the token back in the queue, even if semaphore release failed (though it shouldn't)
            await available_projects_queue.put(project_token)
            # logging.debug(f"api_handler: Project {project_token['id']} token returned to queue. Queue size: {available_projects_queue.qsize()}")
    else:
        logging.warning("api_handler: Attempted to release a null project_token.")


# process_request_with_pooled_key_single_step is not directly used in your current app.py
# for the multi-step request, but I'll keep it consistent with the overall acquire/release pattern
# if it were to be used for isolated agent calls.
async def process_request_with_pooled_key_single_step(
    agent_to_run: Agent,
    user_content: google_genai_types.Content,
    user_id: str
):
    """
    Manages acquiring a key from the pool for a SINGLE agent step,
    respecting concurrency and new session rate limits, running the ADK interaction,
    and then releasing the key.
    Each call to this function is treated as a new "session" for rate limiting.
    """
    project_in_use = None
    request_log_id = str(uuid.uuid4())[:8]

    if not PROJECT_POOL:
        logging.error(f"api_handler [Req-{request_log_id}]: Cannot process. Project pool is empty.")
        return f"ADK_RUNTIME_ERROR: api_handler: Project pool is empty or not configured."

    try:
        project_in_use = await acquire_project()
        pooled_api_key = project_in_use["api_key"]
        project_id_log_tag = f"{project_in_use['id']}/Req-{request_log_id}"

        logging.info(f"api_handler [{project_id_log_tag}]: Using pooled key ...{pooled_api_key[-4:]} for agent '{agent_to_run.name}' for user '{user_id}'.")

        response = await adk_utils.run_adk_interaction(
            agent_to_run=agent_to_run,
            user_content=user_content,
            session_service_instance=adk_utils.session_service,
            user_id=user_id,
            api_key=pooled_api_key
        )
        logging.info(f"api_handler [{project_id_log_tag}]: Agent '{agent_to_run.name}' completed.")
        return response
    except Exception as e:
        project_id_for_error = project_in_use['id'] if project_in_use else 'N/A_NO_PROJECT_ACQUIRED'
        logging.error(f"api_handler [{project_id_for_error}/Req-{request_log_id}]: Error in process_request_with_pooled_key_single_step for '{agent_to_run.name}': {e}", exc_info=True)
        return f"ADK_RUNTIME_ERROR: Exception in api_handler processing request for '{agent_to_run.name}': {e}"
    finally:
        if project_in_use:
            await release_project(project_in_use)


__all__ = [
    "initialize_project_pool",
    "acquire_project",
    "release_project",
    # "process_request_with_pooled_key_single_step" # Commented out as it's not the primary entry for app.py
]