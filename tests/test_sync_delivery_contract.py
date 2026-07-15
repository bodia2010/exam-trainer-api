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


class FirestoreDeliveryResultTest(unittest.TestCase):
    def test_delete_course_returns_false_on_transport_failure(self):
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token',
                             return_value='token'), \
                patch.object(firestore_client.requests, 'delete',
                             side_effect=RuntimeError('offline')):
            self.assertFalse(firestore_client.delete_course('uid', 'course'))

    def test_delete_course_treats_missing_document_as_idempotent_success(self):
        response = Mock(status_code=404)
        with patch.object(firestore_client, '_credentials', object()), \
                patch.object(firestore_client, '_access_token',
                             return_value='token'), \
                patch.object(firestore_client.requests, 'delete',
                             return_value=response):
            self.assertTrue(firestore_client.delete_course('uid', 'course'))

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
