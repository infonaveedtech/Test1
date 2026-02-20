
# auth.py
from typing import Any, Dict

import os
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt, JWTError
from pydantic import BaseModel

# --- Config from environment ---

# This must match the secret your Java/Spring app uses in:
# .signWith(SignatureAlgorithm.HS256, MarlinUtility.Security.getKey())
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")  # change in real env
JWT_ALG = os.getenv("JWT_ALG", "HS256")
JWT_AUD = os.getenv("JWT_AUD", "web")  # should match "aud" claim from the Java token

# HTTPBearer will read the "Authorization: Bearer <token>" header for us
bearer_scheme = HTTPBearer(auto_error=True)


class CurrentUser(BaseModel):
    """
    Minimal representation of the authenticated user.
    For now we ONLY care that the user is valid; we don't use roles etc.
    """
    username: str
    claims: Dict[str, Any]


def _decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate the JWT:
      - verifies signature using JWT_SECRET + JWT_ALG
      - verifies expiration (exp)
      - optionally verifies audience (aud) if JWT_AUD is set
    """
    try:
        options = {"verify_aud": bool(JWT_AUD)}
        decoded = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALG],
            audience=JWT_AUD if JWT_AUD else None,
            options=options,
        )
        return decoded
    except JWTError as e:
        # Any issue (bad signature, expired, wrong audience, etc.) -> 401
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
        ) from e


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> CurrentUser:
    """
    FastAPI dependency:
      - Reads Authorization header
      - Ensures it's a Bearer token
      - Decodes + validates JWT
      - Returns a CurrentUser object
    """
    if creds.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be Bearer <token>",
        )

    token = creds.credentials
    claims = _decode_token(token)

    username = claims.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'sub' (subject) claim",
        )

    return CurrentUser(username=username, claims=claims)
 