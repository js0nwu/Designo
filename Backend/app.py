# app.py
import base64
import asyncio
import json
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.genai import types as google_genai_types
import config
import adk_utils
import agents
import api_handler # We will use acquire_project and release_project from here
from tools import replace_svg_image_links_with_base64, replace_material_icons_in_svg, extract_svg_from_text

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app, origins="*")
logging.basicConfig(level=logging.INFO)

# --- Global State (Manual Chat History per user) ---
chat_history = {}
MAX_CHAT_HISTORY = 10

# --- AI GENERATION ENDPOINT (Modified for pooled key handling) ---
@app.route('/generate', methods=['POST'])
async def handle_generate():
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 415

    uid = request.headers.get("X-Designo-User", "local-designo-user")
    logging.info(f"/generate request from local user UID: {uid}")

    run_interaction_method = None
    api_key_for_this_entire_request = None
    project_in_use_for_this_request = None # Holds the dict from api_handler if a pooled key is used
    requests_today = None

    if config.GOOGLE_API_KEY:
        logging.info(f"Using backend GOOGLE_API_KEY for UID {uid}.")
        run_interaction_method = 'server_key'
        api_key_for_this_entire_request = config.GOOGLE_API_KEY
    elif api_handler.PROJECT_POOL:
        logging.info(f"No backend GOOGLE_API_KEY set. Acquiring a pooled API key for UID {uid}.")
        run_interaction_method = 'pooled_key'
    else:
        return jsonify({
            "success": False,
            "error": "No Gemini API key configured. Set GOOGLE_API_KEY or at least one GOOGLE_API_KEY_0 style pooled key."
        }), 500

    user_history = chat_history.get(uid, [])
    data = request.get_json()
    user_prompt_text = data.get('userPrompt')
    context = data.get('context', {})
    frame_data_base64 = data.get('frameDataBase64')
    element_data_base64 = data.get('elementDataBase64')
    i_mode = data.get('mode')

    if not user_prompt_text:
        return jsonify({"success": False, "error": "Missing 'userPrompt'"}), 400

    history_text = ""
    if user_history:
        user_history_summary = [
            f"User: {item.get('user', '')[:100]}{'...' if len(item.get('user', '')) > 100 else ''}\nAI: {item.get('AI', '')[:100]}{'...' if len(item.get('AI', '')) > 100 else ''}"
            for item in user_history[-MAX_CHAT_HISTORY:]
        ]
        if user_history_summary:
            history_text = "Previous Conversation Summary:\n" + "\n---\n".join(user_history_summary) + "\n\n"

    decision_prompt_text = f"{history_text}**User Request**\n{user_prompt_text}"
    if context:
        decision_prompt_text += f"\n**Figma Context**\n{json.dumps(context)}"
    decision_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=decision_prompt_text)])

    final_result = None
    final_type = "unknown"
    agent_used_name_log = "None" # For logging overall flow

    try:
        # --- Acquire pooled key if needed, ONCE for the entire request ---
        if run_interaction_method == 'pooled_key':
            try:
                # acquire_project now handles its own timeout and raises specific error
                project_in_use_for_this_request = await api_handler.acquire_project()
                api_key_for_this_entire_request = project_in_use_for_this_request["api_key"]
                logging.info(f"UID {uid}: Acquired pooled project '{project_in_use_for_this_request['id']}' (key ...{api_key_for_this_entire_request[-4:]}) for this entire request. Requests remaining today for this key: {project_in_use_for_this_request['requests_today']}.")
            except Exception as acquire_err:
                # Catch the specific error message from api_handler.acquire_project
                error_msg_str = str(acquire_err)
                if "Server is facing too much load" in error_msg_str:
                    logging.warning(f"UID {uid}: Pooled project acquisition failed due to load: {error_msg_str}")
                    return jsonify({"success": False, "error": error_msg_str}), 503 # Service Unavailable
                else:
                    logging.error(f"UID {uid}: Failed to acquire a pooled project (unexpected error): {acquire_err}", exc_info=True)
                    return jsonify({"success": False, "error": f"An internal server error occurred while acquiring resources: {acquire_err}"}), 500

        if not api_key_for_this_entire_request: # Should only happen if pooled_key path failed to set it
             logging.error(f"UID {uid}: Logical error - API key for the request was not set.")
             return jsonify({"success": False, "error": "Internal server error: API key not available for processing."}), 500

        # --- 1. Determine Intent (using the single chosen API key) ---
        agent_used_name_log = agents.decision_agent.name
        intent_mode_raw = await adk_utils.run_adk_interaction(
            agents.decision_agent, decision_content, adk_utils.session_service,
            user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
        )

        print("DEBUG:" ,type(intent_mode_raw) ,intent_mode_raw)

        # adk_utils.run_adk_interaction now returns a dict for decision_agent if successful
        if isinstance(intent_mode_raw, str):
            intent_mode_raw = intent_mode_raw.strip().replace("```json","").replace("```","").strip()
            try:
                intent_mode_raw = json.loads(intent_mode_raw)
            except json.JSONDecodeError as e:
                logging.error(f"UID {uid}: Failed to decode JSON from agent response: {e}")

        if isinstance(intent_mode_raw, dict) and "mode" in intent_mode_raw and "modified_prompt" in intent_mode_raw:
            intent_mode = intent_mode_raw["mode"].strip().lower()
            user_prompt_for_next_agent = intent_mode_raw["modified_prompt"].strip()
            logging.info(f"UID {uid}: Determined Intent: '{intent_mode}' with refined prompt.")
        else: # Fallback if decision agent failed or returned unexpected format
            error_msg = f"Could not determine intent. Agent Response: {intent_mode_raw}"
            logging.error(f"UID {uid}: {error_msg}")
            # Note: If an AGENT_ERROR or ADK_RUNTIME_ERROR occurs, the key is still held until 'finally'
            return jsonify({"success": False, "error": error_msg}), 200

        if intent_mode not in ['create', 'modify', 'answer']:
            logging.warning(f"UID {uid}: Decision agent returned unexpected mode '{intent_mode}'. Falling back to 'answer'.")
            intent_mode = 'answer'
            user_prompt_for_next_agent = user_prompt_text # Use original prompt if mode is invalid

        if intent_mode in ['create', 'modify'] and i_mode != intent_mode:
            logging.warning(f"UID {uid}: Agent intent '{intent_mode}', frontend mode '{i_mode}'. Mismatch.")
            error_message_for_mismatch = ("I detected a creation request, but I need an empty frame selection to create a new design."
                                          if intent_mode == 'create' else
                                          "I detected a modification request, but I need an element selection to proceed.")
            return jsonify({"success": False, "error": error_message_for_mismatch}), 200

        # --- 2. Execute Based on Intent (using the SAME API key) ---
        if intent_mode == 'create':
            final_type = "svg"
            agent_used_name_log = f"{agents.refine_agent.name} -> {agents.create_agent.name}"
            logging.info(f"UID {uid}: --- Initiating Create Flow (using key ...{api_key_for_this_entire_request[-4:]}) ---")
            
            # Use the refined prompt from decision agent
            refine_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=user_prompt_for_next_agent)])
            
            refined_prompt_md = await adk_utils.run_adk_interaction(
                agents.refine_agent, refine_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            refined_prompt_md = str(refined_prompt_md)
            if not refined_prompt_md or refined_prompt_md.startswith("AGENT_ERROR:") or refined_prompt_md.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Refine Agent failed or returned error for create: {refined_prompt_md}")
            
            refined_prompt_clean = refined_prompt_md.strip()
            if not refined_prompt_clean:
                 logging.warning(f"UID {uid}: Refine agent returned empty brief for create, falling back to original refined prompt from decision agent.")
                 refined_prompt_clean = user_prompt_for_next_agent
            
            # ======== For Debugging Puposes(To be removed in prod) ======
            try:
                with open("output_create_rf.md","w", errors="replace", encoding="utf8") as f:
                    f.write(refined_prompt_clean)
            except:
                print("failed to write refined prompt")
            # ============================================================
            create_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=refined_prompt_clean)])
            response = await adk_utils.run_adk_interaction(
                agents.create_agent, create_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            initial_svg = extract_svg_from_text(response)
            if not initial_svg or initial_svg.startswith("AGENT_ERROR:") or initial_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Create Agent failed or returned error: {initial_svg}")
            
            cleaned_svg = adk_utils.is_valid_svg(initial_svg)
            if not cleaned_svg:
                 raise ValueError(f"Create Agent response is not valid SVG. Snippet: {str(initial_svg)[:200]}...")
            final_result = cleaned_svg
            logging.info(f"UID {uid}: Create flow successful.")

        elif intent_mode == 'modify':
            final_type = "svg"
            agent_used_name_log = f"{agents.refine_agent.name} -> {agents.modify_agent.name}"
            logging.info(f"UID {uid}: --- Initiating Modify Flow (using key ...{api_key_for_this_entire_request[-4:]}) ---")
            if not frame_data_base64 or not element_data_base64 or not context.get('elementInfo'):
                 raise ValueError("Missing 'frameDataBase64', 'elementDataBase64', or 'elementInfo' for modify mode")

            # Use the refined prompt from decision agent
            refine_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=user_prompt_for_next_agent)])
            refined_prompt_md = await adk_utils.run_adk_interaction(
                agents.refine_agent, refine_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            refined_prompt_md = str(refined_prompt_md)
            if not refined_prompt_md or refined_prompt_md.startswith("AGENT_ERROR:") or refined_prompt_md.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Refine Agent failed or returned error for modify: {refined_prompt_md}")

            refined_prompt_clean = refined_prompt_md.strip()
            if not refined_prompt_clean:
                 logging.warning(f"UID {uid}: Refine agent returned empty brief for modify, falling back to original refined prompt from decision agent.")
                 refined_prompt_clean = user_prompt_for_next_agent

            modify_agent_prompt_text = f"""**Modification Brief**\n{refined_prompt_clean}\n\n**Original User Prompt for context:**\n{user_prompt_text}\n\n**Figma Context:**\nFrame Name: {context.get('frameName', 'N/A')}\nElement Info: {context.get('elementInfo','N/A')}"""
            message_parts = [google_genai_types.Part(text=modify_agent_prompt_text)]
            try:
                frame_bytes = base64.b64decode(frame_data_base64)
                element_bytes = base64.b64decode(element_data_base64)
                message_parts.append(google_genai_types.Part(inline_data=google_genai_types.Blob(mime_type="image/png", data=frame_bytes)))
                message_parts.append(google_genai_types.Part(inline_data=google_genai_types.Blob(mime_type="image/png", data=element_bytes)))
            except Exception as e:
                raise ValueError(f"Invalid image data received for modify mode: {e}")
            
            modify_content = google_genai_types.Content(role='user', parts=message_parts)
            modified_svg = await adk_utils.run_adk_interaction(
                agents.modify_agent, modify_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            modified_svg = str(modified_svg)
            if not modified_svg or modified_svg.startswith("AGENT_ERROR:") or modified_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Modify Agent failed or returned error: {modified_svg}")
            
            cleaned_svg = adk_utils.is_valid_svg(modified_svg)
            if not cleaned_svg:
                 raise ValueError(f"Modify Agent response is not valid SVG. Snippet: {str(modified_svg)[:200]}...")
            final_result = cleaned_svg
            logging.info(f"UID {uid}: Modify flow successful.")

        elif intent_mode == 'answer':
            final_type = "answer"
            agent_used_name_log = agents.answer_agent.name
            logging.info(f"UID {uid}: --- Running Answer Agent (using key ...{api_key_for_this_entire_request[-4:]}) ---")
            
            # Use the refined prompt from decision agent
            answer_prompt_text = f"{history_text}**User Query**\n{user_prompt_for_next_agent}"
            answer_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=answer_prompt_text)])
            
            answer_text = await adk_utils.run_adk_interaction(
                agents.answer_agent, answer_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            answer_text = str(answer_text)
            if not answer_text :
                 logging.info(f"UID {uid}: Answer agent returned empty response. Providing default.")
                 final_result = "I could not find specific information regarding your query at the moment."
            elif answer_text.startswith("AGENT_ERROR:") or answer_text.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Answer Agent failed or returned error: {answer_text}")
            else:
                final_result = answer_text
            logging.info(f"UID {uid}: Answer flow successful.")
        
        else:
            logging.error(f"UID {uid}: Internal error - Unhandled intent '{intent_mode}'.")
            return jsonify({"success": False, "error": f"Internal error: Unhandled intent type '{intent_mode}'."}), 500

    except ValueError as ve:
        error_message = str(ve)
        logging.error(f"UID {uid}: ValueError during '{agent_used_name_log}' execution: {error_message}", exc_info=False) # Set exc_info based on verbosity preference
        return jsonify({"success": False, "error": error_message}), 200
    except Exception as e:
        error_message = f"An unexpected error occurred during '{agent_used_name_log}' execution."
        logging.error(f"UID {uid}: {error_message} Details: {e}", exc_info=True)
        return jsonify({"success": False, "error": "An internal server error occurred."}), 500
    finally:
        # --- Release the pooled project IF it was acquired for this request ---
        if project_in_use_for_this_request: # This implies run_interaction_method was 'pooled_key'
            await api_handler.release_project(project_in_use_for_this_request)
            logging.info(f"UID {uid}: Released pooled project '{project_in_use_for_this_request['id']}' after request completion/failure.")

    # --- Format and Return Success Response ---
    if final_result is None and not (isinstance(intent_mode_raw, str) and (intent_mode_raw.startswith("AGENT_ERROR:") or intent_mode_raw.startswith("ADK_RUNTIME_ERROR:"))): # Check if error already handled
         logging.error(f"UID {uid}: Execution completed for '{agent_used_name_log}' but final_result is unexpectedly None for mode '{intent_mode}'.")
         return jsonify({"success": False, "error": "Agent processing failed to produce a result."}), 500

    user_history = chat_history.get(uid, [])
    user_history.append({'uid': uid, 'user': user_prompt_text, 'AI': final_result if isinstance(final_result, str) else "SVG content generated."})
    chat_history[uid] = user_history[-MAX_CHAT_HISTORY:]
    
    response_payload = {
        "success": True,
        "mode": final_type,
        "requests_today": requests_today,
        "using_server_key": run_interaction_method == 'server_key'
    }
    # NEW: Add information about the pooled key's remaining requests
    if run_interaction_method == 'pooled_key' and project_in_use_for_this_request:
        response_payload["pooled_key_requests_remaining_today"] = project_in_use_for_this_request['requests_today']

    if final_type == "svg":
        svg_withbase64_images = replace_svg_image_links_with_base64(final_result)
        svg_with_vector_icons = replace_material_icons_in_svg(svg_withbase64_images)
        response_payload["svg"] = svg_with_vector_icons
        # Check if refined_prompt_clean is available and valid before using for frameName
        frame_name_from_refined_prompt = ""
        if refined_prompt_clean and isinstance(refined_prompt_clean, str):
            first_line = refined_prompt_clean.splitlines()[0]
            # Strip markdown and common prefixes from the first line
            frame_name_from_refined_prompt = first_line.replace('#','').replace('*','').replace(' Brief','').strip()
        
        response_payload["frameName"] = frame_name_from_refined_prompt if frame_name_from_refined_prompt else "Generated Design"


        # ======== For Debugging Puposes(To be removed in prod) ======
        try:
            with open("output.svg", 'w', encoding='utf-8', errors='replace') as f:
                f.write(svg_with_vector_icons)
        except:
            print("write to output.svg failed")
        # ============================================================

    elif final_type == "answer":
        response_payload["answer"] = final_result
    
    if run_interaction_method == 'server_key':
        key_info = "(Server GOOGLE_API_KEY)"
    else:
        key_info = f"(Pooled project: {project_in_use_for_this_request['id'] if project_in_use_for_this_request else 'N/A'})"
    logging.info(f"UID {uid}: Request completed successfully (type: {final_type}) {key_info}.")
    return jsonify(response_payload), 200

# --- Run the App (Unchanged) ---
if __name__ == '__main__':
    logging.info(f"Running Flask app with AGENT_MODEL='{config.AGENT_MODEL}'")
    logging.info("Local mode enabled; configure GOOGLE_API_KEY or GOOGLE_API_KEY_0 style pooled keys.")

    from hypercorn.config import Config as HypercornConfig
    import hypercorn.asyncio

    async def serve_app():
        await api_handler.initialize_project_pool() # Initialize pool
        
        hypercorn_config_obj = HypercornConfig()
        hypercorn_config_obj.bind = ["0.0.0.0:5001"]
        # hypercorn_config_obj.accesslog = "-"

        await hypercorn.asyncio.serve(app, hypercorn_config_obj)

    try:
        asyncio.run(serve_app())
    except KeyboardInterrupt:
        logging.info("\nServer stopped by user.")
    except Exception as e:
         logging.error(f"Server failed to start or run: {e}", exc_info=True)
