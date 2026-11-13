"""Private JSON-only SQLite memory; definitions are data and are never executed.

One connection per operation, WAL, bounded lock waits, atomic migrations and
explicit schema checks make a MemoryStore safe to share across worker threads
and processes. Errors propagate to callers; no failed write is silently ignored.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
import uuid

SCHEMA_VERSION = 2
REDACTED = "[REDACTED]"
OMITTED = "[OMITTED]"


class SchemaVersionError(RuntimeError):
    pass


class RecordTooLargeError(ValueError):
    pass


# Matching normalized keys also catches api-key/apiKey and nested credentials.
_SECRET = ("password", "passwd", "secret", "token", "authorization", "apikey",
           "accesskey", "privatekey", "cookie", "credential", "captcha", "验证码",
           "校验码", "动态码", "密码", "密钥", "账号", "account", "username",
           "email", "phone", "手机号", "身份证")
_SECRET_EXACT = {"pwd", "otp", "pin", "auth", "login", "user", "用户名"}
_RAW = {"xml", "uixml", "rawui", "uidump", "elements", "texts", "observation",
        "observations", "screenstate", "screenshot", "screenshots", "screenshotpath",
        "image", "images", "imageurl", "imagebytes", "screenshotbytes", "base64",
        "rawresponse", "rawscreen", "rawxml", "focused", "hierarchy", "uiautomator"}
_SENSITIVE_TEXT = re.compile(
    r"(?:\b(?:password|passwd|secret|token|authorization|api[_-]?key|access[_-]?key|"
    r"captcha|otp|account|username|email|phone)\b\s*(?:[:=]|\bis\b)?\s*\S+|"
    r"(?:验证码|校验码|动态码|密码|密钥|账号|手机号)\s*[:：=是为]?\s*\S+|"
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|https?://[^/\s:@]+:[^/\s@]+@|"
    r"(?:输入|填入)\D{0,6}\d{4,8}(?!\d))", re.I)


def _normalized_key(key: str) -> str:
    return re.sub(r"[\W_]", "", key).lower()


def _safe(value: Any, *, declarative=False, depth=0, budget=None) -> Any:
    """Recursively redact before serialization, SQL, hashing, or identifiers.

    Raw observations/images and free-form credential messages are omitted.
    Declarative skill precondition texts are rules, not captured raw UI, and may
    be retained. Input-text tool values are always treated as private.
    """
    budget = budget if budget is not None else [20000]
    budget[0] -= 1
    if depth > 24 or budget[0] < 0:
        raise RecordTooLargeError("JSON nesting or item count exceeds memory limits")
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value
    if isinstance(value, str):
        if re.search(r"<(?:\?xml|hierarchy|node)\b|data:image/", value, re.I):
            return OMITTED
        if _SENSITIVE_TEXT.search(value):
            return REDACTED
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return OMITTED
    if isinstance(value, dict):
        output = {}
        input_tool = value.get("tool", value.get("name")) in ("tv_input_text", "input_text")
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            normalized = _normalized_key(key)
            safe_key = _safe(key, depth=depth + 1, budget=budget)
            if normalized in _SECRET_EXACT or any(word in normalized for word in _SECRET):
                output[safe_key] = REDACTED
            elif (normalized in _RAW and not (declarative and normalized == "texts")) or normalized.startswith("screenshot"):
                output[safe_key] = OMITTED
            elif input_tool and key in ("args", "text", "value"):
                if isinstance(item, dict):
                    output[safe_key] = {k: REDACTED if k in ("text", "value") else
                                        _safe(v, declarative=declarative, depth=depth + 1, budget=budget)
                                        for k, v in item.items()}
                else:
                    output[safe_key] = REDACTED
            else:
                output[safe_key] = _safe(item, declarative=declarative, depth=depth + 1, budget=budget)
        return output
    if isinstance(value, (list, tuple)):
        return [_safe(item, declarative=declarative, depth=depth + 1, budget=budget) for item in value]
    raise TypeError(f"Memory accepts JSON data only, not {type(value).__name__}")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _numeric(value, default=0., *, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return default
    return int(value) if integer else float(value)


def _count(value) -> int:
    return len(value) if isinstance(value, (list, dict)) else _numeric(value, 0, integer=True)


def _validation_passed(report) -> bool:
    return isinstance(report, dict) and (report.get("status") == "SUCCESS" or
           any(report.get(key) is True for key in ("ok", "passed", "success")))


_MIGRATIONS = {
    1: [
        """CREATE TABLE tasks (
            id TEXT PRIMARY KEY, goal_json TEXT NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL,
            finished_at REAL, elapsed REAL NOT NULL, steps_json TEXT NOT NULL,
            step_timings_json TEXT NOT NULL, recoveries INTEGER NOT NULL,
            llm_calls INTEGER NOT NULL, task_json TEXT NOT NULL, result_json TEXT NOT NULL)""",
        """CREATE TABLE paths (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, package TEXT NOT NULL,
            steps_json TEXT NOT NULL, attempts INTEGER NOT NULL, successes INTEGER NOT NULL,
            failures INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
        """CREATE TABLE pages (
            package TEXT NOT NULL, fingerprint TEXT NOT NULL, features_json TEXT NOT NULL,
            visits INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(package,fingerprint))""",
        """CREATE TABLE recoveries (
            failure TEXT NOT NULL, strategy TEXT NOT NULL, attempts INTEGER NOT NULL,
            successes INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(failure,strategy))""",
        """CREATE TABLE preferences (
            key TEXT PRIMARY KEY, value_json TEXT NOT NULL, explicit INTEGER NOT NULL CHECK(explicit=1),
            updated_at REAL NOT NULL)""",
    ],
    2: [
        """CREATE TABLE skills (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL
            CHECK(status IN ('draft','validated','approved','disabled')),
            definition_json TEXT NOT NULL, source_task TEXT NOT NULL,
            validation_json TEXT NOT NULL, approval_json TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
        """CREATE TABLE skill_candidates (
            id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
            definition_json TEXT NOT NULL, source_task TEXT NOT NULL, created_at REAL NOT NULL)""",
        "CREATE INDEX tasks_created ON tasks(created_at DESC,id)",
        "CREATE INDEX paths_lookup ON paths(key,package)",
        "CREATE INDEX skills_status ON skills(status,updated_at DESC)",
    ],
}


class MemoryStore:
    def __init__(self, path, *, max_record_bytes=65536, busy_timeout=5000):
        if str(path) == ":memory:":
            raise ValueError("MemoryStore requires a filesystem path for WAL and persistence")
        self.path = str(Path(path).expanduser().resolve())
        self.max_record_bytes = int(max_record_bytes)
        self.busy_timeout = int(busy_timeout)
        if self.max_record_bytes < 256 or not 1 <= self.busy_timeout <= 60000:
            raise ValueError("Invalid memory size or SQLite timeout")
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout / 1000,
                                     isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _check_version(connection, *, exact=False):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(f"Database version {version} is newer than supported {SCHEMA_VERSION}")
        if exact and version != SCHEMA_VERSION:
            raise SchemaVersionError("Database schema changed; reopen MemoryStore to migrate")
        return version

    def _initialize(self):
        connection = self._connect()
        try:
            self._check_version(connection)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = self._check_version(connection)
            existing = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            if version < SCHEMA_VERSION and existing:
                destination = f"{self.path}.premigration-v{version}-{time.time_ns()}.bak"
                # Independent source sees the last committed schema, while this
                # write lease prevents any other writer racing the migration.
                source = self._connect()
                try:
                    self._backup_from(source, destination)
                finally:
                    source.close()
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[target]:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={target}")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._check_version(connection, exact=True)
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _check_size(self, *values):
        if sum(len(value.encode("utf-8")) for value in values) > self.max_record_bytes:
            raise RecordTooLargeError(f"Memory record exceeds {self.max_record_bytes} bytes")

    def _encode(self, value, *, declarative=False):
        sanitized = _safe(value, declarative=declarative)
        encoded = _json(sanitized)
        self._check_size(encoded)
        return sanitized, encoded

    def _label(self, value, *, empty=False):
        if not isinstance(value, str) or (not empty and not value) or len(value) > 512:
            raise ValueError("Identifier must be a string of 1..512 characters")
        value = _safe(value)
        if value in (REDACTED, OMITTED):
            raise ValueError("Sensitive data cannot be used as an identifier")
        return value

    def record_task(self, task: dict, result: dict) -> str:
        if not isinstance(task, dict) or not isinstance(result, dict):
            raise TypeError("Task and result must be JSON objects")
        task, task_json = self._encode(task)
        result, result_json = self._encode(result)
        identifier = self._label(task.get("id") or task.get("task_id") or result.get("task_id") or uuid.uuid4().hex)
        now = time.time()
        status = result.get("status") or task.get("state") or ("SUCCESS" if result.get("ok") is True else "UNKNOWN")
        status = self._label(str(getattr(status, "value", status))).upper()
        started = _numeric(task.get("started_at", task.get("started")), now)
        created = _numeric(task.get("created_at"), started)
        finished = None if status in ("QUEUED", "RUNNING", "PAUSED") else _numeric(task.get("finished_at"), now)
        elapsed = _numeric(result.get("elapsed", task.get("elapsed")), 0.)
        goal = task.get("goal", task.get("instruction", result.get("instruction", "")))
        steps = result.get("steps", result.get("log", task.get("steps", [])))
        timings = result.get("step_timings", result.get("timings", task.get("step_timings", {})))
        goal_json, steps_json, timings_json = _json(goal), _json(steps), _json(timings)
        recoveries = _count(result.get("recoveries", task.get("recoveries", 0)))
        llm_calls = _count(result.get("llm_calls", task.get("llm_calls", 0)))
        self._check_size(identifier, status, goal_json, steps_json, timings_json, task_json, result_json)
        with self._transaction() as connection:
            connection.execute("""INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET goal_json=excluded.goal_json,status=excluded.status,
                updated_at=excluded.updated_at,started_at=excluded.started_at,finished_at=excluded.finished_at,
                elapsed=excluded.elapsed,steps_json=excluded.steps_json,step_timings_json=excluded.step_timings_json,
                recoveries=excluded.recoveries,llm_calls=excluded.llm_calls,
                task_json=excluded.task_json,result_json=excluded.result_json""",
                (identifier, goal_json, status, created, now, started, finished, elapsed,
                 steps_json, timings_json, recoveries, llm_calls, task_json, result_json))
        return identifier

    @staticmethod
    def _task(row):
        output = dict(row)
        for name in ("goal", "steps", "step_timings", "task", "result"):
            output[name] = json.loads(output.pop(name + "_json"))
        return output

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 0 <= limit <= 10000:
            raise ValueError("Limit must be an integer in 0..10000")
        return limit

    def history(self, limit=100):
        with self._reader() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY created_at DESC,id DESC LIMIT ?",
                                      (self._limit(limit),)).fetchall()
        return [self._task(row) for row in rows]

    def stats(self):
        with self._reader() as connection:
            connection.execute("BEGIN")
            statuses = {row[0]: row[1] for row in connection.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status")}
            totals = connection.execute("SELECT COUNT(*),COALESCE(SUM(elapsed),0),COALESCE(SUM(recoveries),0),COALESCE(SUM(llm_calls),0) FROM tasks").fetchone()
            recovery = [dict(row) for row in connection.execute("SELECT * FROM recoveries ORDER BY failure,strategy")]
            counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                      for name in ("paths", "pages", "preferences", "skills", "skill_candidates")}
        successes = statuses.get("SUCCESS", 0) + statuses.get("SUCCEEDED", 0)
        for row in recovery:
            row["success_rate"] = row["successes"] / row["attempts"] if row["attempts"] else 0.
        return {"tasks": totals[0], "total_tasks": totals[0], "statuses": statuses,
                "successes": successes, "success_rate": successes / totals[0] if totals[0] else 0.,
                "elapsed": totals[1], "recoveries": totals[2], "llm_calls": totals[3],
                "recovery_methods": recovery, **counts, "schema_version": SCHEMA_VERSION}

    def remember_path(self, key, package, steps, verified=True):
        key, package = self._label(key), self._label(package, empty=True)
        if not isinstance(steps, list) or type(verified) is not bool:
            raise ValueError("Path steps must be a JSON array and verified must be boolean")
        _, encoded = self._encode(steps)
        self._check_size(key, package, encoded)
        identifier = hashlib.sha256(_json([key, package, encoded]).encode()).hexdigest()
        now = time.time()
        with self._transaction() as connection:
            connection.execute("""INSERT INTO paths VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET attempts=paths.attempts+1,
                successes=paths.successes+excluded.successes,failures=paths.failures+excluded.failures,
                updated_at=excluded.updated_at""",
                (identifier, key, package, encoded, 1, int(verified), int(not verified), now, now))
        return identifier

    def best_path(self, key, package=""):
        key, package = self._label(key), self._label(package, empty=True)
        query = "SELECT * FROM paths WHERE key=? AND successes>0"
        params = [key]
        if package:
            query += " AND package=?"
            params.append(package)
        query += " ORDER BY (successes+1.0)/(attempts+2.0) DESC,successes DESC,updated_at DESC,id LIMIT 1"
        with self._reader() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["steps"] = json.loads(result.pop("steps_json"))
        result["success_rate"] = result["successes"] / result["attempts"]
        result["reliability"] = (result["successes"] + 1) / (result["attempts"] + 2)
        return result

    def record_page(self, package, fingerprint, features):
        package, fingerprint = self._label(package, empty=True), self._label(fingerprint)
        _, encoded = self._encode(features)
        self._check_size(package, fingerprint, encoded)
        now = time.time()
        with self._transaction() as connection:
            connection.execute("""INSERT INTO pages VALUES(?,?,?,?,?,?)
                ON CONFLICT(package,fingerprint) DO UPDATE SET features_json=excluded.features_json,
                visits=pages.visits+1,updated_at=excluded.updated_at""", (package, fingerprint, encoded, 1, now, now))

    def pages(self, package="", limit=100):
        package = self._label(package, empty=True)
        with self._reader() as connection:
            rows = connection.execute("SELECT * FROM pages WHERE (?='' OR package=?) ORDER BY updated_at DESC LIMIT ?",
                                      (package, package, self._limit(limit))).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["features"] = json.loads(item.pop("features_json"))
            output.append(item)
        return output

    def record_recovery(self, failure, strategy, success):
        failure, strategy = self._label(failure), self._label(strategy)
        if type(success) is not bool:
            raise ValueError("Recovery success must be boolean")
        with self._transaction() as connection:
            connection.execute("""INSERT INTO recoveries VALUES(?,?,?,?,?)
                ON CONFLICT(failure,strategy) DO UPDATE SET attempts=recoveries.attempts+1,
                successes=recoveries.successes+excluded.successes,updated_at=excluded.updated_at""",
                (failure, strategy, 1, int(success), time.time()))

    def set_preference(self, key, value, explicit=False):
        if explicit is not True:
            raise ValueError("Only explicitly supplied preferences may be remembered")
        key = self._label(key)
        sanitized, encoded = self._encode(value)
        self._check_size(key, encoded)
        with self._transaction() as connection:
            connection.execute("""INSERT INTO preferences VALUES(?,?,1,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (key, encoded, time.time()))
        return {"key": key, "value": sanitized, "explicit": True}

    def preferences(self):
        with self._reader() as connection:
            return {row[0]: json.loads(row[1]) for row in connection.execute("SELECT key,value_json FROM preferences ORDER BY key")}

    @staticmethod
    def _skill(row):
        if row is None:
            return None
        output = dict(row)
        for name in ("definition", "validation", "approval"):
            output[name] = json.loads(output.pop(name + "_json"))
        return output

    def propose_skill(self, definition, source_task=""):
        if not isinstance(definition, dict):
            raise TypeError("Skill definition must be a JSON object")
        sanitized, encoded = self._encode(definition, declarative=True)
        name = self._label(sanitized.get("name") or "Unnamed skill")
        source_task = self._label(source_task, empty=True)
        self._check_size(encoded, encoded, name, source_task)
        identifier, now = uuid.uuid4().hex, time.time()
        with self._transaction() as connection:
            connection.execute("INSERT INTO skills VALUES(?,?,?,?,?,?,?,?,?)",
                               (identifier, name, "draft", encoded, source_task, "{}", "{}", now, now))
            connection.execute("INSERT INTO skill_candidates VALUES(?,?,?,?,?)",
                               (uuid.uuid4().hex, identifier, encoded, source_task, now))
        return identifier

    def get_skill(self, id):
        with self._reader() as connection:
            row = connection.execute("SELECT * FROM skills WHERE id=?", (self._label(id),)).fetchone()
        return self._skill(row)

    def list_skills(self):
        with self._reader() as connection:
            return [self._skill(row) for row in connection.execute("SELECT * FROM skills ORDER BY updated_at DESC,id")]

    def update_skill(self, id, **fields):
        allowed = {"name", "definition", "status", "validation", "approval", "validation_info", "approval_info"}
        if not fields or set(fields) - allowed:
            raise ValueError("Unknown or empty skill update fields")
        for alias, name in (("validation_info", "validation"), ("approval_info", "approval")):
            if alias in fields:
                if name in fields:
                    raise ValueError("Duplicate skill update field")
                fields[name] = fields.pop(alias)
        id = self._label(id)
        # The read, transition checks and write share a single write transaction.
        with self._transaction() as connection:
            row = self._skill(connection.execute("SELECT * FROM skills WHERE id=?", (id,)).fetchone())
            if row is None:
                raise KeyError(id)
            old_status = row["status"]
            if "definition" in fields:
                if not isinstance(fields["definition"], dict):
                    raise TypeError("Skill definition must be a JSON object")
                if fields.get("status", "draft") != "draft" or any(key in fields for key in ("validation", "approval")):
                    raise ValueError("Changed definitions must be independently validated and approved")
                row["definition"], _ = self._encode(fields["definition"], declarative=True)
                row.update(status="draft", validation={}, approval={})
                row["name"] = row["definition"].get("name") or row["name"]
                # Preserve proposed revisions as data for audit/review.
                connection.execute("INSERT INTO skill_candidates VALUES(?,?,?,?,?)",
                                   (uuid.uuid4().hex, id, _json(row["definition"]), row["source_task"], time.time()))
            for name in ("validation", "approval"):
                if name in fields:
                    if not isinstance(fields[name], dict):
                        raise TypeError(f"Skill {name} must be a JSON object")
                    row[name], _ = self._encode(fields[name])
            if "name" in fields:
                row["name"] = self._label(fields["name"])
            desired = fields.get("status", row["status"])
            if desired not in ("draft", "validated", "approved", "disabled"):
                raise ValueError("Invalid skill status")
            if "validation" in fields and not _validation_passed(row["validation"]):
                if fields.get("status") in ("validated", "approved"):
                    raise ValueError("Successful validation is required for this status")
                desired = "draft"
            if desired == "validated" and not _validation_passed(row["validation"]):
                raise ValueError("Validated skills require a successful validation report")
            if desired == "approved":
                if old_status not in ("validated", "approved") or not _validation_passed(row["validation"]):
                    raise ValueError("Approval requires prior successful validation")
                if row["approval"].get("explicit") is not True and row["approval"].get("confirmed") is not True:
                    raise ValueError("Approval requires explicit user confirmation")
            if desired != "approved":
                row["approval"] = {}
            if fields.get("status") == "draft" and "validation" not in fields:
                row["validation"] = {}
            row["status"] = desired
            row["name"] = self._label(row["name"])
            definition, validation, approval = (_json(row[name]) for name in ("definition", "validation", "approval"))
            self._check_size(row["name"], definition, validation, approval, row["source_task"])
            connection.execute("""UPDATE skills SET name=?,status=?,definition_json=?,validation_json=?,
                approval_json=?,updated_at=? WHERE id=?""",
                (row["name"], row["status"], definition, validation, approval, time.time(), id))
            updated = connection.execute("SELECT * FROM skills WHERE id=?", (id,)).fetchone()
        return self._skill(updated)

    @staticmethod
    def _backup_from(source, destination):
        # Never overwrite another file or back up into the active DB/WAL.
        path = Path(destination).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        target = sqlite3.connect(str(path))
        try:
            source.backup(target)
        finally:
            target.close()
        return str(path)

    def backup(self, path):
        target = str(Path(path).expanduser().resolve())
        if target in (self.path, self.path + "-wal", self.path + "-shm"):
            raise ValueError("Backup destination cannot be the active database")
        with self._reader() as connection:
            return self._backup_from(connection, target)

    def cleanup(self, days=30, max_tasks=10000):
        if type(days) not in (int, float) or not math.isfinite(days) or days < 0:
            raise ValueError("Cleanup days must be a nonnegative number")
        if type(max_tasks) is not int or max_tasks < 0:
            raise ValueError("max_tasks must be a nonnegative integer")
        cutoff = time.time() - days * 86400
        with self._transaction() as connection:
            aged = connection.execute("DELETE FROM tasks WHERE created_at<? AND status NOT IN ('QUEUED','RUNNING','PAUSED')", (cutoff,)).rowcount
            # Preserve active tasks even when they exceed the terminal-task cap.
            capped = connection.execute("""DELETE FROM tasks WHERE id IN (
                SELECT id FROM tasks WHERE status NOT IN ('QUEUED','RUNNING','PAUSED')
                ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?)""", (max_tasks,)).rowcount
        return {"tasks_deleted": aged + capped, "aged_tasks_deleted": aged, "capped_tasks_deleted": capped}
