"""Verify and seal the Windows IPC bundle before loading it into new children."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import struct
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.security import win_sandbox

BUNDLE_DIRECTORY = Path(__file__).resolve().parents[1] / "native/windows/bin"
FILES = tuple(f"ipc-{kind}-{arch}.{extension}"
              for arch in ("x64", "x86")
              for kind, extension in (("compat", "dll"), ("loader", "exe")))
_LOCK = threading.RLock()


@dataclass(frozen=True)
class _SealedBundle:
    directory: Path
    handles: tuple[int, ...]  # OS closes these non-inheritable handles on host exit.


_BUNDLES: dict[str, _SealedBundle] = {}


def verify_bundle(directory: Path = BUNDLE_DIRECTORY, *, check_sources: bool = False) -> dict[str, bytes]:
    """Validate exact filenames, PE architecture and SHA256 without executing binaries."""
    try:
        raw = (directory / "manifest.json").read_bytes()
        if len(raw) > 64_000:
            raise ValueError("manifest too large")
        manifest = json.loads(raw)
        hashes = manifest["files"]
        if manifest["schema_version"] != 1 or not isinstance(hashes, dict) or set(hashes) != set(FILES):
            raise ValueError("unexpected IPC manifest fields")
        data = {"manifest.json": raw}
        for name in FILES:
            expected = hashes[name]
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("invalid binary hash")
            path = directory / name
            if path.is_symlink() or path.stat().st_size > 4_000_000:
                raise ValueError("invalid binary path or size")
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != expected:
                raise ValueError(f"IPC binary hash mismatch: {name}")
            if len(blob) < 64 or blob[:2] != b"MZ":
                raise ValueError(f"invalid PE file: {name}")
            offset = struct.unpack_from("<I", blob, 60)[0]
            machine = 0x8664 if "-x64." in name else 0x14C
            if offset + 6 > len(blob) or blob[offset:offset + 4] != b"PE\0\0" or struct.unpack_from("<H", blob, offset + 4)[0] != machine:
                raise ValueError(f"IPC architecture mismatch: {name}")
            data[name] = blob
        if check_sources:
            sources = manifest["sources"]
            names = {"ipc_common.h", "ipc_common.c", "ipc_compat.c", "ipc_loader.c"}
            if not isinstance(sources, dict) or set(sources) != names:
                raise ValueError("unexpected source manifest")
            for name in names:
                if hashlib.sha256((directory.parent / name).read_bytes()).hexdigest() != sources[name]:
                    raise ValueError(f"native source changed; rebuild: {name}")
        return data
    except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
        raise win_sandbox.SandboxUnavailable(f"Windows IPC 组件缺失或校验失败：{exc}；请重建或重装组件") from exc


def _open_locked(path: Path, *, directory: bool) -> int:
    """Hold a non-inheritable read handle that prevents replacement or deletion."""
    kernel = win_sandbox._kernel32
    from ctypes import wintypes

    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    share = 3 if directory else 1  # READ|WRITE for directories, READ only for binaries.
    handle = kernel.CreateFileW(str(path), 0x80000000, share, None, 3,
                                0x02000000 if directory else 0, None)
    if not handle or handle == win_sandbox.INVALID_HANDLE_VALUE:
        raise win_sandbox.SandboxUnavailable(f"无法锁定 Windows IPC 组件：{ctypes.get_last_error()}")
    return int(handle)


def prepare_bundle() -> Path:
    """Copy verified bytes into a guarded, locked host-owned cache; never fail open."""
    with _LOCK:
        data = verify_bundle()
        from local_agent.security.process_cleanup import shim_script

        data["taskkill.cmd"] = shim_script()
        key = hashlib.sha256(data["manifest.json"]).hexdigest()
        existing = _BUNDLES.get(key)
        if existing is not None:
            return existing.directory
        directory = Path(tempfile.mkdtemp(prefix="local-agent-ipc-"))
        handles: list[int] = []
        try:
            handles.append(_open_locked(directory, directory=True))
            # mkdtemp may rely on OWNER RIGHTS, which a LUA token cannot use.
            # The component needs explicit read/execute, never filesystem write.
            win_sandbox._set_access(str(directory), win_sandbox._access_entry(
                win_sandbox._current_user_sid_text(), win_sandbox.GRANT_ACCESS,
                inheritance=win_sandbox.CONTAINER_INHERIT_ACE | win_sandbox.OBJECT_INHERIT_ACE,
                permissions=0xA0000000,  # GENERIC_READ | GENERIC_EXECUTE
            ))
            win_sandbox._set_access(str(directory), win_sandbox._access_entry(
                win_sandbox.NATIVE_RUNTIME_GUARD_SID, win_sandbox.DENY_ACCESS,
                inheritance=win_sandbox.CONTAINER_INHERIT_ACE | win_sandbox.OBJECT_INHERIT_ACE,
                permissions=0x400D0040,  # write, DELETE, WRITE_DAC/OWNER, DELETE_CHILD
            ))
            for name, blob in data.items():
                with (directory / name).open("xb") as output:
                    output.write(blob)
                handles.append(_open_locked(directory / name, directory=False))
            if any((directory / name).read_bytes() != blob for name, blob in data.items()):
                raise win_sandbox.SandboxUnavailable("Windows IPC 缓存字节校验失败")
        except BaseException:
            for handle in handles:
                win_sandbox._kernel32.CloseHandle(handle)
            raise
        _BUNDLES[key] = _SealedBundle(directory, tuple(handles))
        return directory


def attach(process_id: int, process_handle: Any, directory: Path) -> None:
    """Initialize a verified component only in the caller's new suspended process."""
    from ctypes import wintypes

    from local_agent.security.process_guard import ProcessRegistry

    # The runner revalidates the sealed bytes before invoking any native helper.
    verify_bundle(directory)
    kernel = win_sandbox._kernel32
    kernel.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT),
                                      ctypes.POINTER(wintypes.USHORT)]
    kernel.IsWow64Process2.restype = wintypes.BOOL
    target, native = wintypes.USHORT(), wintypes.USHORT()
    if not kernel.IsWow64Process2(process_handle, ctypes.byref(target), ctypes.byref(native)):
        raise win_sandbox.SandboxUnavailable(f"无法识别命令架构：{ctypes.get_last_error()}")
    machine = target.value or native.value
    arch = {0x8664: "x64", 0x14C: "x86"}.get(machine)
    if arch is None:
        raise win_sandbox.SandboxUnavailable(f"Windows IPC 组件暂不支持命令架构：{machine:#x}")
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    result = ProcessRegistry().run([
        str(directory / f"ipc-loader-{arch}.exe"), "--pid", str(process_id),
        str(directory / f"ipc-compat-{arch}.dll"),
    ], env=environment, timeout_seconds=20, creationflags=0x08000000)
    if result.exit_code != 0 or result.timed_out or result.cancelled:
        raise win_sandbox.SandboxUnavailable(
            f"Windows IPC 组件初始化失败（退出码 {result.exit_code}）；命令入口未放行，未自动重试"
        )
