"""PID-only taskkill compatibility within the caller's Windows sandbox Job.

The system taskkill service path can fail under LUA tokens. This helper uses
ordinary process handles, never an elevated broker or filesystem permissions.
"""
from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Sequence
from ctypes import wintypes


def shim_script() -> bytes:
    """ASCII batch entry: the executable path is supplied by the trusted launcher."""
    args = ("--internal-sandbox-taskkill" if getattr(sys, "frozen", False)
            else '-I "%CHAIAI_CLEANUP_MODULE%"')
    return f'@"%CHAIAI_CLEANUP_EXECUTABLE%" {args} %*\r\n'.encode("ascii")


def parse_arguments(arguments: Sequence[str]) -> tuple[int, bool]:
    """Only /PID <decimal> /F [/T] is supported; no image/remote/user selectors."""
    pid: int | None = None
    force = tree = False
    index = 0
    while index < len(arguments):
        value = arguments[index].lower()
        if value == "/pid" and pid is None and index + 1 < len(arguments):
            text = arguments[index + 1]
            if not text.isascii() or not text.isdecimal() or not 0 < int(text) < 2**32:
                raise ValueError("/PID must be a positive DWORD")
            pid = int(text)
            index += 2
        elif value == "/f" and not force:
            force = True
            index += 1
        elif value == "/t" and not tree:
            tree = True
            index += 1
        else:
            raise ValueError("sandbox taskkill supports /PID <pid> /F [/T] only")
    if pid is None or not force:
        raise ValueError("sandbox taskkill requires /PID <pid> /F")
    return pid, tree


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


class _WindowsProcesses:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("sandbox taskkill requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
            "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result

    def job_pids(self) -> set[int]:
        # NULL means the caller's Job, not any Job containing the target process.
        size = 4096
        while size <= 1_048_576:
            buffer = ctypes.create_string_buffer(size)
            if self.kernel.QueryInformationJobObject(None, 3, buffer, size, None):
                count = wintypes.DWORD.from_buffer(buffer, 4).value
                if count > (size - 8) // ctypes.sizeof(ctypes.c_size_t):
                    raise OSError("invalid Job process list")
                ids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
                return set(ids)
            error = ctypes.get_last_error()
            if error != 234:  # ERROR_MORE_DATA
                raise OSError(error, "cannot query the caller's sandbox Job")
            size *= 2
        raise OSError("sandbox Job process list is too large")

    def parents(self) -> dict[int, int]:
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "cannot enumerate process ancestry")
        result: dict[int, int] = {}
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            ok = self.kernel.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                result[entry.th32ProcessID] = entry.th32ParentProcessID
                ok = self.kernel.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise OSError(ctypes.get_last_error(), "incomplete process ancestry")
            return result
        finally:
            self.kernel.CloseHandle(handle)

    def stop(self, pid: int, tree: bool) -> None:
        if pid == os.getpid() or pid not in self.job_pids():
            raise PermissionError("target is outside the caller's sandbox Job")
        ancestry = self.parents()
        ancestor = os.getpid()
        seen: set[int] = set()
        while ancestor and ancestor not in seen:
            seen.add(ancestor)
            if ancestor == pid:
                raise PermissionError("cannot terminate an ancestor of the cleanup helper")
            ancestor = ancestry.get(ancestor, 0)
        # Keep handles open throughout cleanup. Recheck membership after opening,
        # and use the handle's signaled state so PID reuse cannot target an outsider.
        handles: dict[int, int] = {}
        known = {pid}
        try:
            for _ in range(8):
                owned = self.job_pids()
                parents = self.parents() if tree else {}
                if tree:
                    while True:
                        children = {child for child, parent in parents.items()
                                    if parent in known and child in owned}
                        if children <= known:
                            break
                        known |= children
                pending = [item for item in known if item in owned and item not in handles]
                if not pending:
                    return
                for item in pending:
                    handle = self.kernel.OpenProcess(0x00101001, False, item)
                    if not handle:
                        if ctypes.get_last_error() == 87:  # process already gone
                            continue
                        raise OSError(ctypes.get_last_error(), "cannot open sandbox process")
                    handles[item] = handle
                    if item not in self.job_pids():
                        raise PermissionError("process left the caller's sandbox Job")
                    if (self.kernel.WaitForSingleObject(handle, 0) == 258
                            and not self.kernel.TerminateProcess(handle, 1)
                            and self.kernel.WaitForSingleObject(handle, 0) == 258):
                        raise OSError(ctypes.get_last_error(), "cannot terminate sandbox process")
                for item in pending:
                    if item in handles and self.kernel.WaitForSingleObject(handles[item], 1000) == 258:
                        raise OSError("sandbox process did not exit")
            raise OSError("sandbox process tree did not settle")
        finally:
            for handle in handles.values():
                self.kernel.CloseHandle(handle)


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        pid, tree = parse_arguments(sys.argv[1:] if arguments is None else arguments)
        _WindowsProcesses().stop(pid, tree)
    except (OSError, ValueError) as exc:
        print(f"sandbox taskkill: {exc}", file=sys.stderr)
        return 1
    print(f"Terminated sandbox process {pid}" + (" and descendants" if tree else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
