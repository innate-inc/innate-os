#!/usr/bin/env python3
import os
import requests
from brain_client.skill_types import Skill, SkillResult


class SendTelegram(Skill):
    """
    Skill for sending Telegram messages via the Bot API.
    Requires a chat_id (obtained from retrieve_telegram output).
    """

    def __init__(self, logger):
        self.logger = logger
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    @property
    def name(self):
        return "send_telegram"

    def guidelines(self):
        return (
            "Use to send a Telegram message to a specific chat. "
            "Requires chat_id (from retrieve_telegram output) and the message text. "
            "Use this to reply to Telegram users or confirm actions were completed."
        )

    def execute(self, chat_id: int, message: str):
        """
        Sends a text message to a Telegram chat.

        Args:
            chat_id (int): The chat ID to send the message to (from retrieve_telegram output)
            message (str): The text message to send

        Returns:
            tuple: (result_message, SkillResult)
        """
        if not self.bot_token:
            return (
                "TELEGRAM_BOT_TOKEN not set. See skills/TELEGRAM_SETUP.md",
                SkillResult.FAILURE,
            )

        if not message or not message.strip():
            return "Message cannot be empty", SkillResult.FAILURE

        self.logger.info(
            f"\033[96m[BrainClient] Sending Telegram message to chat {chat_id}\033[0m"
        )

        try:
            resp = requests.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                error_desc = data.get("description", "Unknown Telegram API error")
                self.logger.error(f"Telegram API error: {error_desc}")
                return f"Telegram API error: {error_desc}", SkillResult.FAILURE

            self.logger.info(
                f"\033[92m[BrainClient] Telegram message sent to chat {chat_id}\033[0m"
            )
            return f"Message sent to chat {chat_id}", SkillResult.SUCCESS

        except requests.exceptions.Timeout:
            return "Telegram API request timed out", SkillResult.FAILURE
        except requests.exceptions.ConnectionError:
            return "Could not connect to Telegram API", SkillResult.FAILURE
        except Exception as e:
            error_msg = f"Failed to send Telegram message: {str(e)}"
            self.logger.error(error_msg)
            return error_msg, SkillResult.FAILURE

    def cancel(self):
        self.logger.info(
            "\033[91m[BrainClient] Telegram send cannot be canceled once started\033[0m"
        )
        return "Telegram send is an atomic operation that cannot be canceled"
