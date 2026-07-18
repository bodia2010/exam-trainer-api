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
from dataclasses import dataclass

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


@dataclass(frozen=True)
class CourseRecord:
    """One course document, including delete-wins sync metadata.

    ``revision == 0`` represents a legacy active document written before
    CR-07.  A deleted document deliberately remains in Firestore as a
    tombstone; deleting the document itself would let a stale offline upload
    recreate it.
    """

    course_id: str
    revision: int
    deleted: bool
    updated_at: str | None
    course_json: str | None


@dataclass(frozen=True)
class CourseMutationResult:
    """Outcome of a compare-and-set course mutation.

    ``bool(result)`` keeps the old internal bool API convenient for callers
    which only care whether delivery succeeded, while the endpoint can expose
    the revision/conflict distinction to new clients.
    """

    status: str  # success | conflict | unavailable | rejected
    revision: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == 'success'

    def __bool__(self) -> bool:
        return self.ok


def _courses_url(uid: str, course_id: str | None = None) -> str:
    base = (
        f'https://firestore.googleapis.com/v1/projects/{_PROJECT_ID}'
        f'/databases/(default)/documents/users/{uid}/courses'
    )
    return f'{base}/{course_id}' if course_id else base


def _course_id_from_document(doc: dict) -> str | None:
    name = doc.get('name')
    if not isinstance(name, str) or not name.rsplit('/', 1)[-1]:
        return None
    return name.rsplit('/', 1)[-1]


def _int_field(fields: dict, name: str, default: int = 0) -> int:
    try:
        value = fields.get(name, {}).get('integerValue', default)
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _record_from_document(doc: dict) -> CourseRecord | None:
    course_id = _course_id_from_document(doc)
    if not course_id:
        return None
    fields = doc.get('fields', {})
    if not isinstance(fields, dict):
        return None
    course_json = fields.get('json', {}).get('stringValue')
    if not isinstance(course_json, str):
        course_json = None
    updated_at = fields.get('updatedAt', {}).get('timestampValue')
    if not isinstance(updated_at, str):
        updated_at = doc.get('updateTime') if isinstance(doc.get('updateTime'), str) else None
    return CourseRecord(
        course_id=course_id,
        revision=_int_field(fields, 'revision'),
        deleted=fields.get('deleted', {}).get('booleanValue') is True,
        updated_at=updated_at,
        course_json=course_json,
    )


def _get_course_document(uid: str, course_id: str, headers: dict):
    """Returns ``(document, None)`` / ``(None, 'missing')`` / unavailable.

    Fetching the document before every mutation supplies Firestore's
    ``updateTime`` for the REST precondition.  This avoids a read-then-write
    race without adding the heavy firebase-admin dependency.
    """
    try:
        response = requests.get(
            _courses_url(uid, course_id), headers=headers, timeout=10)
    except Exception:
        return None, 'unavailable'
    if response.status_code == 404:
        return None, 'missing'
    if response.status_code != 200:
        return None, 'unavailable'
    try:
        document = response.json()
    except ValueError:
        return None, 'unavailable'
    if not isinstance(document, dict) or not document.get('updateTime'):
        return None, 'unavailable'
    return document, None


def _patch_course(
        uid: str,
        course_id: str,
        headers: dict,
        fields: dict,
        *,
        update_time: str | None = None,
        create_only: bool = False,
        update_mask: tuple[str, ...] = ()) -> str:
    """Applies a Firestore document PATCH with a real CAS precondition."""
    params: list[tuple[str, str]] = []
    if create_only:
        params.append(('currentDocument.exists', 'false'))
    elif update_time:
        params.append(('currentDocument.updateTime', update_time))
    else:
        return 'unavailable'
    for field in update_mask:
        params.append(('updateMask.fieldPaths', field))
    try:
        response = requests.patch(
            _courses_url(uid, course_id),
            headers=headers,
            params=params,
            json={'fields': fields},
            timeout=15,
        )
    except Exception:
        return 'unavailable'
    if response.status_code == 200:
        return 'success'
    # Firestore represents a failed currentDocument precondition as ABORTED
    # (409).  Accept 412 too for compatible proxies/emulators.
    if response.status_code in (409, 412):
        return 'conflict'
    return 'unavailable'


def _active_course_fields(course_json: str, revision: int) -> dict:
    return {
        'json': {'stringValue': course_json},
        'deleted': {'booleanValue': False},
        'revision': {'integerValue': str(revision)},
        'updatedAt': {'timestampValue': _now_iso()},
    }


def _tombstone_fields(revision: int) -> dict:
    return {
        'deleted': {'booleanValue': True},
        'revision': {'integerValue': str(revision)},
        'updatedAt': {'timestampValue': _now_iso()},
    }


def save_course(
        uid: str,
        course_id: str,
        course_json: str,
        expected_revision: int | None = None,
) -> CourseMutationResult:
    """Saves a course only if it has not been deleted or changed meanwhile.

    Legacy documents with no metadata are active at revision 0.  A tombstone
    is never converted back into an active course, even for legacy clients
    which do not send ``expected_revision``.
    """
    if (not _credentials or
            len(course_json.encode('utf-8')) > _MAX_COURSE_JSON_BYTES):
        return CourseMutationResult('rejected')
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
    except Exception:
        return CourseMutationResult('unavailable')

    document, error = _get_course_document(uid, course_id, headers)
    if error == 'unavailable':
        return CourseMutationResult('unavailable')
    if error == 'missing':
        if expected_revision is not None and expected_revision != 0:
            return CourseMutationResult('conflict')
        revision = 1
        status = _patch_course(
            uid, course_id, headers, _active_course_fields(course_json, revision),
            create_only=True,
        )
        return CourseMutationResult(status, revision if status == 'success' else None)

    record = _record_from_document(document)
    if record is None:
        return CourseMutationResult('unavailable')
    if record.deleted:
        # Delete-wins: a stale device must explicitly import a new course ID,
        # never revive a prior one.
        return CourseMutationResult('conflict')
    if expected_revision is not None and expected_revision != record.revision:
        return CourseMutationResult('conflict')
    revision = record.revision + 1
    status = _patch_course(
        uid, course_id, headers, _active_course_fields(course_json, revision),
        update_time=document['updateTime'],
        update_mask=('json', 'deleted', 'revision', 'updatedAt'),
    )
    return CourseMutationResult(status, revision if status == 'success' else None)


def list_course_records(uid: str) -> list[CourseRecord] | None:
    """Returns all active and deleted course records, or None on failure.

    Tombstones are intentionally included so another device can remove an old
    local copy instead of uploading it back to the cloud.
    """
    if not _credentials:
        return None
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
        response = requests.get(_courses_url(uid), headers=headers, timeout=15)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        documents = response.json().get('documents', [])
    except (ValueError, AttributeError):
        return None
    result = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        record = _record_from_document(document)
        if record is not None:
            result.append(record)
    return result


def list_courses(uid: str) -> list[str]:
    """Returns the raw JSON string of every course stored for this
    account. Malformed/unreadable docs are skipped rather than failing
    the whole sync."""
    records = list_course_records(uid)
    if records is None:
        return []
    return [record.course_json for record in records
            if not record.deleted and record.course_json]


def delete_course(
        uid: str,
        course_id: str,
        expected_revision: int | None = None,
) -> CourseMutationResult:
    """Writes a permanent tombstone instead of deleting a course document.

    Delete is intentionally stronger than a stale expected revision: once the
    caller asks to delete, an active remote version is replaced by a
    tombstone.  A concurrent write may make one CAS attempt conflict; retrying
    once lets normal delete-vs-upload races still converge to delete-wins.
    """
    if not _credentials:
        return CourseMutationResult('unavailable')
    try:
        headers = {'Authorization': f'Bearer {_access_token()}'}
    except Exception:
        return CourseMutationResult('unavailable')

    # ``expected_revision`` is parsed by the API and deliberately not used as
    # a rejection condition here: an old offline deletion must not resurrect
    # data merely because another device edited the course first.
    del expected_revision
    for _ in range(2):
        document, error = _get_course_document(uid, course_id, headers)
        if error == 'unavailable':
            return CourseMutationResult('unavailable')
        if error == 'missing':
            revision = 1
            status = _patch_course(
                uid, course_id, headers, _tombstone_fields(revision),
                create_only=True,
            )
            if status != 'conflict':
                return CourseMutationResult(
                    status, revision if status == 'success' else None)
            continue

        record = _record_from_document(document)
        if record is None:
            return CourseMutationResult('unavailable')
        if record.deleted:
            return CourseMutationResult('success', record.revision)
        revision = record.revision + 1
        status = _patch_course(
            uid, course_id, headers, _tombstone_fields(revision),
            update_time=document['updateTime'],
            # Including json in the update mask deletes the old payload.
            update_mask=('json', 'deleted', 'revision', 'updatedAt'),
        )
        if status != 'conflict':
            return CourseMutationResult(
                status, revision if status == 'success' else None)
    return CourseMutationResult('conflict')


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
