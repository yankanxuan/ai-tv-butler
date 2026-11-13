"""Acceptance recorder privacy and consent. Not real ASR acceptance."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


def recorder():
    spec=importlib.util.spec_from_file_location('asr_b8_test',ROOT/'scripts/asr_acceptance_b8.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


class RecorderPrivacyTests(unittest.TestCase):
    def test_public_export_excludes_raw_transcript_and_arbitrary_fields(self):
        module=recorder()
        raw={'index':1,'hash':'abc','chars':20,'asr_text':'私人的完整语音',
             'route':'agent','intent':'open','status':'SUCCESS','raw_log':'private',
             'tv':{'state':'PLAYING','package':'com.trim.tv','title':'private-film'}}
        public=module.redacted(raw)
        self.assertNotIn('私人的完整语音',json.dumps(public,ensure_ascii=False))
        self.assertNotIn('raw_log',public)
        self.assertNotIn('title',public['tv'])
        self.assertEqual(public['status'],'SUCCESS')

    def test_capture_requires_explicit_consent_before_any_device_or_file_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'report.json'
            result=subprocess.run([sys.executable,str(ROOT/'scripts/asr_acceptance_b8.py'),
                                   '--report',str(report)],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,2)
            self.assertIn('consent',result.stderr)
            self.assertFalse(report.exists())
            self.assertEqual(result.stdout,'')

    def test_route_matching_ignores_old_or_malformed_frames(self):
        module=recorder()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'voice_events.jsonl'
            path.write_text('not-json\n'+json.dumps({'at':1,'hash':'old'})+'\n'+
                            json.dumps({'at':100,'hash':'fresh','status':'SUCCESS'})+'\n')
            self.assertEqual([e['hash'] for e in module.route_events(path,50)],['fresh'])

    def test_private_transcript_file_permissions(self):
        module=recorder()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'private.json'
            module.private_json(path,{'asr_text':'仅本地授权测试'})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
