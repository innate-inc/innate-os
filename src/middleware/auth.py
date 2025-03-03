import os
from typing import Optional, Dict, Any
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
import requests
from functools import lru_cache

# Auth0 configuration
AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN", "")
AUTH0_AUDIENCE = os.getenv("AUTH0_AUDIENCE", "")
AUTH0_ALGORITHMS = ["RS256"]

# Security scheme for Swagger UI
security = HTTPBearer()


# Cache the JWKS to avoid fetching it for every request
@lru_cache(maxsize=1)
def get_jwks() -> Dict[str, Any]:
    """Fetch the JSON Web Key Set from Auth0"""
    if not AUTH0_DOMAIN:
        return {}

    jwks_url = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
    try:
        jwks = requests.get(jwks_url).json()
        return jwks
    except Exception as e:
        print(f"Error fetching JWKS: {e}")
        return {}


def get_signing_key(token: str) -> Optional[Dict[str, Any]]:
    """Get the signing key used to sign the token"""
    if not AUTH0_DOMAIN:
        return None

    try:
        jwks = get_jwks()
        unverified_header = jwt.get_unverified_header(token)
        for key in jwks.get("keys", []):
            if key["kid"] == unverified_header["kid"]:
                return key
        return None
    except Exception as e:
        print(f"Error getting signing key: {e}")
        return None


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    """Verify the JWT token from Auth0"""
    # Skip verification if AUTH0_DOMAIN is not set (development mode)
    if not AUTH0_DOMAIN or not AUTH0_AUDIENCE:
        print("Auth0 configuration not set, skipping token verification")
        return {"sub": "development-user"}

    token = credentials.credentials

    try:
        # Print token for debugging (first 20 chars)
        print(f"Token prefix: {token[:20]}...")

        # Get the signing key
        signing_key = get_signing_key(token)

        if not signing_key:
            print(
                "Unable to find appropriate key to verify token, using development user"
            )
            return {"sub": "development-user"}

        # python-jose can use the JWK directly without the JWK class
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=AUTH0_ALGORITHMS,
            audience=AUTH0_AUDIENCE,
            issuer=f"https://{AUTH0_DOMAIN}/",
        )
        return payload
    except jwt.ExpiredSignatureError:
        print("Token has expired, using development user")
        return {"sub": "development-user"}
    except jwt.JWTClaimsError as e:
        print(f"Invalid claims: {str(e)}, using development user")
        return {"sub": "development-user"}
    except Exception as e:
        print(f"Invalid token: {str(e)}, using development user")
        return {"sub": "development-user"}


# Simplified version for routes that don't need the full token payload
async def get_current_user(
    token_payload: Dict[str, Any] = Depends(verify_token)
) -> Dict[str, Any]:
    """Extract the user information from the token payload"""
    print(f"Token payload keys: {list(token_payload.keys())}")
    print(f"Token payload: {token_payload}")

    user_id = token_payload.get("sub", "")

    # Extract email from token payload - try different possible keys
    email = token_payload.get("email", "")

    # If email is not found directly, try to find it in other places
    if not email:
        # Check if it's in a nested structure
        if "https://example.com/email" in token_payload:
            email = token_payload["https://example.com/email"]
        elif "https://your-domain.auth0.com/email" in token_payload:
            email = token_payload["https://your-domain.auth0.com/email"]
        # Try with the actual Auth0 domain
        elif f"https://{AUTH0_DOMAIN}/email" in token_payload:
            email = token_payload[f"https://{AUTH0_DOMAIN}/email"]

    # If we still don't have an email and we're in development mode, use a default
    if not email and user_id == "development-user":
        email = "axel@innate.bot"  # Use a default email for development

    print(f"Extracted user_id: {user_id}, email: {email}")

    return {"user_id": user_id, "email": email, "token_payload": token_payload}
