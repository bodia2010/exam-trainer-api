"""Verifies Firebase ID tokens without the firebase-admin SDK.

firebase-admin pulls in grpc/protobuf/google-api-core — the same class of
dependency bloat that pushed a previous version of this function over
Vercel's 250MB unzipped-function limit (see main.py's MarkItDown/Gemini
history). A Firebase ID token is a standard RS256 JWT signed by Google;
PyJWT + its public JWKS endpoint verifies it in a few KB, no SDK needed.
"""
import json
import os

import jwt
import requests
from jwt import PyJWKClient
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

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


# --- Admin operation: account deletion --------------------------------
#
# Reuses the SAME service-account JSON already configured for
# firestore_client.py (FIREBASE_SERVICE_ACCOUNT_JSON) rather than adding a
# new secret. Google's Identity Platform REST API accepts a service-account
# OAuth2 access token scoped to `identitytoolkit` for admin-authorized
# operations (delete-by-uid) — this is the same mechanism the firebase-admin
# SDK uses internally for `deleteUser()`, just called directly over REST so
# we don't need the firebase-admin dependency (see firestore_client.py's
# module docstring for why that SDK is avoided here: grpc/protobuf risk
# Vercel's unzipped-function size limit).
#
# The alternative — deleting via the *user's own* ID token with
# `accounts:delete?key=<web-api-key>` — was considered but rejected: it
# would require introducing a brand-new secret (a Firebase Web API key) that
# nothing else in this backend currently reads from the environment, whereas
# this approach reuses credentials already proven to be configured in prod.
_admin_credentials = None
_service_account_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '')
if _service_account_json:
    _admin_credentials = service_account.Credentials.from_service_account_info(
        json.loads(_service_account_json),
        scopes=['https://www.googleapis.com/auth/identitytoolkit'],
    )


def _admin_access_token() -> str:
    if not _admin_credentials.valid:
        _admin_credentials.refresh(GoogleAuthRequest())
    return _admin_credentials.token


def delete_user(uid: str) -> bool:
    """Deletes the Firebase Auth account for uid via the Identity Platform
    admin REST API. Returns False on ANY failure (missing/misconfigured
    credentials, network error, non-2xx response) — callers must treat
    False as 'auth account NOT confirmed deleted' and surface that to the
    caller rather than assume success."""
    if not _admin_credentials:
        return False
    try:
        resp = requests.post(
            f'https://identitytoolkit.googleapis.com/v1/projects/'
            f'{_PROJECT_ID}/accounts:delete',
            headers={'Authorization': f'Bearer {_admin_access_token()}'},
            json={'localId': uid},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f'FIREBASE_AUTH_DELETE_ERROR uid={uid} {type(e).__name__}: {e}')
        return False
