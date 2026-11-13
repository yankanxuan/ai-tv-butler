"""Packaging security regressions; fixtures never use live operator data."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class PackageSecurityTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("package_v343", ROOT / "scripts/package.py")
        self.package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.package)
        self.temp = tempfile.TemporaryDirectory(prefix="package_v343_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.root_patch = patch.object(self.package, "ROOT", self.source)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.write("adbtv/__init__.py", '__version__ = "3.4.3"\n')

    def write(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_public_excludes_private_env_variants_at_any_depth(self):
        private = (".env", ".env.local", ".env.production", ".env.example.local",
                   "tools/.env", "tools/.env.test", "tools/.envrc", ".envrc",
                   "tools/.env.production.example")
        examples = (".env.example", "tools/.env.example")
        for relative in private + examples:
            self.write(relative, "# fixture\n")
        names = {relative for _, relative in self.package.iter_files("public")}
        self.assertTrue(set(private).isdisjoint(names), names)
        self.assertTrue(set(examples).issubset(names))
        private_names = {relative for _, relative in self.package.iter_files("private")}
        self.assertTrue(set(private + examples).issubset(private_names))

    def test_audit_rejects_env_even_when_file_list_is_injected(self):
        path = self.write(".env.local", "# no content secret needed\n")
        self.assertTrue(self.package.audit([(path, ".env.local")], "public"))

    def test_generic_credentials_are_detected_without_fixed_token_prefix(self):
        values = ("console_token: ", "API_KEY=", '"password": ', "client_secret = ",
                  "access_token: ", "refresh_token=", "AWS_SECRET_ACCESS_KEY=", "export API_KEY=")
        for index, key in enumerate(values):
            with self.subTest(key=key):
                path = self.write(f"credential-{index}.txt", key + '"arbitrary-credential-928!"\n')
                self.assertTrue(self.package.audit([(path, path.name)], "public"))

    def test_templates_and_code_references_are_allowed(self):
        lines = ["api_key: \"\"", "password: null", "token: ${CONSOLE_TOKEN}",
                 "api_key: YOUR_API_KEY", "token: <your-token>",
                 'token = os.environ.get("CONSOLE_TOKEN")', 'token = cfg.get("token", "")']
        path = self.write("example.txt", "\n".join(lines))
        self.assertEqual(self.package.audit([(path, path.name)], "public"), [])

    def test_home_paths_rejected_for_linux_macos_and_windows(self):
        paths = ("/" + "home/alice/project", "/" + "Users/alice/project",
                 "C:" + "\\Users\\alice\\project")
        for value in paths:
            with self.subTest(value=value):
                path = self.write("guide.md", value)
                self.assertTrue(self.package.audit([(path, path.name)], "public"))

    def test_large_files_are_fully_audited(self):
        path = self.write("large.txt", "x" * 2_000_001 + "\nconsole_" + "token: leaked-value\n")
        self.assertTrue(self.package.audit([(path, path.name)], "public"))

    def test_build_audits_bytes_actually_written_to_archive(self):
        self.write("README.md", "clean source\n")
        original_add = tarfile.TarFile.add

        def replaced_add(archive, name, arcname=None, **kwargs):
            if Path(name).name == "README.md":
                data = ("console_" + "token: leaked-after-source-audit\n").encode()
                member = tarfile.TarInfo(arcname)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            else:
                original_add(archive, name, arcname=arcname, **kwargs)

        out = self.root / "public.tar.gz"
        with patch.object(tarfile.TarFile, "add", replaced_add):
            with self.assertRaises(SystemExit):
                self.package.build("public", out)
        self.assertFalse(out.exists(), "Rejected public artifact must not be published")

    def test_fixture_exception_is_limited_to_known_python_test_values(self):
        path = self.write("tests/test_fixture.py", "token: " + '"arbitrary-credential-928!"\n')
        self.assertTrue(self.package.audit([(path, "tests/test_fixture.py")], "public"))
        path.write_text("token: " + '"secret-token"\n')
        self.assertEqual(self.package.audit([(path, "tests/test_fixture.py")], "public"), [])
        self.assertTrue(self.package.audit([(path, "config.example.yaml")], "public"))

    def test_archive_rejects_forbidden_members_and_links(self):
        for relative, kind in ((".env.local", tarfile.REGTYPE),
                               ("config.yaml", tarfile.REGTYPE),
                               ("docs/link", tarfile.SYMTYPE),
                               ("../escape", tarfile.REGTYPE)):
            with self.subTest(relative=relative):
                out = self.root / "injected.tar.gz"
                with tarfile.open(out, "w:gz") as archive:
                    member = tarfile.TarInfo("ai-tv-butler-3.4.3/" + relative)
                    member.type = kind
                    member.linkname = "outside" if kind == tarfile.SYMTYPE else ""
                    archive.addfile(member, io.BytesIO(b""))
                self.assertTrue(self.package.audit_archive(out, "public"))

    def test_failed_build_preserves_existing_artifact(self):
        out = self.root / "previous.tar.gz"
        out.write_bytes(b"existing artifact")
        with patch.object(self.package, "audit_archive", return_value=["injected failure"]):
            with self.assertRaises(SystemExit):
                self.package.build("public", out)
        self.assertEqual(out.read_bytes(), b"existing artifact")
        self.assertEqual(list(self.root.glob(".package-*")), [])

    def test_public_archive_is_audited_and_private_api_is_preserved(self):
        self.write(".env", "# private fixture\n")
        self.write("config.yaml", "# private fixture\n")
        for mode in ("public", "private"):
            out = self.root / (mode + ".tar.gz")
            self.assertEqual(self.package.build(mode, out), out)
            with tarfile.open(out) as archive:
                names = archive.getnames()
            self.assertEqual(any(name.endswith("/.env") for name in names), mode == "private")
            self.assertEqual(any(name.endswith("/config.yaml") for name in names), mode == "private")


if __name__ == "__main__":
    unittest.main()
