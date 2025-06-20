import asyncio
import os
import uuid
import logging
from random import shuffle
from dotenv import load_dotenv
import datetime # Import datetime
import pytz # For timezone-aware datetimes
import time # Import time for timeout checks

from google.genai import types as google_genai_types
from google.adk.agents import Agent

import agents
import adk_utils

load_dotenv()

# --- Configuration for the Project Pool ---
num_projects_str = os.getenv('NUM_PROJECTS')
DEFAULT_NUM_PROJECTS = 6 # As you mentioned you have 6 now
CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE = 3 # Client's new requirement
MAX_CONCURRENT_REQUESTS_PER_KEY = 3 # Simultaneous active users per key
MAX_DAILY_REQUESTS_PER_KEY = 500 # NEW: Daily request limit per pooled API key
MAX_ACQUIRE_WAIT_TIME_SECONDS = 15 # NEW: Max time to wait for a pooled key before raising an error

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
logging.info(f"api_handler: Max daily requests per key: {MAX_DAILY_REQUESTS_PER_KEY}.")
logging.info(f"api_handler: Max wait time for key acquisition: {MAX_ACQUIRE_WAIT_TIME_SECONDS} seconds.")


PROJECT_POOL = []
if API_KEYS:
    PROJECT_POOL = [
        {
            "api_key": API_KEYS[i],
            "id": f"pooled_project_{i+1}", # 1-based id
            "semaphore": asyncio.Semaphore(MAX_CONCURRENT_REQUESTS_PER_KEY),
            "session_start_timestamps": [], # Stores datetime objects of when sessions started
            "rate_limit_new_sessions_per_minute": CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE,
            "requests_today": MAX_DAILY_REQUESTS_PER_KEY, # NEW: Daily request counter
            "last_reset_date": datetime.datetime.now(pytz.utc).date() # NEW: Date for daily reset
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
    new session rate limit, AND daily request limit.
    Waits if no such project is available, and raises an Exception if
    it cannot acquire a project within MAX_ACQUIRE_WAIT_TIME_SECONDS.
    """
    if not PROJECT_POOL:
        logging.error("api_handler: Project pool is empty. Cannot acquire project.")
        raise Exception("api_handler: Project pool is not configured or empty.")

    start_time = time.time()
    num_projects = len(PROJECT_POOL)
    checked_in_cycle = 0 # Tracks how many unique projects we've checked in a cycle

    while True:
        # Check overall timeout for acquiring any project from the pool
        if time.time() - start_time > MAX_ACQUIRE_WAIT_TIME_SECONDS:
            logging.error(f"api_handler: Failed to acquire project within {MAX_ACQUIRE_WAIT_TIME_SECONDS} seconds. Pool exhausted or highly contended.")
            raise Exception("Server is facing too much load, please try again later or use your own Gemini API key.")

        project_token = None
        try:
            # Try to get a project without waiting, to cycle through potentially available ones
            project_token = available_projects_queue.get_nowait()
        except asyncio.QueueEmpty:
            # If queue is empty, wait briefly, then retry the loop and the timeout check
            logging.debug(f"acquire_project: Queue empty, waiting for {0.1}s to re-check pool status.")
            await asyncio.sleep(0.1)
            continue # Go back to the top of the while loop, re-check overall timeout and queue

        if project_token["semaphore"]._value==0:
            raise Exception("Server is facing too much load, please try again later or use your own Gemini API key.")

        project_id_log = project_token['id']

        # Ensure we use timezone-aware UTC for comparisons
        now_utc = datetime.datetime.now(pytz.utc)

        # NEW: 1. Daily Reset Check
        now_utc_date = now_utc.date()
        if project_token["last_reset_date"] < now_utc_date:
            logging.info(f"api_handler: Daily reset for project {project_token['id']}. Resetting requests_today to {MAX_DAILY_REQUESTS_PER_KEY} and last_reset_date to {now_utc_date}.")
            project_token["requests_today"] = MAX_DAILY_REQUESTS_PER_KEY
            project_token["last_reset_date"] = now_utc_date
            
        # 2. Prune old timestamps (older than 60 seconds)
        project_token["session_start_timestamps"] = [
            ts for ts in project_token["session_start_timestamps"]
            if (now_utc - ts).total_seconds() < 60
        ]
        current_sessions_in_rate_window = len(project_token["session_start_timestamps"])

        # NEW: 3. Check combined conditions: New session rate limit AND daily limit
        if current_sessions_in_rate_window < project_token["rate_limit_new_sessions_per_minute"] and \
           project_token["requests_today"] > 2:
            # Rate limit and daily limit passed. Now, try to acquire the concurrency semaphore.
            try:
                await project_token["semaphore"].acquire()

                # If we reach here, semaphore acquired! This key can handle another concurrent user.
                # Add current time to mark the start of this new session for rate-limiting.
                project_token["session_start_timestamps"].append(now_utc)
                project_token["requests_today"] -= 1 # NEW: Decrement daily request count
                logging.info(f"api_handler: Acquired project {project_token['id']}. Concurrency slot taken. New session started. Sessions in last 60s: {len(project_token['session_start_timestamps'])}. Requests remaining today: {project_token['requests_today']}.")
                return project_token # Successfully acquired!
            except Exception as e: # Should not happen with standard semaphore acquire unless cancelled
                logging.error(f"api_handler: Unexpected error acquiring semaphore for {project_id_log}: {e}", exc_info=True)
                # If semaphore acquisition fails unexpectedly, put token back and try another.
                await available_projects_queue.put(project_token)
                checked_in_cycle += 1 # Count this check as an unsuccessful one
                # If we've checked all projects and still no luck, pause briefly
                if checked_in_cycle >= num_projects:
                    logging.debug(f"api_handler: Cycled through projects after semaphore error; pausing briefly.")
                    await asyncio.sleep(0.2)
                    checked_in_cycle = 0
                continue # Try next iteration
        else:
            # This project_token is currently unsuitable (either rate-limited or daily-limited).
            # Put it back to the end of the queue.
            # logging.debug(f"api_handler: Project {project_id_log} is unsuitable (rate/daily limit). Returning to queue.")
            await available_projects_queue.put(project_token)
            checked_in_cycle += 1 # Count this check

            # If we've cycled through all project tokens and none were suitable,
            # wait briefly before trying another cycle to prevent tight looping.
            if checked_in_cycle >= num_projects:
                logging.debug(f"api_handler: Cycled through all projects ({checked_in_cycle}/{num_projects}); all appeared busy, rate-limited, or daily-limited. Brief pause.")
                await asyncio.sleep(0.2) # Short pause to yield control
                checked_in_cycle = 0 # Reset cycle count


async def release_project(project_token):
    """Releases a project's concurrency semaphore slot and returns its token to the pool."""
    if project_token:
        try:
            project_token["semaphore"].release()
            logging.info(f"api_handler: Project {project_token['id']} concurrency slot released. Requests remaining today: {project_token['requests_today']}.")
        except Exception as e:
            logging.error(f"api_handler: Error releasing semaphore for {project_token.get('id', 'UNKNOWN')}: {e}", exc_info=True)
        finally:
            # Always try to put the token back in the queue, even if semaphore release failed (though it shouldn't)
            await available_projects_queue.put(project_token)
            # logging.debug(f"api_handler: Project {project_token['id']} token returned to queue. Queue size: {available_projects_queue.qsize()}")
    else:
        logging.warning("api_handler: Attempted to release a null project_token.")


__all__ = [
    "initialize_project_pool",
    "acquire_project", # Exporting for app.py
    "release_project", # Exporting for app.py
]