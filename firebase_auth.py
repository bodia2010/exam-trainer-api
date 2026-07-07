"""Verifies Firebase ID tokens without the firebase-admin SDK.

firebase-admin pulls in grpc/protobuf/google-api-core — the same class of
dependency bloat that pushed a previous version of this function over
Vercel's 250MB unzipped-function limit (see main.py's MarkItDown/Gemini
history). A Firebase ID token is a standard RS256 JWT signed by Google;
PyJWT + its public JWKS endpoint verifies it in a few KB, no SDK needed.
"""
import os

import jwt
from jwt import PyJWKClient

_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID', '')
_JWKS_URL = (
    'https://www.googleapis.com/service_accounts/v1/jwk/'
    'securetoken@system.gserviceaccount.com'
)
# Cached across warm invocations of the same serverless instance — avoids
# refetching Google's public keys on every request.
_jwks_client = PyJWKClient(_JWKS_URL, cache_keys=True, lifespan=3600)


def verify_id_token(token: str) -> str:
    """Returns the verified user's Firebase UID. Raises on any failure
    (expired, wrong project, bad signature, malformed, ...) — callers
    should treat any exception here as 401 Unauthorized."""
    signing_key = _jwks_client.get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=['RS256'],
        audience=_PROJECT_ID,
        issuer=f'https://securetoken.google.com/{_PROJECT_ID}',
    )
    uid = payload.get('sub')
    if not uid or not isinstance(uid, str):
        raise ValueError('token has no sub claim')
    return uid


def authenticate_request(headers) -> str | None:
    """Returns the caller's UID, or None if the request isn't authenticated
    with a valid Firebase ID token."""
    auth_header = headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    token = auth_header[len('Bearer '):].strip()
    if not token:
        return None
    try:
        return verify_id_token(token)
    except Exception:
        return None
