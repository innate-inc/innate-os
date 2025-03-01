import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time
from brain_client.primitives.types import Primitive


class SendEmail(Primitive):
    """
    Primitive for sending emails for emergency notifications.
    This is a simplified version that logs the email content rather than actually sending.
    In a production environment, you would configure proper SMTP settings.
    """

    def __init__(self, logger):
        self.logger = logger
        self.default_recipient = "axel@innate.bot"

    @property
    def name(self):
        return "send_email"

    def guidelines(self):
        return (
            "Use to send an emergency email notification. Provide a subject and message. "
            "This should be used when a potential emergency is detected and assistance might be required."
        )

    def execute(self, subject: str, message: str, recipient: str = None):
        """
        Simulates sending an email to the specified recipient (or default if none provided).

        Args:
            subject (str): Email subject line
            message (str): Email body content
            recipient (str, optional): Email recipient. Defaults to axel@innate.bot if not specified.

        Returns:
            tuple: (confirmation message, success boolean)
        """
        if recipient is None:
            recipient = self.default_recipient

        self.logger.info(
            f"\033[96m[BrainClient] Sending emergency email notification\033[0m\n"
            f"To: {recipient}\n"
            f"Subject: {subject}\n"
            f"Message: {message}"
        )

        # In a real implementation, you would connect to an SMTP server here
        # For demonstration, we'll just simulate it with a delay
        time.sleep(1)

        # Log success message
        self.logger.info(
            f"\033[92m[BrainClient] Emergency email notification sent to {recipient}\033[0m"
        )

        return f"Email sent to {recipient}", True
