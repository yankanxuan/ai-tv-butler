"""b8: restarting logcat alone must not pretend to reconnect ADB."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from adbtv.voice import VoiceListener


class ListenerReconnectTests(unittest.TestCase):
    def listener(self,auto_connect=True):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        cfg={'adb':{'serial':'test-tv:5555','auto_connect':auto_connect},
             'execution':{'state_dir':temp.name},
             'voice':{'listener':{'state_file':str(Path(temp.name)/'voice.json')}}}
        return VoiceListener(cfg,lambda u:None)

    def test_reconnect_checks_device_before_launching_logcat(self):
        listener=self.listener()
        with patch('adbtv.adb.AdbClient') as cls:
            cls.return_value.ensure.return_value=None
            self.assertTrue(listener._ensure_adb())
            cls.return_value.ensure.assert_called_once()

    def test_failed_connection_is_reported_without_claiming_connected(self):
        listener=self.listener()
        from adbtv.adb import AdbError
        events=[];listener.on_event=events.append
        with patch('adbtv.adb.AdbClient') as cls:
            cls.return_value.ensure.side_effect=AdbError('private target could not connect')
            self.assertFalse(listener._ensure_adb())
        self.assertTrue(any(event['kind']=='adb_reconnect_failed' for event in events))
        self.assertNotIn('private target',str(events))

    def test_client_is_reused_after_disconnect(self):
        listener=self.listener()
        with patch('adbtv.adb.AdbClient') as cls:
            self.assertTrue(listener._ensure_adb())
            self.assertTrue(listener._ensure_adb())
            cls.assert_called_once()
            self.assertEqual(cls.return_value.ensure.call_count,2)

    def test_custom_nonconnecting_log_source_keeps_legacy_behavior(self):
        listener=self.listener(False)
        with patch('adbtv.adb.AdbClient') as cls:
            self.assertTrue(listener._ensure_adb())
            cls.assert_not_called()
