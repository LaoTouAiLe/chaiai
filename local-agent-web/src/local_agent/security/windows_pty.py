"""ConPTY with the existing restricted token; entered only through process_guard.

References: Microsoft Learn, Creating a Pseudoconsole Session / ClosePseudoConsole.
No unrestricted fallback, inherited environment, or terminal transcript on disk.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from pathlib import Path

from local_agent.security import win_ipc
from local_agent.security import win_sandbox as sandbox
from local_agent.security.process_command import windows_command_line


class _Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _StartupEx(ctypes.Structure):
    _fields_ = [("StartupInfo", sandbox._STARTUPINFOW), ("attributes", ctypes.c_void_p)]


def supported() -> bool:
    """Require both the write sandbox and the OS pseudoconsole API."""
    return sandbox.is_supported() and hasattr(sandbox._kernel32, "CreatePseudoConsole")


def _bind() -> None:
    kernel = sandbox._kernel32
    declarations = {
        "CreatePseudoConsole": ([_Coord, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.HANDLE)], ctypes.c_long),
        "ResizePseudoConsole": ([wintypes.HANDLE, _Coord], ctypes.c_long),
        "ClosePseudoConsole": ([wintypes.HANDLE], None),
        "InitializeProcThreadAttributeList": ([ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        "UpdateProcThreadAttribute": ([ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                       ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                       ctypes.c_void_p], wintypes.BOOL),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "DuplicateHandle": ([wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                             ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL,
                             wintypes.DWORD], wintypes.BOOL),
    }
    for name, (arguments, result) in declarations.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result


def _check(ok: object, operation: str) -> None:
    if not ok:
        raise sandbox.SandboxUnavailable(f"{operation} 失败：{ctypes.get_last_error()}")


class WindowsPty:
    """One restricted process tree and its pseudoconsole handles."""

    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str],
                 writable_sids: tuple[str, ...], cols: int, rows: int) -> None:
        if not supported():
            raise sandbox.SandboxUnavailable("此平台不支持受控 ConPTY 终端。")
        _bind()
        self._lock = threading.RLock()
        self._input = wintypes.HANDLE()
        self._output = wintypes.HANDLE()
        self._console = wintypes.HANDLE()
        self._job = wintypes.HANDLE()
        self._process = wintypes.HANDLE()
        self._exit: int | None = None
        self.pid = 0
        kernel = sandbox._kernel32
        input_read, output_write = wintypes.HANDLE(), wintypes.HANDLE()
        token, attributes_ready = 0, False
        attribute_buffer = None
        info = sandbox._PROCESS_INFORMATION()
        try:
            directory = win_ipc.prepare_bundle()
            token = sandbox.restricted_token(writable_sids=writable_sids)
            _check(kernel.CreatePipe(ctypes.byref(input_read), ctypes.byref(self._input), None, 65536),
                   "CreatePipe(input)")
            _check(kernel.CreatePipe(ctypes.byref(self._output), ctypes.byref(output_write), None, 65536),
                   "CreatePipe(output)")
            result = kernel.CreatePseudoConsole(_Coord(cols, rows), input_read, output_write, 0,
                                                ctypes.byref(self._console))
            if result < 0:
                raise sandbox.SandboxUnavailable(f"CreatePseudoConsole 失败：0x{result & 0xffffffff:08x}")
            size = ctypes.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attribute_buffer = ctypes.create_string_buffer(size.value)
            _check(kernel.InitializeProcThreadAttributeList(attribute_buffer, 1, 0, ctypes.byref(size)),
                   "InitializeProcThreadAttributeList")
            attributes_ready = True
            _check(kernel.UpdateProcThreadAttribute(attribute_buffer, 0, 0x00020016,
                                                    self._console, ctypes.sizeof(wintypes.HANDLE),
                                                    None, None), "UpdateProcThreadAttribute")
            startup = _StartupEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            # Prevent redirected server/test stdio from bypassing ConPTY. Null
            # standard handles are filled by the attached console at startup.
            startup.StartupInfo.dwFlags = sandbox.STARTF_USESTDHANDLES
            startup.attributes = ctypes.cast(attribute_buffer, ctypes.c_void_p)
            environment = ctypes.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda pair: pair[0].upper()))
                + "\0\0"
            )
            command = ctypes.create_unicode_buffer(windows_command_line(argv))
            _check(sandbox._advapi32.CreateProcessAsUserW(
                token, argv[0], command, None, None, False,
                0x00080000 | 0x00000400 | sandbox.CREATE_SUSPENDED,
                environment, str(cwd), ctypes.byref(startup.StartupInfo), ctypes.byref(info),
            ), "CreateProcessAsUserW(ConPTY)")
            self._process = wintypes.HANDLE(info.hProcess)
            self.pid = int(info.dwProcessId)
            self._job = wintypes.HANDLE(kernel.CreateJobObjectW(None, None))
            _check(self._job.value, "CreateJobObjectW")
            limits = sandbox._JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = sandbox.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(kernel.SetInformationJobObject(self._job, sandbox.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                                  ctypes.byref(limits), ctypes.sizeof(limits)),
                   "SetInformationJobObject")
            _check(kernel.AssignProcessToJobObject(self._job, self._process), "AssignProcessToJobObject")
            win_ipc.attach(self.pid, self._process, directory)
            _check(kernel.ResumeThread(info.hThread) != 0xFFFFFFFF, "ResumeThread")
        except BaseException:
            if self._process.value:
                kernel.TerminateProcess(self._process, 127)
            self.close()
            raise
        finally:
            if attributes_ready:
                kernel.DeleteProcThreadAttributeList(attribute_buffer)
            for handle in (input_read, output_write, info.hThread, token):
                if handle:
                    kernel.CloseHandle(handle)

    def read(self) -> bytes:
        """Drain available bytes without blocking input or process cleanup."""
        with self._lock:
            if not self._output.value:
                return b""
            kernel = sandbox._kernel32
            available = wintypes.DWORD()
            if not kernel.PeekNamedPipe(self._output, None, 0, None, ctypes.byref(available), None):
                return b""
            if not available.value:
                return b""
            buffer = ctypes.create_string_buffer(min(65536, available.value))
            read = wintypes.DWORD()
            if not kernel.ReadFile(self._output, buffer, len(buffer), ctypes.byref(read), None):
                return b""
            return buffer.raw[:read.value]

    def write(self, data: bytes) -> None:
        """Write on the input worker; keep a duplicate handle safe from concurrent close."""
        kernel = sandbox._kernel32
        duplicate = wintypes.HANDLE()
        with self._lock:
            if not self._input.value:
                raise RuntimeError("终端已结束。")
            current = kernel.GetCurrentProcess()
            _check(kernel.DuplicateHandle(current, self._input, current, ctypes.byref(duplicate),
                                          0, False, 2), "DuplicateHandle")
        try:
            offset = 0
            while offset < len(data):
                buffer = ctypes.create_string_buffer(data[offset:])
                written = wintypes.DWORD()
                _check(kernel.WriteFile(duplicate, buffer, len(data) - offset,
                                        ctypes.byref(written), None), "WriteFile")
                _check(written.value, "WriteFile(empty)")
                offset += written.value
        finally:
            kernel.CloseHandle(duplicate)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the actual console buffer, not just the browser element."""
        with self._lock:
            if not self._console.value:
                raise RuntimeError("终端已结束。")
            if sandbox._kernel32.ResizePseudoConsole(self._console, _Coord(cols, rows)) < 0:
                raise sandbox.SandboxUnavailable("终端尺寸调整失败。")

    def poll(self) -> int | None:
        """Return only an observed process exit code."""
        with self._lock:
            if self._exit is not None or not self._process.value:
                return self._exit
            kernel = sandbox._kernel32
            if kernel.WaitForSingleObject(self._process, 0) == 0:
                code = wintypes.DWORD()
                _check(kernel.GetExitCodeProcess(self._process, ctypes.byref(code)), "GetExitCodeProcess")
                self._exit = int(code.value)
            return self._exit

    def close(self) -> None:
        """Kill the entire job before releasing pipes; safe to call more than once."""
        with self._lock:
            kernel = sandbox._kernel32
            if self._job.value:
                _check(kernel.TerminateJobObject(self._job, 1), "TerminateJobObject")
                kernel.CloseHandle(self._job)
                self._job = wintypes.HANDLE()
            if self._process.value:
                _check(kernel.WaitForSingleObject(self._process, 3000) == 0, "WaitForSingleObject(exit)")
                self.poll()
                kernel.CloseHandle(self._process)
                self._process = wintypes.HANDLE()
            # Close output first: pre-24H2 ClosePseudoConsole otherwise may wait for its drain.
            for name in ("_output", "_input"):
                handle = getattr(self, name)
                if handle.value:
                    kernel.CloseHandle(handle)
                    setattr(self, name, wintypes.HANDLE())
            if self._console.value:
                kernel.ClosePseudoConsole(self._console)
                self._console = wintypes.HANDLE()
