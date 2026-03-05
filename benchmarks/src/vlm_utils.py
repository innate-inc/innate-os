#!/usr/bin/env python3
import os
import time
import json
import mimetypes
from dotenv import load_dotenv


def _init_gemini_backend(api_key):
    """
    Initialize Gemini SDK backend.

    Prefers the latest google-genai client API and falls back to the legacy
    google-generativeai API only when needed.
    """
    new_api_error = None
    try:
        from google import genai as google_genai

        return {"mode": "new", "client": google_genai.Client(api_key=api_key), "genai": google_genai}
    except Exception as e:
        new_api_error = e

    legacy_api_error = None
    try:
        import google.generativeai as legacy_genai

        legacy_genai.configure(api_key=api_key)
        return {"mode": "legacy", "genai": legacy_genai}
    except Exception as e:
        legacy_api_error = e

    raise RuntimeError(
        "Failed to initialize Gemini SDK. "
        f"google-genai error: {new_api_error}; "
        f"google-generativeai error: {legacy_api_error}"
    )


def _extract_response_text(response):
    """Extract text from Gemini response across SDK versions."""
    text = getattr(response, "text", None)
    if text:
        return text

    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            first = candidates[0]
            content = getattr(first, "content", None)
            parts = getattr(content, "parts", None) or []
            collected = []
            for part in parts:
                part_text = getattr(part, "text", None)
                if part_text:
                    collected.append(part_text)
            if collected:
                return "\n".join(collected)
    except Exception:
        pass

    return str(response)


def _generate_content(backend, model_name, prompt, frame_paths):
    """Generate content with images using whichever Gemini backend is available."""
    images = []
    for frame_path in frame_paths:
        image_data = encode_image(frame_path)
        if image_data:
            images.append(image_data)

    if backend["mode"] == "new":
        genai_module = backend["genai"]
        contents = [prompt]
        for image in images:
            contents.append(
                genai_module.types.Part.from_bytes(
                    data=image["data"], mime_type=image["mime_type"]
                )
            )
        response = backend["client"].models.generate_content(
            model=model_name, contents=contents
        )
        return _extract_response_text(response)

    model = backend["genai"].GenerativeModel(model_name)
    content = [prompt]
    content.extend(images)
    response = model.generate_content(content)
    return _extract_response_text(response)


def encode_image(image_path):
    """
    Load an image for Gemini API.

    Args:
        image_path (str): Path to the image file

    Returns:
        dict: Image data for Gemini API
    """
    try:
        with open(image_path, "rb") as image_file:
            image_data = image_file.read()
            mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            return {"mime_type": mime_type, "data": image_data}
    except Exception as e:
        print(f"Error loading image {image_path}: {e}")
        return None


def get_representative_frames(first_person_dir, chase_dir):
    """
    Select representative frames from the benchmark for VLM evaluation.

    Args:
        first_person_dir (Path): Directory containing first-person view frames
        chase_dir (Path): Directory containing chase view frames

    Returns:
        list: Paths to selected image frames
    """
    first_person_frames = sorted(list(first_person_dir.glob("*.jpg")))
    chase_frames = sorted(list(chase_dir.glob("*.jpg")))

    if not first_person_frames and not chase_frames:
        return []
    # Select frames at regular intervals
    interval = 30

    # Combine frames from both cameras, alternating
    selected_frames = []
    max_frames = min(len(first_person_frames), len(chase_frames))

    for i in range(0, max_frames, interval):
        if i < len(first_person_frames):
            selected_frames.append(str(first_person_frames[i]))
        if i < len(chase_frames):
            selected_frames.append(str(chase_frames[i]))

    return selected_frames[:20]  # Limit to 20 frames to avoid token limits


def evaluate_periodic_with_vlm(
    success_criterion,
    early_stop_criterion,
    frame_paths,
    chat_log_with_coordinates,
    deterministic_check_status,
    metrics=None,
):
    """
    Evaluate both success and early stop criteria using Gemini 2.5 Flash with 3-state response system.

    Args:
        success_criterion (str): The success criterion to evaluate
        early_stop_criterion (str): The early stop criterion to evaluate
        frame_paths (list): Paths to image frames
        chat_log_with_coordinates (list): Chat log including robot coordinates
        deterministic_check_status (dict): Status of deterministic checks (passed/failed)
        metrics (dict): Benchmark metrics with timestamps

    Returns:
        dict: Result with 'action' ("continue", "success", or "stop") and 'reason' fields
    """
    # Load the API key from .env file
    load_dotenv("benchmarks/.env")
    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise ValueError("No GOOGLE_API_KEY provided in benchmarks/.env")

    try:
        backend = _init_gemini_backend(api_key)

        # Format the chat log with coordinates
        chat_log_text = ""
        if chat_log_with_coordinates:
            chat_log_text = (
                "Chat Log with Robot Coordinates (time_since_start in seconds):\n"
            )
            for msg in chat_log_with_coordinates:
                sender = msg.get("sender", "unknown")
                text = msg.get("text", "")
                time_since_start = msg.get("time_since_start", "unknown")
                coordinates = msg.get("coordinates", "unknown")
                chat_log_text += (
                    f"[{time_since_start}s] {sender} @{coordinates}: {text}\n"
                )

        # Format deterministic check status
        deterministic_status_text = "Deterministic Check Status:\n"
        for check_id, status in deterministic_check_status.items():
            deterministic_status_text += (
                f"- {check_id}: {'PASSED' if status else 'NOT PASSED'}\n"
            )

        # Calculate current time since benchmark start
        current_time = time.time()
        if metrics and "start_timestamp" in metrics:
            time_since_start = current_time - metrics["start_timestamp"]
            time_info = f"Current benchmark running time: {round(time_since_start, 2)} seconds\n\n"
        else:
            time_info = "Benchmark running time: unknown\n\n"

        # Build the prompt
        prompt = f"""You are an AI evaluator for robot benchmarks. You will analyze robot behavior and determine if a benchmark should continue, succeed, or stop every 30 seconds.

You will receive:
1. A SUCCESS CRITERION - what the robot needs to achieve to succeed
2. An EARLY STOP CRITERION - conditions that should end the benchmark early
3. Robot camera feeds showing the robot's perspective and actions
4. Chat log with timestamps and robot coordinates
5. Status of deterministic checks (whether specific conditions like location or primitive calls have been met)

Your task is to return one of three actions (NOTHING ELSE):
- "continue": The task is not yet finished and should continue running
- "success": The deterministic checks have passed AND the success criterion is **FULLY** met
- "stop": The early stop criterion has been met (task failed, robot stuck, etc.)

IMPORTANT GUIDELINES:
1. Only return "success" if BOTH the deterministic checks are validated AND the success criterion is completely satisfied
2. Return "stop" if the early stop criterion is met (robot is stuck, task is impossible, etc.). BUT ONlY RETURN STOP EARLY IN A LAST CASE SCENARIO, DO NOT STOP TOO EARLY, LET THE ROBOT RUN FOR AS LONG AS POSSIBLE
3. Return "continue" in all other cases - when the task is still in progress
4. Be conservative - err on the side of "continue" rather than premature success/stop decisions
5. Pay attention to robot coordinates and movement patterns to detect if the robot is stuck or making progress

Evaluate the current state of this robot benchmark:

SUCCESS CRITERION:
{success_criterion}

EARLY STOP CRITERION:
{early_stop_criterion}

{deterministic_status_text}

{time_info}{chat_log_text}

Based on the above information and the provided camera feeds, determine the appropriate action.

Please respond with a JSON object containing:
- "reflection": Detailed analysis of the current state
- "action": One of "continue", "success", or "stop"
- "reason": Detailed explanation for the chosen action"""

        response_text = _generate_content(
            backend=backend,
            model_name="gemini-2.5-pro",
            prompt=prompt,
            frame_paths=frame_paths,
        )

        # Parse the JSON response
        try:
            # First try to parse as direct JSON
            result = json.loads(response_text)
            return result
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            text = response_text
            if "```json" in text:
                # Extract JSON from markdown code blocks
                start = text.find("```json") + 7
                end = text.find("```", start)
                if end != -1:
                    json_text = text[start:end].strip()
                    try:
                        result = json.loads(json_text)
                        return result
                    except json.JSONDecodeError:
                        pass

            # If JSON parsing fails, try to extract action from text
            text_lower = text.lower()
            if "success" in text_lower and "deterministic" in text_lower:
                action = "success"
            elif "stop" in text_lower and (
                "stuck" in text_lower or "impossible" in text_lower
            ):
                action = "stop"
            else:
                action = "continue"

            return {
                "action": action,
                "reason": response_text,
                "reflection": "Response parsing failed, extracted action from text",
            }

    except Exception as e:
        print(f"Error in Gemini VLM evaluation: {e}")
        return {"action": "continue", "reason": f"Error in VLM evaluation: {e}"}


def evaluate_with_vlm(
    criterion,
    frame_paths,
    chat_log=None,
    metrics=None,
    is_stop_check=False,
    print_evaluation=False,
):
    """
    Evaluate a criterion using Gemini 2.5 Flash with the given frames.
    [Legacy function - maintained for backward compatibility]

    Args:
        criterion (str): The criterion to evaluate
        frame_paths (list): Paths to image frames
        chat_log (list): Optional chat log for context
        metrics (dict): Benchmark metrics with timestamps
        is_stop_check (bool): Whether this is a stop criterion check

    Returns:
        dict: Structured result with success/should_stop and reason fields
    """
    # Load the API key from .env file
    load_dotenv("benchmarks/.env")
    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise ValueError("No GOOGLE_API_KEY provided in benchmarks/.env")

    try:
        backend = _init_gemini_backend(api_key)

        # Format the chat log for inclusion in the prompt
        chat_log_text = ""
        if chat_log:
            chat_log_text = "Chat Log (with time_since_start in seconds):\n"
            for msg in chat_log:
                sender = msg.get("sender", "unknown")
                text = msg.get("text", "")
                time_since_start = msg.get("time_since_start", "unknown")
                chat_log_text += f"[{time_since_start}s] {sender}: {text}\n"

        # Calculate current time since benchmark start
        current_time = time.time()
        if metrics and "start_timestamp" in metrics:
            time_since_start = current_time - metrics["start_timestamp"]
            time_info = (
                f"Current benchmark running time: {round(time_since_start, 2)} "
                f"seconds\n\n"
            )
        else:
            time_info = "Benchmark running time: unknown\n\n"

        # Build the prompt
        prompt_type = "stop" if is_stop_check else "success"
        result_key = "should_stop" if is_stop_check else "success"

        prompt = f"""You are an AI evaluator for robot benchmarks. You will receive frames showing a robot performing tasks, a chat log with timestamps, and information about benchmark running time. Evaluate whether the {prompt_type} criterion has been met based on all the information provided.

Based on the provided frames and chat log, evaluate the following {prompt_type} criterion:

{criterion}

IMPORTANT: Only indicate that the criterion has been met if you are completely certain and have clear evidence. When in doubt, err on the side of caution and indicate the criterion has NOT been met. Be conservative in your evaluation.

{time_info}{chat_log_text}

Please respond with a JSON object containing:
- "reflection": Detailed explanation of the criterion evaluation
- "{result_key}": true/false whether the criterion has been met
- "reason": Detailed explanation of the criterion evaluation"""

        response_text = _generate_content(
            backend=backend,
            model_name="gemini-2.5-flash",
            prompt=prompt,
            frame_paths=frame_paths,
        )

        # Parse the JSON response
        try:
            # First try to parse as direct JSON
            result = json.loads(response_text)
            if print_evaluation:
                print(f"VLM evaluation result: {result}\n\n")
            return result
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            text = response_text
            if "```json" in text:
                # Extract JSON from markdown code blocks
                start = text.find("```json") + 7
                end = text.find("```", start)
                if end != -1:
                    json_text = text[start:end].strip()
                    try:
                        result = json.loads(json_text)
                        if print_evaluation:
                            print(f"VLM evaluation result: {result}\n\n")
                        return result
                    except json.JSONDecodeError:
                        pass

            # If JSON parsing fails, try to extract boolean result from text
            text_lower = text.lower()
            if is_stop_check:
                should_stop = "should stop" in text_lower or "stop" in text_lower
                result = {
                    "should_stop": should_stop,
                    "reason": response_text,
                    "reflection": "Response parsing failed, extracted result from text",
                }
            else:
                success = "success" in text_lower and (
                    "achieved" in text_lower or "completed" in text_lower
                )
                result = {
                    "success": success,
                    "reason": response_text,
                    "reflection": "Response parsing failed, extracted result from text",
                }

            if print_evaluation:
                print(f"VLM evaluation result: {result}\n\n")
            return result

    except Exception as e:
        print(f"Error in Gemini VLM evaluation: {e}")
        if is_stop_check:
            return {"should_stop": False, "reason": f"Error in VLM evaluation: {e}"}
        else:
            return {"success": False, "reason": f"Error in VLM evaluation: {e}"}
