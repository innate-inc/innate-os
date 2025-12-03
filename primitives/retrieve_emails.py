#!/usr/bin/env python3
import imaplib
import email
from email.header import decode_header
from brain_client.primitive_types import Primitive, PrimitiveResult


class RetrieveEmails(Primitive):
    """
    Primitive for retrieving recent emails from an IMAP server.
    This retrieves email titles and content from the configured email account.
    """

    def __init__(self, logger):
        self.logger = logger
        # Email server configuration - using same credentials as send_email
        self.imap_server = "imap.gmail.com"  # Gmail IMAP server
        self.imap_port = 993
        self.email = "axel@innate.bot"  # Same email as in send_email
        self.password = "ncbtviozpktktsdm"  # Same app password as in send_email

    @property
    def name(self):
        return "retrieve_emails"

    def guidelines(self):
        return (
            "Use to retrieve recent emails from the configured email account. "
            "Provide the number of emails to retrieve (default is 5). "
            "Returns email subjects and content. This should be used when you need "
            "to check for recent messages or respond to incoming communications."
        )

    def _decode_header_value(self, value):
        """
        Decode email header values that might be encoded.
        
        Args:
            value: The header value to decode
            
        Returns:
            str: The decoded header value
        """
        if value is None:
            return ""
        
        decoded_parts = decode_header(value)
        decoded_value = ""
        
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                if encoding:
                    try:
                        decoded_value += part.decode(encoding)
                    except (UnicodeDecodeError, LookupError):
                        decoded_value += part.decode('utf-8', errors='ignore')
                else:
                    decoded_value += part.decode('utf-8', errors='ignore')
            else:
                decoded_value += str(part)
                
        return decoded_value

    def _extract_email_content(self, msg):
        """
        Extract text content from an email message.
        
        Args:
            msg: Email message object
            
        Returns:
            str: The text content of the email
        """
        content = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                
                # Skip attachments
                if "attachment" in content_disposition:
                    continue
                    
                if content_type == "text/plain":
                    try:
                        body = part.get_payload(decode=True)
                        if body:
                            content += body.decode('utf-8', errors='ignore')
                    except Exception as e:
                        self.logger.warning(f"Could not decode email part: {e}")
                        continue
                elif content_type == "text/html" and not content:
                    # Only use HTML if no plain text is available
                    try:
                        body = part.get_payload(decode=True)
                        if body:
                            content += body.decode('utf-8', errors='ignore')
                    except Exception as e:
                        self.logger.warning(f"Could not decode HTML email part: {e}")
                        continue
        else:
            # Single part message
            try:
                body = msg.get_payload(decode=True)
                if body:
                    content = body.decode('utf-8', errors='ignore')
            except Exception as e:
                self.logger.warning(f"Could not decode email body: {e}")
                
        return content.strip()

    def execute(self, count: int = 5):
        """
        Retrieves the most recent emails from the configured IMAP server.

        Args:
            count (int): Number of recent emails to retrieve (default: 5, max: 20)

        Returns:
            tuple: (result_message, result_status) where result_status is a
                   PrimitiveResult enum value
        """
        # Limit the count to prevent overwhelming responses
        count = min(max(1, count), 20)
        
        self.logger.info(
            f"\033[96m[BrainClient] Retrieving last {count} emails\033[0m"
        )

        try:
            # Connect to the IMAP server
            mail = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
            mail.login(self.email, self.password)
            
            # Select the INBOX
            mail.select("INBOX")
            
            # Search for all emails and get the most recent ones
            status, messages = mail.search(None, "ALL")
            
            if status != "OK":
                self.logger.error("Failed to search emails")
                mail.logout()
                return "Failed to search emails", PrimitiveResult.FAILURE
            
            # Get message IDs
            email_ids = messages[0].split()
            
            if not email_ids:
                self.logger.info("No emails found in inbox")
                mail.logout()
                return "No emails found in inbox", PrimitiveResult.SUCCESS
            
            # Get the most recent emails (last 'count' emails)
            recent_email_ids = email_ids[-count:]
            
            emails_info = []
            
            for email_id in reversed(recent_email_ids):  # Most recent first
                try:
                    # Fetch the email
                    status, msg_data = mail.fetch(email_id, "(RFC822)")
                    
                    if status != "OK":
                        self.logger.warning(f"Failed to fetch email {email_id}")
                        continue
                    
                    # Parse the email
                    msg = email.message_from_bytes(msg_data[0][1])
                    
                    # Extract email information
                    subject = self._decode_header_value(msg.get("Subject", "No Subject"))
                    sender = self._decode_header_value(msg.get("From", "Unknown Sender"))
                    date = msg.get("Date", "Unknown Date")
                    content = self._extract_email_content(msg)
                    
                    # Truncate content if it's too long
                    if len(content) > 500:
                        content = content[:500] + "... [truncated]"
                    
                    email_info = {
                        "subject": subject,
                        "from": sender,
                        "date": date,
                        "content": content if content else "[No text content available]"
                    }
                    
                    emails_info.append(email_info)
                    
                except Exception as e:
                    self.logger.warning(f"Error processing email {email_id}: {e}")
                    continue
            
            mail.logout()
            
            if not emails_info:
                return "No emails could be retrieved", PrimitiveResult.FAILURE
            
            # Format the result message
            result_lines = [f"Retrieved {len(emails_info)} recent email(s):\n"]
            
            for i, email_info in enumerate(emails_info, 1):
                result_lines.append(f"Email {i}:")
                result_lines.append(f"  Subject: {email_info['subject']}")
                result_lines.append(f"  From: {email_info['from']}")
                result_lines.append(f"  Date: {email_info['date']}")
                result_lines.append(f"  Content: {email_info['content']}")
                result_lines.append("")  # Empty line between emails
            
            result_message = "\n".join(result_lines)
            
            self.logger.info(
                f"\033[92m[BrainClient] Successfully retrieved {len(emails_info)} emails\033[0m"
            )
            
            return result_message, PrimitiveResult.SUCCESS
            
        except imaplib.IMAP4.error as e:
            error_msg = f"IMAP error: {str(e)}"
            self.logger.error(error_msg)
            return error_msg, PrimitiveResult.FAILURE
            
        except Exception as e:
            error_msg = f"Failed to retrieve emails: {str(e)}"
            self.logger.error(error_msg)
            return error_msg, PrimitiveResult.FAILURE

    def cancel(self):
        """
        Cancel the email retrieval operation.

        Since email retrieval is typically a quick operation that completes almost
        instantly, this method doesn't do much. It's implemented to satisfy the
        Primitive interface.

        Returns:
            str: A message describing the cancellation result.
        """
        self.logger.info(
            "\033[91m[BrainClient] Email retrieval operation cannot be canceled "
            "once started\033[0m"
        )
        return (
            "Email retrieval is an atomic operation that cannot be canceled once started"
        )
