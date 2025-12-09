#!/usr/bin/env python3
import smtplib
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from brain_client.primitive_types import Primitive, PrimitiveResult, RobotStateType


class SendPictureViaEmail(Primitive):
    """
    Primitive for sending an email with an attached picture.
    """

    def __init__(self, logger):
        self.logger = logger
        self.default_recipient = "axel@innate.bot"
        # Email server configuration (same as SendEmail)
        self.smtp_server = "smtp.gmail.com"
        self.smtp_port = 587
        self.sender_email = "axel@innate.bot"
        self.password = ""  # Use app password for Gmail
        self.last_main_camera_image_b64 = None

    @property
    def name(self):
        return "send_picture_via_email"

    def get_required_robot_states(self) -> list[RobotStateType]:
        """Declare that this primitive needs the last image."""
        return [RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64]

    def update_robot_state(self, **kwargs):
        """Store the last image received from the robot state."""
        self.last_main_camera_image_b64 = kwargs.get(
            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value
        )
        if self.last_main_camera_image_b64:
            self.logger.info("[SendPictureViaEmail] Received image for email.")
        else:
            self.logger.warn("[SendPictureViaEmail] Did not receive image for email.")

    def guidelines(self):
        return (
            "Use to send an email with the latest view from the robot eyes. "
            "Provide a subject and a message body. "
            "The view will be automatically attached."
        )

    def execute(self, subject: str, message: str, recipient: str = None):
        """
        Sends an email with the last captured image attached.

        Args:
            subject (str): Email subject line.
            message (str): Email body content.
            recipient (str, optional): Email recipient. Defaults to default_recipient.

        Returns:
            tuple: (result_message, result_status)
        """
        if not recipient:  # Checks for None or empty string
            recipient = self.default_recipient

        if not self.last_main_camera_image_b64:
            self.logger.error("[SendPictureViaEmail] No image available to send.")
            return "No image available to send", PrimitiveResult.FAILURE

        self.logger.info(
            f"\\033[96m[BrainClient] Sending email with picture to {recipient}\\033[0m"
        )

        try:
            # Decode the base64 image
            image_data = base64.b64decode(self.last_main_camera_image_b64)

            # Create message
            msg = MIMEMultipart()
            msg["From"] = self.sender_email
            msg["To"] = recipient
            msg["Subject"] = subject

            # Attach the text message
            msg.attach(MIMEText(message, "plain"))

            # Attach the image
            image = MIMEImage(image_data, name="robot_capture.jpg")
            msg.attach(image)

            # Connect to server and send
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.sender_email, self.password)
            server.send_message(msg)
            server.quit()

            self.logger.info(
                f"\\033[92m[BrainClient] Email with picture sent to {recipient}\\033[0m"
            )
            return f"Email with picture sent to {recipient}", PrimitiveResult.SUCCESS

        except Exception as e:
            self.logger.error(f"[SendPictureViaEmail] Failed to send email: {str(e)}")
            return f"Failed to send email: {str(e)}", PrimitiveResult.FAILURE

    def cancel(self):
        """
        Cancel the email sending operation (typically quick, so not much to do).
        """
        self.logger.info(
            "\\033[91m[BrainClient] Email sending cannot be effectively canceled "
            "once started.\\033[0m"
        )
        return (
            "Email sending is an atomic operation and cannot be effectively canceled."
        )
