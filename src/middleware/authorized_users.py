from typing import Dict, Any

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
    if not user or not user.get("email"):
        return False

    email = user.get("email", "").lower()

    # Check if the email is in the authorized users list
    if email in AUTHORIZED_USERS:
        return True

    # Check if the email domain is in the authorized domains list
    for domain in AUTHORIZED_DOMAINS:
        if email.endswith(f"@{domain}"):
            return True

    return False


# Stripe payment link for unauthorized users
STRIPE_PAYMENT_LINK = "https://buy.stripe.com/your_payment_link_here"
