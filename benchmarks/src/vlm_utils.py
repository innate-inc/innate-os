#!/usr/bin/env python3
import os
import time
from openai import OpenAI
from dotenv import load_dotenv


def encode_image(image_path):
    """
    Encode an image to base64 for VLM API.

    Args:
        image_path (str): Path to the image file

    Returns:
        str: Base64 encoded image
    """
    try:
        import base64

        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
            return encoded_string
    except Exception as e:
        print(f"Error encoding image {image_path}: {e}")
        return None


def get_representative_frames(first_person_dir, chase_dir, comprehensive=False):
    """
    Select representative frames from the benchmark for VLM evaluation.

    Args:
        first_person_dir (Path): Directory containing first-person view frames
        chase_dir (Path): Directory containing chase view frames
        comprehensive (bool): If True, includes more frames for a more thorough evaluation

    Returns:
        list: Paths to selected image frames
    """
    # Select frames at regular intervals
    first_person_frames = sorted(list(first_person_dir.glob("*.jpg")))
    chase_frames = sorted(list(chase_dir.glob("*.jpg")))

    if not first_person_frames and not chase_frames:
        return []

    # Select frames at regular intervals
    interval = 5 if comprehensive else 20  # More frames for comprehensive analysis

    # Combine frames from both cameras, alternating
    selected_frames = []
    max_frames = min(len(first_person_frames), len(chase_frames))

    for i in range(0, max_frames, interval):
        if i < len(first_person_frames):
            selected_frames.append(str(first_person_frames[i]))
        if i < len(chase_frames):
            selected_frames.append(str(chase_frames[i]))

    return selected_frames[:20]  # Limit to 20 frames to avoid token limits


def evaluate_with_vlm(
    criterion, frame_paths, chat_log=None, metrics=None, is_stop_check=False
):
    """
    Evaluate a criterion using a VLM model with the given frames.

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
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("No VLM API key provided in benchmarks/.env")

    try:
        # Create OpenAI client
        client = OpenAI(api_key=api_key)

        # Create schema for structured output
        result_key = "should_stop" if is_stop_check else "success"
        schema = {
            "type": "object",
            "properties": {
                "reflection": {
                    "type": "string",
                    "description": "Detailed explanation of why the criterion was met or not met",
                },
                result_key: {
                    "type": "boolean",
                    "description": "Whether the criterion has been met",
                },
                "reason": {
                    "type": "string",
                    "description": "Detailed explanation of why the criterion was met or not met",
                },
            },
            "required": ["reflection", result_key, "reason"],
            "additionalProperties": False,
        }

        # Build messages with images - proper format for OpenAI API
        messages = []

        # Build system message
        prompt_type = "stop" if is_stop_check else "success"
        system_message = {
            "role": "system",
            "content": (
                f"You are an AI evaluator for robot benchmarks. You will receive frames "
                f"showing a robot performing tasks, a chat log with timestamps, and "
                f"information about how long the benchmark has been running. "
                f"Evaluate whether the {prompt_type} criterion has been met based on "
                f"all the information provided."
            ),
        }
        messages.append(system_message)

        # Format the chat log for inclusion in the prompt
        chat_log_text = ""
        if chat_log:
            chat_log_text = "Chat Log (with time_since_start in seconds):\n"
            for msg in chat_log:
                sender = msg.get("sender", "unknown")
                text = msg.get("text", "")
                time_since_start = msg.get("time_since_start", "unknown")
                chat_log_text += f"[{time_since_start}s] {sender}: {text}\n"

        # Build user message with text, chat log, and images
        user_message_content = []

        # Calculate current time since benchmark start
        current_time = time.time()
        if metrics and "start_timestamp" in metrics:
            time_since_start = current_time - metrics["start_timestamp"]
            time_info = f"Current benchmark running time: {round(time_since_start, 2)} seconds\n\n"
        else:
            time_info = "Benchmark running time: unknown\n\n"

        # First add the text part with chat log
        eval_text = (
            f"Based on the provided frames and chat log, evaluate the "
            f"following {prompt_type} criterion:\n\n{criterion}\n\n"
            f"{time_info}{chat_log_text}"
        )
        user_message_content.append(
            {
                "type": "input_text",
                "text": eval_text,
            }
        )

        # Then add each image
        for frame_path in frame_paths:
            encoded_image = encode_image(frame_path)
            if encoded_image:
                user_message_content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded_image}",
                    }
                )

        # Add user message with all content
        messages.append({"role": "user", "content": user_message_content})

        # Call the OpenAI API with the properly formatted messages
        response = client.responses.create(
            model="gpt-4o-2024-08-06",
            input=messages,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "criterion_evaluation",
                    "schema": schema,
                    "strict": True,
                }
            },
        )

        # Parse the structured output
        result = response.output_text

        # If response is a string (JSON), parse it
        if isinstance(result, str):
            import json

            result = json.loads(result)

        print(f"VLM evaluation result: {result}")
        return result

    except Exception as e:
        print(f"Error in VLM evaluation: {e}")
        if is_stop_check:
            return {"should_stop": False, "reason": f"Error in VLM evaluation: {e}"}
        else:
            return {"success": False, "reason": f"Error in VLM evaluation: {e}"}
