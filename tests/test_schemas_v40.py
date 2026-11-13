"""Pydantic-backed contracts, plus parity with the builtin fallback.

Run with a Python that has pydantic (the project venv) for the pydantic branch;
the stdlib branch runs everywhere. Both must accept and reject the same shapes.
"""
import unittest

from adbtv import schemas


def valid_decision():
    return {"route": "agent", "intent": "play", "target": "三体",
            "parameters": {"title": "三体"}, "confidence": 0.9,
            "reply": "", "risk": "low"}


class ContractParityTests(unittest.TestCase):
    def test_backend_is_reported(self):
        info = schemas.availability()
        self.assertIn(info["backend"], ("pydantic", "builtin"))

    def test_decision_accepts_valid_shape(self):
        self.assertEqual(schemas.validate_voice_decision(valid_decision())["intent"], "play")

    def test_decision_rejects_bad_shapes(self):
        bad = [
            {**valid_decision(), "route": "shell"},
            {**valid_decision(), "confidence": True},
            {**valid_decision(), "confidence": 2.0},
            {**valid_decision(), "confidence": float("nan")},
            {k: v for k, v in valid_decision().items() if k != "reply"},
            {**valid_decision(), "extra": 1},
        ]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(schemas.SchemaError):
                schemas.validate_voice_decision(item)

    def test_pending_rejects_shell_and_bad_nonce(self):
        good = {"nonce": "a" * 32, "command": "播放《三体》第1季第一集",
                "intent": "play", "created": 1.0, "expires": 61.0}
        self.assertEqual(schemas.validate_pending(good)["intent"], "play")
        for bad in ({**good, "nonce": "xyz"},
                    {**good, "command": "adb shell rm -rf /"},
                    {**good, "command": "播放; rm -rf /"},
                    {**good, "expires": 0}):
            with self.subTest(bad=bad), self.assertRaises(schemas.SchemaError):
                schemas.validate_pending(bad)

    def test_job_and_response_contracts(self):
        job = {"id": "b" * 32, "state": "READY", "audio_url": "/api/voice/audio/x.wav",
               "duration_ms": 900}
        self.assertEqual(schemas.validate_job(job)["state"], "READY")
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_job({"id": "not-hex", "state": "READY"})
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_job({"id": "b" * 32, "state": "PLAYING"})

        response = {"status": "SUCCESS", "route": "agent", "reply": "已完成。",
                    "candidates": [{"id": "c1", "title": "三体"}]}
        self.assertEqual(schemas.validate_voice_response(response)["status"], "SUCCESS")
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_voice_response({"status": "SUCCESS",
                                             "candidates": [{"id": "../x", "title": "t"}]})


class PydanticBackendTests(unittest.TestCase):
    def setUp(self):
        if not schemas.PYDANTIC:
            self.skipTest("pydantic 未安装：本机使用内置校验（记录的 skip 不是通过）")

    def test_pydantic_models_reject_strict_type_coercion(self):
        # strict=True: a string where a float is required must not be coerced.
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_voice_decision({**valid_decision(), "confidence": "0.9"})

    def test_pydantic_job_ignores_extra_but_types_known_fields(self):
        schemas.validate_job({"id": "c" * 32, "state": "QUEUED", "extra": object()})
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_job({"id": "c" * 32, "state": "QUEUED", "duration_ms": "long"})


if __name__ == "__main__":
    unittest.main()