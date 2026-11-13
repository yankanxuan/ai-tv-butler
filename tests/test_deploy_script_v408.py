"""Deployment helper safety checks. These mocks are NOT Docker acceptance."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def module():
    spec=importlib.util.spec_from_file_location('deploy_b8_test',ROOT/'scripts/deploy_b8.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


class DeploySafetyTests(unittest.TestCase):
    def test_permission_failure_happens_before_any_backup_or_stop(self):
        deploy=module()
        with tempfile.TemporaryDirectory() as tmp,patch.object(deploy,'run',side_effect=RuntimeError('permission denied')) as run:
            backup=Path(tmp)/'not-created'
            with self.assertRaisesRegex(RuntimeError,'permission denied'):
                deploy.deploy('ai-tv-butler',backup)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][:2],['docker','info'])
            self.assertFalse(backup.exists())

    def test_key_backup_check_preserves_binary_whitespace(self):
        deploy=module()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);raw=b'\n'+b'x'*30+b' '
            (p/'media_key').write_bytes(raw)
            self.assertEqual(deploy.key_digest(p),__import__('hashlib').sha256(raw).hexdigest())
            self.assertEqual((p/'media_key').read_bytes(),raw)

    def test_corrupt_key_is_not_regenerated(self):
        deploy=module()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'media_key').write_bytes(b'broken')
            with self.assertRaisesRegex(RuntimeError,'invalid length'):deploy.key_digest(p)
            self.assertEqual((p/'media_key').read_bytes(),b'broken')

    def test_untrusted_rollback_tag_rejected_before_docker(self):
        deploy=module()
        with tempfile.TemporaryDirectory() as tmp,patch.object(deploy,'run') as run:
            manifest=Path(tmp)/'manifest.json'
            manifest.write_text(json.dumps({'project':'ai-tv-butler','images':{service:{'rollback_tag':'untrusted'} for service in deploy.SERVICES}}))
            with self.assertRaisesRegex(ValueError,'Untrusted'):deploy.rollback('ai-tv-butler',manifest)
            run.assert_not_called()

    def test_rollback_uses_recorded_image_tags_with_no_build(self):
        deploy=module()
        image_id='sha256:'+'a'*64
        evidence={'health':{s:'healthy' for s in deploy.SERVICES},
                  'running':{s:True for s in deploy.SERVICES},
                  'versions':{s:'ai-tv 4.0.0b7' for s in deploy.SERVICES},
                  'image_ids':{s:image_id for s in deploy.SERVICES}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(deploy,'run') as run, \
                patch.object(deploy,'verify_services',return_value=evidence) as verify:
            manifest=Path(tmp)/'manifest.json'
            images={service:{'rollback_tag':'ai-tv-butler:pre-b8-20261112T070000-'+service,
                             'id':image_id} for service in deploy.SERVICES}
            manifest.write_text(json.dumps({'project':'ai-tv-butler','images':images}))
            report=deploy.rollback('ai-tv-butler',manifest)
            command=run.call_args.args[0]
            self.assertIn('--no-build',command)
            verify.assert_called_once()
            self.assertEqual(report['status'],'ROLLBACK_VERIFIED')
            self.assertEqual(report['restored_images'],{s:image_id for s in deploy.SERVICES})
            override=json.loads((manifest.parent/'rollback.compose.json').read_text())
            self.assertEqual(set(override['services']),set(deploy.SERVICES))
            self.assertTrue(all(service['build'] is None for service in override['services'].values()))

    def test_rollback_fails_when_containers_are_not_actually_healthy(self):
        deploy=module()
        with tempfile.TemporaryDirectory() as tmp,patch.object(deploy,'run'), \
                patch.object(deploy,'verify_services',side_effect=RuntimeError('Services not healthy')):
            manifest=Path(tmp)/'manifest.json'
            images={s:{'rollback_tag':'ai-tv-butler:pre-b8-20261112T070000-'+s,'id':'sha256:'+'b'*64}
                    for s in deploy.SERVICES}
            manifest.write_text(json.dumps({'project':'ai-tv-butler','images':images}))
            with self.assertRaisesRegex(RuntimeError,'not healthy'):
                deploy.rollback('ai-tv-butler',manifest)
            self.assertFalse((Path(tmp)/'rollback-verification.json').exists())

    def test_rollback_fails_when_images_were_not_restored(self):
        deploy=module()
        wrong='sha256:'+'c'*64
        evidence={'health':{s:'healthy' for s in deploy.SERVICES},'running':{s:True for s in deploy.SERVICES},
                  'versions':{s:'ai-tv 4.0.0b7' for s in deploy.SERVICES},
                  'image_ids':{s:wrong for s in deploy.SERVICES}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(deploy,'run'), \
                patch.object(deploy,'verify_services',return_value=evidence):
            manifest=Path(tmp)/'manifest.json'
            images={s:{'rollback_tag':'ai-tv-butler:pre-b8-20261112T070000-'+s,'id':'sha256:'+'d'*64}
                    for s in deploy.SERVICES}
            manifest.write_text(json.dumps({'project':'ai-tv-butler','images':images}))
            with self.assertRaisesRegex(RuntimeError,'did not restore'):
                deploy.rollback('ai-tv-butler',manifest)
