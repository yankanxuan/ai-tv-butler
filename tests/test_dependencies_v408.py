"""Runtime version/hash locks must match every production install path."""
import hashlib
import re
from pathlib import Path
import unittest
import tomllib

ROOT=Path(__file__).resolve().parents[1]


def lock_versions(name):
    content=(ROOT/name).read_text()
    return dict(re.findall(r'^([A-Za-z0-9_-]+)==([^\s\\]+)',content,re.M))


class RuntimeLockTests(unittest.TestCase):
    def test_direct_metadata_matches_hash_locked_runtime(self):
        project=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']
        versions=lock_versions('requirements-runtime.lock')
        self.assertEqual(project['dependencies'],['PyYAML=='+versions['PyYAML']])
        self.assertEqual(project['optional-dependencies']['schemas'],['pydantic=='+versions['pydantic']])
        self.assertIn('-r requirements-runtime.lock',(ROOT/'requirements.txt').read_text())

    def test_locks_contain_only_exact_versions_with_sha256(self):
        for name in ('requirements-runtime.lock','requirements-build.lock','requirements-test.lock'):
            text=(ROOT/name).read_text()
            self.assertNotRegex(text,r'>=|~=')
            for block in text.split('\n\n'):
                if re.search(r'^[A-Za-z0-9_-]+==',block,re.M):
                    self.assertRegex(block,r'--hash=sha256:[0-9a-f]{64}')

    def test_docker_uses_same_lock_and_does_not_install_test_dependencies(self):
        docker=(ROOT/'Dockerfile').read_text()
        self.assertIn('--require-hashes -r requirements-runtime.lock',docker)
        self.assertIn('--require-hashes -r requirements-build.lock',docker)
        self.assertIn('--no-deps --no-build-isolation ".[schemas]"',docker)
        self.assertNotIn('requirements-test',docker)
        self.assertNotIn('sherpa-onnx',docker)
        self.assertNotIn('torch',docker)
        self.assertIn('ARG INSTALL_PIPER=0',docker)

    def test_test_dependencies_are_pinned_separately(self):
        project=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']
        versions=lock_versions('requirements-test.lock')
        self.assertEqual(project['optional-dependencies']['e2e'],['playwright=='+versions['playwright']])
        self.assertIn('greenlet',versions)
        self.assertNotIn('playwright',lock_versions('requirements-runtime.lock'))

    def test_external_script_is_packaged_for_installed_docker_entry(self):
        package=tomllib.loads((ROOT/'pyproject.toml').read_text())['tool']['setuptools']['package-data']['adbtv']
        self.assertIn('web/*.js',package)
        self.assertIn('web/vendor/*',package)

    def test_alpine_files_and_exact_license_match_recorded_hashes(self):
        vendor=ROOT/'adbtv/web/vendor'
        records={name:sha for sha,name in (line.split(None,1) for line in (vendor/'SHA256SUMS').read_text().splitlines())}
        for name,sha in records.items():
            self.assertEqual(hashlib.sha256((vendor/name).read_bytes()).hexdigest(),sha,name)
        self.assertEqual(records['alpine-csp-3.14.9.min.js'],'820bc9503874024057da244dc30c21301548329eb9a00e7937e91c4d3908b273')
        self.assertEqual(records['alpine-3.14.9.LICENSE.txt'],'5f05c4925dd3edf049a70b4b403ac0dd43b20f72501d9359911ddd25d5ac657d')
