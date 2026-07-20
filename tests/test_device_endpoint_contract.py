import unittest
from unittest.mock import patch

import main


class DeviceEndpointIdValidationTest(unittest.TestCase):
    """Regression for a path-injection gap: device_id used to reach
    firestore_client with no format check, unlike course_id (see
    main._valid_course_id). Since it is concatenated straight into a
    Firestore REST document path (`users/{uid}/devices/{deviceId}`), a
    value containing '/' or '..' segments could target a Firestore path
    outside the caller's own uid subtree."""

    def setUp(self):
        self.client = main.app.test_client()

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'check_and_register_device')
    def test_device_rejects_path_traversal_id(
            self, check_and_register, _authenticate):
        response = self.client.post(
            '/api/device',
            json={
                'deviceId': '../../other-uid/devices/evil',
                'deviceName': 'Phone',
            },
        )
        self.assertEqual(response.status_code, 400)
        check_and_register.assert_not_called()

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'check_and_register_device')
    def test_device_accepts_a_real_uuid(
            self, check_and_register, _authenticate):
        check_and_register.return_value = True
        response = self.client.post(
            '/api/device',
            json={
                'deviceId': '550e8400-e29b-41d4-a716-446655440000',
                'deviceName': 'Phone',
            },
        )
        self.assertEqual(response.status_code, 200)
        check_and_register.assert_called_once_with(
            'uid-1', '550e8400-e29b-41d4-a716-446655440000', 'Phone')

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'force_register_device')
    def test_device_force_rejects_path_traversal_id(
            self, force_register, _authenticate):
        response = self.client.post(
            '/api/device/force',
            json={
                'deviceId': '../../other-uid/devices/evil',
                'deviceName': 'Phone',
            },
        )
        self.assertEqual(response.status_code, 400)
        force_register.assert_not_called()

    @patch.object(main, '_authenticate', return_value='uid-1')
    @patch.object(main.firestore_client, 'force_register_device')
    def test_device_force_accepts_a_real_uuid(
            self, force_register, _authenticate):
        force_register.return_value = True
        response = self.client.post(
            '/api/device/force',
            json={
                'deviceId': '550e8400-e29b-41d4-a716-446655440000',
                'deviceName': 'Phone',
            },
        )
        self.assertEqual(response.status_code, 200)
        force_register.assert_called_once_with(
            'uid-1', '550e8400-e29b-41d4-a716-446655440000', 'Phone')


if __name__ == '__main__':
    unittest.main()
