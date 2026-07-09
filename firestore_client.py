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

# --- Account deletion ---------------------------------------------------
#
# users/{uid} has three known subcollections — devices (registered
# devices, see check_and_register_device above), courses (imported PDFs,
# see save_course above), and progress (reserved for future exercise-
# progress tracking; not written anywhere yet, but wiped defensively so
# account deletion doesn't silently leave data behind the moment something
# starts writing to it).
_ACCOUNT_SUBCOLLECTIONS = ('devices', 'courses', 'progress')


def _delete_all_in_subcollection(uid: str, collection: str) -> None:
    """Deletes every document in users/{uid}/{collection}. Best-effort per
    document — one stuck/failed delete must not stop the rest of the
    subcollection (or the other subcollections) from being wiped."""
    if not _credentials:
        return
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
        url = (
            f'https://firestore.googleapis.com/v1/projects/{_PROJECT_ID}'
            f'/databases/(default)/documents/users/{uid}/{collection}'
        )
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return
        for doc in resp.json().get('documents', []):
            name = doc.get('name', '')  # full resource path
            if not name:
                continue
            try:
                requests.delete(
                    f'https://firestore.googleapis.com/v1/{name}',
                    headers=headers, timeout=10)
            except Exception:
                continue
    except Exception:
        pass


def delete_user_data(uid: str) -> bool:
    """Deletes ALL Firestore data for uid ahead of Firebase Auth account
    deletion: every document in devices/courses/progress, then the
    users/{uid} document itself. Subcollections are wiped best-effort (a
    single stuck doc must not block the rest), but the final delete of
    users/{uid} is the hard signal — this returns False if THAT fails (or
    if credentials aren't configured at all), since a lingering root
    document is still "user data left behind" even if every subcollection
    doc is gone. A 404 on the root document (e.g. a free user who never
    got a users/{uid} doc written) counts as success — there's nothing
    left to delete either way."""
    if not _credentials:
        return False
    for collection in _ACCOUNT_SUBCOLLECTIONS:
        _delete_all_in_subcollection(uid, collection)
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
        url = (
            f'https://firestore.googleapis.com/v1/projects/{_PROJECT_ID}'
            f'/databases/(default)/documents/users/{uid}'
        )
        resp = requests.delete(url, headers=headers, timeout=10)
        return resp.status_code in (200, 404)
    except Exception as e:
        print(f'FIRESTORE_DELETE_USER_ERROR uid={uid} {type(e).__name__}: {e}')
        return False
