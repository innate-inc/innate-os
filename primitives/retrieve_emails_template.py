#!/usr/bin/env python3
import imaplib
import email
from brain_client.primitive_types import Primitive, PrimitiveResult


class RetrieveEmails(Primitive):
    def __init__(self, logger):
        self.logger = logger
        self.imap_server = "imap.gmail.com"
        self.email = "your_email@gmail.com"

    @property
    def name(self):
        return "retrieve_emails"

    def guidelines(self):
        return "Use to retrieve recent emails. Provide count (default 5). Returns subjects and content."

    def execute(self, count: int = 5):
        count = min(max(1, count), 20)
        try:
            mail = imaplib.IMAP4_SSL(self.imap_server, 993)
            mail.login(self.email, self.password)
            # ... fetch and process emails ...
            email_data = "Email 1: Subject, From, Content..."
            self._send_feedback(email_data)
            return f"Retrieved {count} emails with subjects and content", PrimitiveResult.SUCCESS
        except Exception as e:
            return f"Failed to retrieve emails: {str(e)}", PrimitiveResult.FAILURE
