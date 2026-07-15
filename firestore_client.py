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
import datetime
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


# Firebase Auth places no limit on how many devices can be signed into the
# same account at once — a leaked or resold login/password otherwise costs
# nothing extra to use. This is the only real check against that: each
# account may have at most MAX_DEVICES registered devices; a new device
# beyond that is refused until the user explicitly evicts the others.
# Schema mirrors the sister deutch-lernen app: users/{uid}/devices/{deviceId}.
_MAX_DEVICES = 2


def _devices_url(uid: str, device_id: str | None = None) -> str:
    base = (
        f'https://firestore.googleapis.com/v1/projects/{_PROJECT_ID}'
        f'/databases/(default)/documents/users/{uid}/devices'
    )
    return f'{base}/{device_id}' if device_id else base


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.%fZ')


def check_and_register_device(uid: str, device_id: str, device_name: str) -> bool:
    """Registers this device against the account, enforcing MAX_DEVICES.
    Fails open (returns True) on any Firestore/network error — an outage
    here must never lock a paying user out of the app."""
    if not _credentials:
        return True
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}

        existing = requests.get(
            _devices_url(uid, device_id), headers=headers, timeout=10)
        if existing.status_code == 200:
            # Already-registered device — refresh lastSeen, doesn't count
            # against the limit again.
            requests.patch(
                _devices_url(uid, device_id),
                headers=headers,
                params={'updateMask.fieldPaths': 'lastSeen'},
                json={'fields': {'lastSeen': {'timestampValue': _now_iso()}}},
                timeout=10,
            )
            return True

        listing = requests.get(
            _devices_url(uid), headers=headers, timeout=10)
        if listing.status_code == 200:
            count = len(listing.json().get('documents', []))
            if count >= _MAX_DEVICES:
                return False

        requests.patch(
            _devices_url(uid, device_id),
            headers=headers,
            json={'fields': {
                'deviceId': {'stringValue': device_id},
                'name': {'stringValue': device_name},
                'registeredAt': {'timestampValue': _now_iso()},
                'lastSeen': {'timestampValue': _now_iso()},
            }},
            timeout=10,
        )
        return True
    except Exception:
        return True


def force_register_device(uid: str, device_id: str, device_name: str) -> bool:
    """Evicts every other device on the account and registers this one —
    the "use this device instead" action on the device-limit screen."""
    if not _credentials:
        return False
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
        listing = requests.get(
            _devices_url(uid), headers=headers, timeout=10)
        if listing.status_code != 200:
            return False
        for doc in listing.json().get('documents', []):
            name = doc.get('name', '')  # full resource path
            if name:
                deleted = requests.delete(
                    f'https://firestore.googleapis.com/v1/{name}',
                    headers=headers, timeout=10)
                if deleted.status_code not in (200, 404):
                    return False
        registered = requests.patch(
            _devices_url(uid, device_id),
            headers=headers,
            json={'fields': {
                'deviceId': {'stringValue': device_id},
                'name': {'stringValue': device_name},
                'registeredAt': {'timestampValue': _now_iso()},
                'lastSeen': {'timestampValue': _now_iso()},
            }},
            timeout=10,
        )
        return registered.status_code == 200
    except Exception:
        return False


# Courses are stored as a single opaque JSON blob per document rather than
# translated field-by-field into Firestore's typed field format — the
# ParsedCourse shape (nested sections -> variant lists -> per-section-type
# schemas) has no fixed structure worth modelling in Firestore itself; the
# client is the only thing that needs to read it back. A Firestore document
# maxes out at 1 MiB total, so oversized courses are skipped rather than
# erroring — cross-device sync is a convenience, not something an import
# should ever fail over.
_MAX_COURSE_JSON_BYTES = 900_000


def _courses_url(uid: str, course_id: str | None = None) -> str:
    base = (
        f'https://firestore.googleapis.com/v1/projects/{_PROJECT_ID}'
        f'/databases/(default)/documents/users/{uid}/courses'
    )
    return f'{base}/{course_id}' if course_id else base


def save_course(uid: str, course_id: str, course_json: str) -> bool:
    if not _credentials or len(course_json.encode('utf-8')) > _MAX_COURSE_JSON_BYTES:
        return False
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
        resp = requests.patch(
            _courses_url(uid, course_id),
            headers=headers,
            json={'fields': {
                'json': {'stringValue': course_json},
                'updatedAt': {'timestampValue': _now_iso()},
            }},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception:
        return False


def list_courses(uid: str) -> list[str]:
    """Returns the raw JSON string of every course stored for this
    account. Malformed/unreadable docs are skipped rather than failing
    the whole sync."""
    if not _credentials:
        return []
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
        resp = requests.get(_courses_url(uid), headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        result = []
        for doc in resp.json().get('documents', []):
            value = doc.get('fields', {}).get('json', {}).get('stringValue')
            if value:
                result.append(value)
        return result
    except Exception:
        return []


def delete_course(uid: str, course_id: str) -> bool:
    if not _credentials:
        return False
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
        response = requests.delete(
            _courses_url(uid, course_id), headers=headers, timeout=10)
        return response.status_code in (200, 404)
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
