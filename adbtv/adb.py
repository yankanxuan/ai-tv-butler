"""adb 命令行封装。

既支持真机（系统 adb），也支持内置模拟器（tests/fake_adb.py，
通过 adb.binary = "python3 /abs/path/tests/fake_adb.py" 指定）。
"""
from __future__ import annotations

import inspect
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


class AdbError(RuntimeError):
    pass


@dataclass
class CmdResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check_returncode(self) -> None:
        if not self.ok:
            detail = (self.stderr or self.stdout).strip()
            raise AdbError(f"adb 命令失败({self.returncode}): {shlex.join(self.args)}: {detail}")

    @property
    def out(self) -> str:
        self.check_returncode()
        return self.stdout.strip()


class AdbClient:
    def __init__(self, cfg: dict[str, Any]):
        adb = cfg.get("adb", {})
        self.cfg = cfg
        self.binary = adb.get("binary", "adb")
        self.serial = (adb.get("serial") or "").strip()
        self.auto_connect = bool(adb.get("auto_connect", True))
        self.connect_timeout = int(adb.get("connect_timeout", 15))
        self.command_timeout = int(adb.get("command_timeout", 20))
        self.fake_state = adb.get("fake_state") or ""
        # "inprocess" 时不启动子进程：直接调用同一个模拟器实现，供测试提速。
        self.simulator_mode = str(adb.get("simulator", "") or "").lower()
        self._sim_instance = None
        self._connected = False
        # Runtime may pause/cancel before every ADB operation. Nested reads made
        # by the hook itself do not recursively invoke the checkpoint.
        self.checkpoint: Callable[..., None] | None = None
        import threading
        self._checkpoint_state = threading.local()

    @property
    def _in_checkpoint(self):
        return getattr(self._checkpoint_state, "active", False)

    # ---------- 基础执行 ----------
    def _base(self) -> list[str]:
        base = shlex.split(self.binary) if self.binary else ["adb"]
        if self.serial:
            base += ["-s", self.serial]
        return base

    def _env(self) -> dict[str, str]:
        import os
        env = dict(os.environ)
        if self.fake_state:
            env["FAKE_ADB_STATE"] = self.fake_state
        key_home = self.cfg.get("adb", {}).get("key_home")
        if key_home:
            from pathlib import Path
            directory = Path(key_home).expanduser().resolve()
            # Android platform-tools place .android below HOME. The configured
            # persistent directory must be named .android to keep that contract.
            if directory.name != ".android":
                raise AdbError("adb.key_home必须指向名为.android的持久目录")
            env["HOME"] = str(directory.parent)
        return env

    def run(self, args: list[str], timeout: int | None = None,
            binary_out: bool = False) -> CmdResult | subprocess.CompletedProcess:
        if self.checkpoint is not None and not getattr(self._checkpoint_state, "active", False):
            self._checkpoint_state.active = True
            try:
                # Inspect before calling so an internal TypeError is never
                # mistaken for an incompatible callback signature.
                signature = inspect.signature(self.checkpoint)
                try:
                    signature.bind(args)
                except TypeError:
                    signature.bind()
                    self.checkpoint()
                else:
                    self.checkpoint(list(args))
            finally:
                self._checkpoint_state.active = False
        if self.simulator_mode == "inprocess":
            return self._run_inprocess(args)
        cmd = self._base() + args
        t = timeout or self.command_timeout
        try:
            if binary_out:
                p = subprocess.run(cmd, capture_output=True, timeout=t, env=self._env())
                return p  # bytes stdout
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=t, env=self._env())
            return CmdResult(args=cmd, returncode=p.returncode,
                             stdout=p.stdout, stderr=p.stderr)
        except FileNotFoundError as e:
            raise AdbError(f"找不到 adb 可执行文件: {self.binary} ({e})") from e
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 命令超时({t}s): {' '.join(cmd)}") from e

    def _simulator_backend(self):
        if self._sim_instance is None:
            from .simulator import InProcessSimulator
            self._sim_instance = InProcessSimulator(self.fake_state or None)
        return self._sim_instance

    def _run_inprocess(self, args: list[str]) -> CmdResult:
        """Execute deviceless commands against the in-memory simulator."""
        argv = list(args)
        # Strip the "-s <serial>" that real adb would receive.
        if self.serial and argv[:2] == ["-s", self.serial]:
            argv = argv[2:]
        returncode, payload = self._simulator_backend().invoke(argv)
        if isinstance(payload, (bytes, bytearray)):
            text = payload.decode("utf-8", "replace")
        else:
            text = str(payload)
        return CmdResult(args=args, returncode=returncode, stdout=text, stderr="")

    def shell(self, cmd: str | list[str], timeout: int | None = None) -> CmdResult:
        # adb concatenates shell arguments for a *remote* shell. A host-side
        # argv list alone therefore does not quote the arguments on the TV.
        if isinstance(cmd, str):
            # Only fixed internal read commands retain the legacy string form.
            # Variable arguments must use a list, never interpolated shell code.
            allowed = {"wm size", "dumpsys window", "dumpsys media_session",
                       "dumpsys audio", "pm list packages -3", "pm list packages -s"}
            if cmd not in allowed:
                raise ValueError("字符串 shell 仅允许固定内部读取命令；请使用参数列表")
            command = shlex.join(shlex.split(cmd))
        elif isinstance(cmd, list) and cmd and all(isinstance(arg, str) for arg in cmd):
            command = shlex.join(cmd)
        else:
            raise ValueError("shell 需要非空字符串参数列表")
        return self.run(["shell", command], timeout=timeout)  # type: ignore[return-value]

    # ---------- 连接管理 ----------
    def devices(self) -> list[dict[str, str]]:
        r = self.run(["devices", "-l"])  # type: ignore[assignment]
        assert isinstance(r, CmdResult)
        r.check_returncode()
        rows = []
        for line in r.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial = parts[0]
            state = parts[1] if len(parts) > 1 else "?"
            extra = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            rows.append({"serial": serial, "state": state, **extra})
        return rows

    def state(self) -> str:
        if not self.serial:
            return "no-serial"
        for d in self.devices():
            if d["serial"] == self.serial:
                return d["state"]
        return "absent"

    def connect(self, force: bool = False) -> str:
        """若 serial 是 host:port 形式则尝试 adb connect。返回当前状态。"""
        if not self.serial:
            raise AdbError("未配置 adb.serial / adb.host，无法连接。")
        if ":" not in self.serial:
            # USB 直连设备，无需 connect
            return self.state()
        st = self.state()
        if st == "device":
            return st
        r = self.run(["connect", self.serial], timeout=self.connect_timeout)  # type: ignore[assignment]
        assert isinstance(r, CmdResult)
        out = (r.stdout + r.stderr).strip()
        st = self.state()
        # 真机离线/未授权时给出可操作提示
        if st != "device":
            raise AdbError(
                f"连接 {self.serial} 失败（状态={st}）：{out}\n"
                "排查：①电视/盒子需在 设置→开发者选项/运维调试 中打开 ADB；"
                "②首次连接需在电视弹窗点“允许 USB 调试”；"
                "③中兴盒子一般需在“运维调试”里用随机码计算密码激活 ADB；"
                "④确认 IP/端口；⑤标准 ADB 端口通常为 5555。"
            )
        return st

    def ensure(self) -> None:
        st = self.state()
        if st == "device":
            self._connected = True
            return
        if not self.serial:
            raise AdbError("未配置 adb.serial / adb.host，请在 config.yaml 里填写电视地址。")
        self.connect(force=True)

    # ---------- 便捷读取 ----------
    def getprop(self, key: str) -> str:
        return self.shell(["getprop", key]).out

    def screenshot_bytes(self) -> bytes:
        p = self.run(["exec-out", "screencap", "-p"], binary_out=True)
        if p.returncode != 0:
            detail = p.stderr.decode("utf-8", errors="replace") if isinstance(p.stderr, bytes) else p.stderr
            raise AdbError(f"adb 截图失败({p.returncode}): {detail}")
        out = p.stdout  # type: ignore[attr-defined]
        if not out:
            raise AdbError("adb 截图返回空数据")
        return out
