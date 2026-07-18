import unittest
from unittest.mock import Mock, patch

import firestore_client
import main


class SyncEndpointContractTest(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'force_register_device')
    def test_force_device_only_confirms_real_success(
            self, force_register, _authenticate):
        force_register.return_value = False
        failed = self.client.post(
            '/api/device/force',
            json={'deviceId': 'device-1', 'deviceName': 'Phone'},
        )
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed.get_json(), {'ok': False})

        force_register.return_value = True
        succeeded = self.client.post(
            '/api/device/force',
            json={'deviceId': 'device-1', 'deviceName': 'Phone'},
        )
        self.assertEqual(succeeded.status_code, 200)
        self.assertEqual(succeeded.get_json(), {'ok': True})

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'delete_course')
    def test_course_delete_only_confirms_real_success(
            self, delete_course, _authenticate):
        delete_course.return_value = False
        failed = self.client.delete('/api/courses/course-1')
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed.get_json(), {'ok': False})

        delete_course.return_value = True
        succeeded = self.client.delete('/api/courses/course-1')
        self.assertEqual(succeeded.status_code, 200)
        self.assertEqual(succeeded.get_json(), {'ok': True})

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'list_course_records')
    def test_courses_get_preserves_legacy_courses_and_adds_sync(
            self, list_course_records, _authenticate):
        list_course_records.return_value = [
            firestore_client.CourseRecord(
                course_id='active', revision=3, deleted=False,
                updated_at='2026-07-18T10:00:00Z',
                course_json='{"id":"active","title":"Active"}',
            ),
            firestore_client.CourseRecord(
                course_id='deleted', revision=4, deleted=True,
                updated_at='2026-07-18T10:01:00Z', course_json=None,
            ),
        ]

        response = self.client.get('/api/courses')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'courses': [{'id': 'active', 'title': 'Active'}],
            'sync': [
                {'id': 'active', 'revision': 3, 'deleted': False,
                 'updatedAt': '2026-07-18T10:00:00Z'},
                {'id': 'deleted', 'revision': 4, 'deleted': True,
                 'updatedAt': '2026-07-18T10:01:00Z'},
            ],
        })

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'list_course_records')
    def test_courses_get_skips_json_with_mismatched_or_unsafe_id(
            self, list_course_records, _authenticate):
        list_course_records.return_value = [
            firestore_client.CourseRecord(
                course_id='document-id', revision=1, deleted=False,
                updated_at=None,
                course_json='{"id":"../escape","title":"Unsafe"}',
            ),
        ]

        response = self.client.get('/api/courses')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['courses'], [])
        self.assertEqual(response.get_json()['sync'][0]['id'], 'document-id')

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'list_course_records', return_value=None)
    def test_courses_get_returns_503_instead_of_empty_library_on_failure(
            self, _list_course_records, _authenticate):
        response = self.client.get('/api/courses')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {'error': 'Course sync unavailable'})

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'save_course')
    def test_courses_post_returns_revision_and_conflict(self, save_course, _authenticate):
        save_course.return_value = firestore_client.CourseMutationResult(
            'success', revision=7)
        saved = self.client.post(
            '/api/courses',
            json={'course': {'id': 'course-1'}, 'expectedRevision': 6},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json(), {'saved': True, 'revision': 7})
        save_course.assert_called_once_with(
            'uid-1', 'course-1', '{"id": "course-1"}', 6)

        save_course.return_value = firestore_client.CourseMutationResult('conflict')
        conflicted = self.client.post(
            '/api/courses', json={'course': {'id': 'course-1'}, 'expectedRevision': 6})
        self.assertEqual(conflicted.status_code, 409)
        self.assertEqual(conflicted.get_json(), {'saved': False, 'conflict': True})

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'delete_course')
    def test_course_delete_accepts_revision_from_query_or_body(
            self, delete_course, _authenticate):
        delete_course.return_value = firestore_client.CourseMutationResult(
            'success', revision=8)
        query = self.client.delete('/api/courses/course-1?expectedRevision=6')
        self.assertEqual(query.status_code, 200)
        self.assertEqual(query.get_json(), {'ok': True, 'revision': 8})
        delete_course.assert_called_once_with('uid-1', 'course-1', 6)

        body = self.client.delete('/api/courses/course-1', json={'expectedRevision': 5})
        self.assertEqual(body.status_code, 200)
        delete_course.assert_called_with('uid-1', 'course-1', 5)

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_course_mutations_reject_invalid_expected_revision(self, _authenticate):
        post = self.client.post(
            '/api/courses', json={'course': {'id': 'course-1'}, 'expectedRevision': -1})
        delete = self.client.delete('/api/courses/course-1?expectedRevision=1.5')
        self.assertEqual(post.status_code, 400)
        self.assertEqual(delete.status_code, 400)

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_course_post_rejects_path_unsafe_id(self, _authenticate):
        response = self.client.post(
            '/api/courses', json={'course': {'id': '../other-user'}})
        self.assertEqual(response.status_code, 400)

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_course_mutations_reject_non_object_json(self, _authenticate):
        post = self.client.post('/api/courses', json=[])
        delete = self.client.delete('/api/courses/course-1', json=[])
        self.assertEqual(post.status_code, 400)
        self.assertEqual(delete.status_code, 400)

    def test_cors_allows_course_delete(self):
        response = self.client.options('/api/courses/course-1')
        self.assertIn(
            'DELETE', response.headers.get('Access-Control-Allow-Methods', ''))


class FirestoreDeliveryResultTest(unittest.TestCase):
    @staticmethod
    def _document(
            course_id='course', *, revision=None, deleted=None,
            course_json='{"id":"course"}', update_time='2026-07-18T09:00:00Z'):
        fields = {}
        if course_json is not None:
            fields['json'] = {'stringValue': course_json}
        if revision is not None:
            fields['revision'] = {'integerValue': str(revision)}
        if deleted is not None:
            fields['deleted'] = {'booleanValue': deleted}
        return {
            'name': (
                'projects/project/databases/(default)/documents/users/uid/'
                f'courses/{course_id}'),
            'updateTime': update_time,
            'fields': fields,
        }

    @staticmethod
    def _response(status_code, body=None):
        response = Mock(status_code=status_code)
        response.json.return_value = body if body is not None else {}
        return response

    def test_delete_course_returns_false_on_transport_failure(self):
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token',
                             return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             side_effect=RuntimeError('offline')):
            self.assertFalse(firestore_client.delete_course('uid', 'course'))

    def test_delete_missing_course_creates_tombstone_with_exists_precondition(self):
        missing = self._response(404)
        patched = self._response(200)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token',
                             return_value='token'), \
                patch.object(firestore_client.requests, 'get', return_value=missing), \
                patch.object(firestore_client.requests, 'patch', return_value=patched) as patch_request:
            result = firestore_client.delete_course('uid', 'course')
        self.assertTrue(result)
        self.assertEqual(result.revision, 1)
        self.assertIn(('currentDocument.exists', 'false'), patch_request.call_args.kwargs['params'])
        self.assertTrue(patch_request.call_args.kwargs['json']['fields']['deleted']['booleanValue'])

    def test_save_existing_uses_firestore_update_time_precondition(self):
        document = self._document(revision=4, deleted=False)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=self._response(200, document)), \
                patch.object(firestore_client.requests, 'patch',
                             return_value=self._response(200)) as patch_request:
            result = firestore_client.save_course(
                'uid', 'course', '{"id":"course","title":"new"}', 4)
        self.assertTrue(result)
        self.assertEqual(result.revision, 5)
        params = patch_request.call_args.kwargs['params']
        self.assertIn(('currentDocument.updateTime', '2026-07-18T09:00:00Z'), params)
        self.assertEqual(
            patch_request.call_args.kwargs['json']['fields']['revision']['integerValue'], '5')

    def test_save_missing_course_uses_exists_false_precondition(self):
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=self._response(404)), \
                patch.object(firestore_client.requests, 'patch',
                             return_value=self._response(200)) as patch_request:
            result = firestore_client.save_course('uid', 'course', '{}', 0)
        self.assertTrue(result)
        self.assertEqual(result.revision, 1)
        self.assertIn(('currentDocument.exists', 'false'), patch_request.call_args.kwargs['params'])

    def test_save_rejects_stale_revision_before_write(self):
        document = self._document(revision=4, deleted=False)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=self._response(200, document)), \
                patch.object(firestore_client.requests, 'patch') as patch_request:
            result = firestore_client.save_course('uid', 'course', '{}', 3)
        self.assertEqual(result.status, 'conflict')
        patch_request.assert_not_called()

    def test_legacy_active_course_is_saved_as_revision_one(self):
        legacy = self._document(revision=None, deleted=None)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=self._response(200, legacy)), \
                patch.object(firestore_client.requests, 'patch',
                             return_value=self._response(200)):
            result = firestore_client.save_course('uid', 'course', '{}')
        self.assertTrue(result)
        self.assertEqual(result.revision, 1)

    def test_tombstone_never_allows_legacy_upload_to_resurrect_course(self):
        tombstone = self._document(revision=5, deleted=True, course_json=None)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=self._response(200, tombstone)), \
                patch.object(firestore_client.requests, 'patch') as patch_request:
            result = firestore_client.save_course('uid', 'course', '{}')
        self.assertEqual(result.status, 'conflict')
        patch_request.assert_not_called()

    def test_delete_wins_even_when_expected_revision_is_stale(self):
        active = self._document(revision=9, deleted=False)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=self._response(200, active)), \
                patch.object(firestore_client.requests, 'patch',
                             return_value=self._response(200)) as patch_request:
            result = firestore_client.delete_course('uid', 'course', expected_revision=1)
        self.assertTrue(result)
        self.assertEqual(result.revision, 10)
        params = patch_request.call_args.kwargs['params']
        self.assertIn(('currentDocument.updateTime', '2026-07-18T09:00:00Z'), params)
        self.assertIn(('updateMask.fieldPaths', 'json'), params)

    def test_delete_tombstone_is_idempotent_without_extra_write(self):
        tombstone = self._document(revision=10, deleted=True, course_json=None)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=self._response(200, tombstone)), \
                patch.object(firestore_client.requests, 'patch') as patch_request:
            result = firestore_client.delete_course('uid', 'course', expected_revision=1)
        self.assertTrue(result)
        self.assertEqual(result.revision, 10)
        patch_request.assert_not_called()

    def test_list_records_contains_tombstones_but_legacy_list_hides_them(self):
        active = self._document('active', revision=None, deleted=None)
        deleted = self._document('deleted', revision=3, deleted=True, course_json=None)
        response = self._response(200, {'documents': [active, deleted]})
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get', return_value=response):
            records = firestore_client.list_course_records('uid')
        self.assertEqual([(r.course_id, r.revision, r.deleted) for r in records], [
            ('active', 0, False), ('deleted', 3, True)])
        with patch.object(firestore_client, 'list_course_records', return_value=records):
            self.assertEqual(firestore_client.list_courses('uid'), ['{"id":"course"}'])

    def test_list_records_returns_none_on_firestore_failure(self):
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=self._response(503)):
            self.assertIsNone(firestore_client.list_course_records('uid'))

    def test_account_deletion_visits_courses_collection_with_tombstones(self):
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token', return_value='token'), \
                patch.object(firestore_client, '_delete_all_in_subcollection') as clear_collection, \
                patch.object(firestore_client.requests, 'delete',
                             return_value=self._response(200)):
            self.assertTrue(firestore_client.delete_user_data('uid'))
        self.assertEqual(
            [call.args[1] for call in clear_collection.call_args_list],
            ['devices', 'courses', 'progress'])

    def test_force_register_returns_false_when_registration_patch_fails(self):
        listing = Mock(status_code=200)
        listing.json.return_value = {'documents': []}
        registration = Mock(status_code=503)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token',
                             return_value='token'), \
                patch.object(firestore_client.requests, 'get',
                             return_value=listing), \
                patch.object(firestore_client.requests, 'patch',
                             return_value=registration):
            self.assertFalse(
                firestore_client.force_register_device(
                    'uid', 'device', 'Phone'))


if __name__ == '__main__':
    unittest.main()
