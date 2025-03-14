#!/usr/bin/env python3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
        # Email server configuration
        self.smtp_server = "smtp.gmail.com"  # Example using Gmail
        self.smtp_port = 587
        self.sender_email = "axel@innate.bot"  # Replace with robot's email
        self.password = "ncbtviozpktktsdm"  # Use app password for Gmail

    @property
    def name(self):
        return "send_email"

    def guidelines(self):
        return (
            "Use to send an emergency email notification. Provide a subject and message. "
            "This should be used when a potential emergency is detected and assistance "
            "might be required."
        )

    def execute(self, subject: str, message: str, recipient: str = None):
        """
        Sends an email to the specified recipient (or default if none provided).

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

        try:
            # Create message
            msg = MIMEMultipart()
            msg["From"] = self.sender_email
            msg["To"] = recipient
            msg["Subject"] = subject
            msg.attach(MIMEText(message, "plain"))

            # Connect to server and send
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.sender_email, self.password)
            server.send_message(msg)
            server.quit()

            # Log success message
            self.logger.info(
                f"\033[92m[BrainClient] Emergency email notification sent to {recipient}\033[0m"
            )
            return f"Email sent to {recipient}", True

        except Exception as e:
            self.logger.error(f"Failed to send email: {str(e)}")
            return f"Failed to send email: {str(e)}", False

    def cancel(self):
        """
        Cancel the email sending operation.

        Since email sending is typically a quick operation that completes almost instantly,
        this method doesn't do much. It's implemented to satisfy the Primitive interface.

        Returns:
            str: A message describing the cancellation result.
        """
        self.logger.info(
            "\033[91m[BrainClient] Email sending operation cannot be canceled once started\033[0m"
        )
        return (
            "Email sending is an atomic operation that cannot be canceled once started"
        )
