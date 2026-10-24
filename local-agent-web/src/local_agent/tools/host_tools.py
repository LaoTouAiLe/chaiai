"""固定用途宿主适配：证书验证的文件下载、无 WMI 的系统查询。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from local_agent.exceptions import SecurityError
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.browser_tools import checked_url
from local_agent.tools.runtime_sessions import authorize


class _HttpRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_to_file(policy: SecurityPolicy, url: str, path: str, *,
                     overwrite: bool = False, timeout_seconds: float = 60) -> dict[str, Any]:
    """流式下载到同目录临时文件，成功校验授权后再原子落盘。"""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正数")
    checked_url(url)
    if url == "about:blank":
        raise ValueError("下载需要 HTTP 或 HTTPS 地址")
    arguments = {"path": path, "overwrite": overwrite}
    authorize(policy, "download_file", {**arguments, "url": url})
    target = policy.resolve_writable_path(path)
    if target.exists() and not overwrite:
        raise FileExistsError("目标文件已存在；确认覆盖后显式传 overwrite=true")
    target.parent.mkdir(parents=True, exist_ok=True)
    if policy.resolve_writable_path(path) != target:
        raise SecurityError("下载目标已变化")
    opener = urllib.request.build_opener(_HttpRedirects())
    digest = hashlib.sha256()
    size = 0
    descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=".download-", suffix=".part")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            request = urllib.request.Request(url, headers={"User-Agent": "local-agent-web"})
            # 默认 HTTPSHandler 使用平台默认 SSLContext，不能关闭证书校验。
            with opener.open(request, timeout=timeout_seconds) as response:
                final_url = checked_url(response.url)
                status = response.status
                while True:
                    if policy.cancellation is not None and policy.cancellation.cancelled:
                        raise SecurityError(policy.cancellation.refusal_reason())
                    data = response.read(65536)
                    if not data:
                        break
                    output.write(data)
                    digest.update(data)
                    size += len(data)
        authorize(policy, "download_file", {**arguments, "url": url})
        if policy.resolve_writable_path(path) != target:
            raise SecurityError("下载目标已变化，未替换目标文件")
        if overwrite:
            os.replace(temporary, target)
        else:
            # 不覆盖并发创建的文件；同目录硬链接为原子“仅创建”。
            os.link(temporary, target)
            temporary.unlink()
        return {"path": str(target), "bytes": size, "sha256": digest.hexdigest(),
                "http_status": status, "url": final_url}
    finally:
        temporary.unlink(missing_ok=True)


def system_processes(pid: int | None = None) -> list[dict[str, Any]]:
    """读取进程公开标识，不使用 WMI 或读取命令行。"""
    if os.name != "nt":
        proc = Path("/proc")
        if not proc.is_dir():
            raise RuntimeError("当前平台没有 /proc，请用 run_shell 执行本机进程查询")
        result = []
        for proc_path in proc.iterdir():
            if proc_path.name.isdigit() and (pid is None or int(proc_path.name) == pid):
                try:
                    result.append({"pid": int(proc_path.name), "name": (proc_path / "comm").read_text().strip()})
                except OSError:
                    continue
        return result
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        fn = getattr(kernel, name)
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        fn.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        result = []
        while more:
            if pid is None or pid == entry.th32ProcessID:
                result.append({"pid": entry.th32ProcessID, "parent_pid": entry.th32ParentProcessID,
                               "name": entry.szExeFile, "threads": entry.cntThreads})
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
        return result
    finally:
        kernel.CloseHandle(handle)


def build_host_tools(policy: SecurityPolicy) -> list[BaseTool]:
    """构造下载和只读系统查询适配。"""
    @tool
    def download_file(url: str, path: str, overwrite: bool = False,
                      timeout_seconds: float = 60) -> str:
        """下载公开 HTTP/HTTPS 文件到已授权位置，返回真实 HTTP 状态、字节数和 SHA256。

        使用宿主 Python TLS，保持证书验证，避免 Windows 受限令牌的 Schannel 凭据问题。
        适用于公开依赖或资料；不带登录 Cookie、凭据或自定义认证，不替代带认证的 curl 请求。
        默认不覆盖文件，不执行下载内容；外部写入仍必须有目录授权。
        """
        return json.dumps(download_to_file(policy, url, path, overwrite=overwrite,
                                          timeout_seconds=timeout_seconds), ensure_ascii=False)

    @tool
    def inspect_system(kind: Literal["platform", "processes"] = "platform",
                       pid: int | None = None) -> str:
        """只读查询本机平台、CPU 数、工作区磁盘容量或进程 PID/父 PID/名称。

        Windows 使用原生系统 API，不依赖 WMI。不会读取进程环境、命令行密钥或修改进程。
        processes 可传 pid 精确查询；终止进程请用 stop_shell 或已有 taskkill 命令。
        """
        authorize(policy, "inspect_system", {})
        if kind == "processes":
            return json.dumps(system_processes(pid), ensure_ascii=False)
        disk = shutil.disk_usage(policy.workspace_root)
        return json.dumps({"system": platform.system(), "release": platform.release(),
                           "machine": platform.machine(), "cpu_count": os.cpu_count(),
                           "disk_total_bytes": disk.total, "disk_free_bytes": disk.free})

    return [download_file, inspect_system]
