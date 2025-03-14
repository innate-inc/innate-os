from typing import Dict, Any
import os

# List of authorized users (emails)
AUTHORIZED_USERS = [
    "axel@innate.bot",  # Axel's email
]

# Domain whitelist - any email with these domains is authorized
AUTHORIZED_DOMAINS = [
    "innate.bot",  # All Innate employees
]


def is_authorized(user: Dict[str, Any]) -> bool:
    """
    Check if a user is authorized to access the application.

    Args:
        user: The user object from Auth0 containing email and other properties

    Returns:
        bool: True if the user is authorized, False otherwise
    """
    # Check if authentication is required
    need_oauth = os.environ.get("NEED_OAUTH", "True").lower() == "true"
    if not need_oauth:
        print("Authentication bypassed: NEED_OAUTH is False")
        return True

    if not user or not user.get("email"):
        print(f"User not authorized: No user or no email - {user}")
        return False

    email = user.get("email", "").lower()
    print(f"Checking authorization for email: {email}")

    # Check if the email is in the authorized users list
    if email in AUTHORIZED_USERS:
        print(f"User authorized: Email {email} is in AUTHORIZED_USERS list")
        return True

    # Check if the email domain is in the authorized domains list
    for domain in AUTHORIZED_DOMAINS:
        if email.endswith(f"@{domain}"):
            print(
                f"User authorized: Email {email} domain is in AUTHORIZED_DOMAINS list"
            )
            return True

    print(f"User not authorized: Email {email} is not in any authorized list")
    return False


# Stripe payment link for unauthorized users
STRIPE_PAYMENT_LINK = "https://buy.stripe.com/your_payment_link_here"
