"""Reads users/{uid}.isPremium from Firestore without the firebase-admin
SDK — same reasoning as firebase_auth.py: firebase-admin/google-cloud-
firestore pull in grpc/protobuf, risking the Vercel 250MB unzipped-
function limit that broke an earlier version of this backend. google-auth
alone (just service-account JWT signing + OAuth2 token exchange) plus a
plain REST call is a few hundred KB.

Schema matches the sister deutch-lernen app exactly: collection "users",
document ID = Firebase Auth uid, boolean field "isPremium" — so the same
Firestore Console workflow (open the doc, flip the checkbox) works
identically for both apps.
"""
import json
import os

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID', '')

_credentials = None
_service_account_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '')
if _service_account_json:
    _credentials = service_account.Credentials.from_service_account_info(
        json.loads(_service_account_json),
        scopes=['https://www.googleapis.com/auth/datastore'],
    )


def _access_token() -> str:
    if not _credentials.valid:
        _credentials.refresh(GoogleAuthRequest())
    return _credentials.token


def is_premium(uid: str) -> bool:
    """Defaults to False on ANY failure — missing doc, network error,
    misconfigured credentials. Free tier is the safe fallback; never
    silently grant premium because a lookup failed."""
    if not _credentials:
        return False
    try:
        url = (
            f'https://firestore.googleapis.com/v1/projects/{_PROJECT_ID}'
            f'/databases/(default)/documents/users/{uid}'
        )
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {_access_token()}'},
            timeout=10,
        )
        if resp.status_code != 200:
            return False
        fields = resp.json().get('fields', {})
        return fields.get('isPremium', {}).get('booleanValue') is True
    except Exception:
        return False
