#!/usr/bin/env python3
import os
import requests
from datetime import datetime, timezone
from brain_client.skill_types import Skill, SkillResult


class RetrieveTelegram(Skill):
    """
    Skill for retrieving recent Telegram messages sent to the robot's bot.
    Uses the Telegram Bot API (getUpdates) to fetch incoming messages.
    Tracks update offset so each call only returns new messages.
    """

    def __init__(self, logger):
        self.logger = logger
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self._last_update_id = None

    @property
    def name(self):
        return "retrieve_telegram"

    def guidelines(self):
        return (
            "Use to retrieve recent Telegram messages sent to the robot's bot. "
            "Provide the number of messages to retrieve (default is 5). "
            "Returns sender name, message text, and timestamp. Use this when you "
            "need to check for incoming Telegram communications or commands."
        )

    def _format_timestamp(self, unix_ts: int) -> str:
        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    def _extract_sender_name(self, from_data: dict) -> str:
        first = from_data.get("first_name", "")
        last = from_data.get("last_name", "")
        username = from_data.get("username", "")
        full_name = f"{first} {last}".strip()
        if username:
            full_name += f" (@{username})"
        return full_name or "Unknown"

    def execute(self, count: int = 5):
        """
        Retrieves the most recent messages sent to the Telegram bot.

        Args:
            count (int): Number of recent messages to retrieve (default: 5, max: 20)

        Returns:
            tuple: (result_message, SkillResult)
        """
        count = min(max(1, count), 20)

        if not self.bot_token:
            return (
                "TELEGRAM_BOT_TOKEN not set. See skills/TELEGRAM_SETUP.md",
                SkillResult.FAILURE,
            )

        self.logger.info(
            f"\033[96m[BrainClient] Retrieving last {count} Telegram messages\033[0m"
        )

        try:
            # getUpdates returns pending updates; we request more than needed
            # and slice to count, since not all updates are text messages
            params = {"limit": 100, "allowed_updates": '["message"]'}
            if self._last_update_id is not None:
                params["offset"] = self._last_update_id + 1

            resp = requests.get(
                f"{self.api_base}/getUpdates",
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                error_desc = data.get("description", "Unknown Telegram API error")
                self.logger.error(f"Telegram API error: {error_desc}")
                return f"Telegram API error: {error_desc}", SkillResult.FAILURE

            updates = data.get("result", [])

            # Track highest update_id to acknowledge processed updates
            if updates:
                self._last_update_id = max(u["update_id"] for u in updates)

            # Filter to only message updates with text content (newest first)
            messages = []
            for update in reversed(updates):
                msg = update.get("message") or update.get("channel_post")
                if msg and msg.get("text"):
                    messages.append(msg)
                if len(messages) >= count:
                    break

            if not messages:
                return "No new Telegram messages", SkillResult.SUCCESS

            result_lines = [f"Retrieved {len(messages)} new Telegram message(s):\n"]

            for i, msg in enumerate(messages, 1):
                sender = self._extract_sender_name(msg.get("from", {}))
                text = msg.get("text", "[No text]")
                chat_id = msg.get("chat", {}).get("id", "unknown")
                chat_title = msg.get("chat", {}).get("title", "Direct Message")
                timestamp = self._format_timestamp(msg.get("date", 0))

                # Truncate long messages
                if len(text) > 500:
                    text = text[:500] + "... [truncated]"

                result_lines.append(f"Message {i}:")
                result_lines.append(f"  From: {sender}")
                result_lines.append(f"  Chat: {chat_title}")
                result_lines.append(f"  Chat ID: {chat_id}")
                result_lines.append(f"  Date: {timestamp}")
                result_lines.append(f"  Text: {text}")
                result_lines.append("")

            result_message = "\n".join(result_lines)

            # Send feedback to brain for context
            self._send_feedback(result_message)

            self.logger.info(
                f"\033[92m[BrainClient] Successfully retrieved "
                f"{len(messages)} Telegram messages\033[0m"
            )

            return result_message, SkillResult.SUCCESS

        except requests.exceptions.Timeout:
            return "Telegram API request timed out", SkillResult.FAILURE
        except requests.exceptions.ConnectionError:
            return "Could not connect to Telegram API", SkillResult.FAILURE
        except Exception as e:
            error_msg = f"Failed to retrieve Telegram messages: {str(e)}"
            self.logger.error(error_msg)
            return error_msg, SkillResult.FAILURE

    def cancel(self):
        self.logger.info(
            "\033[91m[BrainClient] Telegram retrieval cannot be canceled "
            "once started\033[0m"
        )
        return "Telegram retrieval is an atomic operation that cannot be canceled"
