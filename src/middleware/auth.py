import os
from typing import Optional, Dict, Any
from fastapi import HTTPException, Depends
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
    signing_key = get_signing_key(token)

    if not signing_key:
        raise HTTPException(
            status_code=401,
            detail="Unable to find appropriate key to verify token",
        )

    try:
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
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.JWTClaimsError:
        raise HTTPException(
            status_code=401,
            detail="Invalid claims: please check the audience and issuer",
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")


# Simplified version for routes that don't need the full token payload
async def get_current_user(
    token_payload: Dict[str, Any] = Depends(verify_token)
) -> str:
    """Extract the user ID from the token payload"""
    print(f"Token payload: {token_payload}")
    return token_payload.get("sub", "")
