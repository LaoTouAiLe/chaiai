"""Windows 写侧沙箱：受限令牌、逐可写根 SID、目录 ACL 与 Job Object。

2026-10-14 审查修复：restricting 列表使用 TokenGroups 的真实登录 SID，绝不携带 TokenUser。
令牌 DefaultDacl 给登录 SID 授予内核对象访问权，解决去掉用户 SID 后的 DLL 初始化失败；
普通可写目录显式保留用户写权限供第一道检查使用，第二道检查由当前可写根 SID 决定。
每个可写根使用不同的 v3 SID，撤销授权后下一轮不再携带旧 SID；旧版整树继承 SID 不复用。
源码 runner 用 Python -I 启动；冻结 EXE 使用独立内部入口分流，不读取用户配置或启动 API。
进程挂起创建、装 Job 后恢复；stdin 继承父进程的 DEVNULL，读取返回 EOF。
stdout 走匿名管道收，**stderr 继承 runner 自己的 stderr**（2026-10-15 起）：两者分开，形状与不进沙箱时一致。

每进程首次物化时扫描现有目录；受保护子树添加沙箱 SID 的拒绝 ACE。
因此新目录可创建，且**立即继承可写**（2026-10-15 起：根上一条可继承 ACE，由系统传播到整棵树）。

**根上的 ACE 是"读+写"**（2026-10-15 起；此前只写）：受限令牌**不能**依赖 ``OWNER RIGHTS`` 的隐式所有者
权限，只写会让工作区在"ACL 没有显式列出当前用户"的目录上连 ``dir`` / 管道 / 跑测试都做不了
（实测：pytest 临时目录正是这种 ACL）。读侧本来就不隔离（见下），补读没有新增能力。
目录重命名/删除没有额外授予权限。扫描不跟随目录链接。
普通子目录无法枚举时保留跳过记录并持续报告：实际写权限和内部保护范围未核实。
可写根授权失败或受保护子树的拒绝项建立失败均阻止执行，不能拿继承来的允许项继续运行。
例外仅限普通文件：无法修改 DACL 时，用真实受限令牌逐位核验写、删除、改权限及父目录删除子项
均被拒，保留原 ACL；策略层每次调用用本轮全部写授权 SID 再核验，目录不适用此回退。

已知边界：读侧和网络不隔离；Everyone/登录 SID 本身已获写权限的对象仍可能可写；
NTFS 对象别名（硬链接）、并发宿主改名不提供路径级强隔离；设备对象不受目录授权控制。
FAT/不支持 DACL 的卷：**根**授权失败即拒绝执行。扫描成本随目录数增长，
被跳过的子树不再深入。
DefaultDacl 修复后本机解释器可导入 ctypes/ssl/hashlib，不承诺其他机器的扩展模块兼容性。
本模块直接调用不支持的平台仍失败闭合；策略层已明确选择非 Windows 无 OS 边界运行并报告。
首次扫描后新建的凭据子树不会即时获得拒绝项（需下次进程启动扫描），不是按名称实时隔离。

准入仍由能力可见性与审批门决定；本模块仅经 process_guard 的唯一出口负责进程写边界。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

if __name__ == "__main__":
    # Keep helper imports and this -m runner on one exception/Win32 module instance.
    sys.modules["local_agent.security.win_sandbox"] = sys.modules[__name__]

from local_agent.security.path_guard import (
    PROTECTED_SUBPATH_NAMES,
    ProtectedPathContext,
    protected_target_reason,
)
from local_agent.security.process_command import windows_command_line

# ---- Win32 常量 ---------------------------------------------------------------

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100

DISABLE_MAX_PRIVILEGE = 0x1
LUA_TOKEN = 0x4
WRITE_RESTRICTED = 0x8
# Only denies writes to the sealed native-runtime cache; never grants file access.
NATIVE_RUNTIME_GUARD_SID = "S-1-5-21-313704811-1392647291-206703895-1048577"

STARTF_USESTDHANDLES = 0x00000100
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
HANDLE_FLAG_INHERIT = 0x00000001
INFINITE = 0xFFFFFFFF

GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000
#: ``GetStdHandle`` 的标准句柄号（无符号 32 位写法）。
STD_ERROR_HANDLE = 0xFFFFFFF4
STD_INPUT_HANDLE = 0xFFFFFFF6
#: ``GetStdHandle`` 失败时可能给的哨兵值（``INVALID_HANDLE_VALUE``；``NULL`` 在 ctypes 里是 ``None``）。
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GRANT_ACCESS = 1
DENY_ACCESS = 3
REVOKE_ACCESS = 4
CONTAINER_INHERIT_ACE = 0x2
OBJECT_INHERIT_ACE = 0x1
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

# **Job 句柄必须活到持有它的进程退出**（2026-10-14）：``KILL_ON_JOB_CLOSE`` 的触发点就是"最后一个句柄
# 关闭"那一刻，而"runner 自己退出"正是我们要的时机。这里显式持有，不依赖"ctypes 不会自动关句柄"这种
# 隐晦行为——那种依赖一旦被重构掉，"进程一定收得回"就会静默失效（本仓刚被这类静默失效咬过）。
_LIVE_JOBS: list[int] = []
#: 本进程内**已经物化过**的可写根（规范化路径）。
#:
#: 为什么只按根记：根的写 ACE 现在带 `(OI)(CI)` 继承，Windows 会把它传播到整棵子树，
#: 所以"这个根授权过了"就等于"整棵树都授权过了"——不必再逐目录判断（那是旧实现每次
#: 调用 45 秒的来源）。跨重启复用由 :func:`_dacl_mentions_sid` 的幂等检查兜住。
_MATERIALIZED_ROOTS: dict[str, GrantReport] = {}


class SandboxUnavailable(RuntimeError):
    """沙箱不可用（非 Windows、或某个 Win32 调用失败）。

    调用方**不许**在收到它之后退回"裸跑"——那是静默降级，正是本机制要消灭的东西
    （dsh 的移植笔记明确记过 POC 的 fail-open 坑）。要退出并如实说明。
    """


@dataclass(frozen=True)
class SkippedDirectory:
    """物化时被跳过的目录及其原因：**不静默**，由调用方如实说明给用户与模型。"""

    path: str
    reason: str


@dataclass(frozen=True)
class GrantReport:
    """一次 :func:`grant_write` 的结果：跳过了哪些目录、为什么。

    ``skipped`` 非空表示未完成检查的普通目录，不证明它们不可写；记录在缓存命中时也要保留。
    ``read_only_files`` 记录无法修改 DACL、但已核验不可变更的普通文件；每次执行仍须用完整 SID 复核。
    其余受保护子树拒绝项失败直接阻止执行。
    """

    skipped: tuple[SkippedDirectory, ...] = ()
    # 无法添加拒绝项、但已通过受限令牌访问检查的普通文件；策略层用完整 SID 集合复核。
    read_only_files: tuple[str, ...] = ()


if sys.platform == "win32":  # pragma: no cover - 平台分支
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - 非 Windows 上只有导入与类型可用
    _advapi32 = None
    _kernel32 = None


# ---- 结构体 -------------------------------------------------------------------


class _SID_IDENTIFIER_AUTHORITY(ctypes.Structure):
    _fields_ = [("Value", ctypes.c_ubyte * 6)]


class SID(ctypes.Structure):
    """Windows SID 的最大容量；派生 SID 使用 4 段，真实用户 SID 可以更长。"""

    _fields_ = [
        ("Revision", ctypes.c_ubyte),
        ("SubAuthorityCount", ctypes.c_ubyte),
        ("IdentifierAuthority", _SID_IDENTIFIER_AUTHORITY),
        ("SubAuthority", ctypes.c_ulong * 15),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_GROUPS(ctypes.Structure):
    _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _GENERIC_MAPPING(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in ("read", "write", "execute", "all")]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


class _TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", wintypes.DWORD),
        ("TrusteeForm", wintypes.DWORD),
        ("TrusteeType", wintypes.DWORD),
        ("ptstrName", ctypes.c_void_p),
    ]


class _EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", wintypes.DWORD),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", _TRUSTEE_W),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _bind() -> None:
    """声明 Win32 签名。放在函数里，非 Windows 上就不执行（模块可安全导入）。"""
    if _advapi32 is None or _kernel32 is None:  # pragma: no cover - 平台分支
        return
    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.SetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _advapi32.SetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)
    ]
    _advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.POINTER(SID), ctypes.POINTER(wintypes.LPWSTR)
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    _advapi32.GetLengthSid.restype = wintypes.DWORD
    _advapi32.IsValidSid.argtypes = [ctypes.POINTER(SID)]
    _advapi32.IsValidSid.restype = wintypes.BOOL
    _advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(SID_AND_ATTRIBUTES), wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(SID_AND_ATTRIBUTES), ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.CreateRestrictedToken.restype = wintypes.BOOL
    _advapi32.DuplicateToken.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.DuplicateToken.restype = wintypes.BOOL
    _advapi32.AccessCheck.argtypes = [
        ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(_GENERIC_MAPPING),
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.AccessCheck.restype = wintypes.BOOL
    _advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
    _advapi32.SetEntriesInAclW.argtypes = [
        wintypes.ULONG, ctypes.POINTER(_EXPLICIT_ACCESS_W), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.SetEntriesInAclW.restype = wintypes.DWORD
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.InitializeSecurityDescriptor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
    _advapi32.SetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, ctypes.c_void_p, wintypes.BOOL,
    ]
    _advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    _advapi32.SetFileSecurityW.restype = wintypes.BOOL
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD,
    ]
    _kernel32.CreatePipe.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    _kernel32.GetStdHandle.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    # 幂等检查用：把安全描述符转成 SDDL 文本，再判我们的 SID 是否已经在里面（不做 ACE 解析）。
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    _kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    _kernel32.SetHandleInformation.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL


_bind()


# ---- SID 派生与 ACL 物化 -------------------------------------------------------


def workspace_sid_text(path: str) -> str:
    """由**规范化工作区路径**派生确定性 SID 文本（``S-1-5-…``，四个 subauthority 来自 sha256）。

    工作区、临时目录及每个外部读写根分别派生；只有当前可写根的 SID 才进入令牌。
    ACE 可复用：根上一条可继承 ACE 就够，系统会把它传播到整棵子树（不必逐目录写）。
    """
    return _sid_to_text(_workspace_sid_struct(path))


def _workspace_sid_struct(path: str) -> SID:
    normalized = os.path.normcase(os.path.realpath(path))
    # v1 的整树继承 ACE 不再进入令牌；避免旧的过宽授权继续生效。
    # v3（2026-10-15）：根上的 ACE 由"只写"改成"读+写"（见 :func:`grant_write`）。旧 v2 ACE 是只写的，
    # 而幂等检查只看"这条 SID 有没有被提到过"，所以换版本号让每个可写根**重新物化一次**——
    # 否则已经授权过的工作区永远补不上读权限（受限令牌连 `dir` 都做不了）。
    digest = hashlib.sha256(("directory-v3:" + normalized).encode("utf-8")).digest()
    sid = SID()
    sid.Revision = 1
    sid.SubAuthorityCount = 4
    sid.IdentifierAuthority.Value = (ctypes.c_ubyte * 6)(0, 0, 0, 0, 0, 5)  # SECURITY_NT_AUTHORITY
    for index in range(4):
        # 强制非零：避开"subauthority 全零"这类畸形 SID（同时也是 v3 里验过的取法）。
        sid.SubAuthority[index] = int.from_bytes(digest[index * 4 : index * 4 + 4], "little") | 1
    if not _advapi32.IsValidSid(ctypes.byref(sid)):  # pragma: no cover - 派生逻辑固定合法
        raise SandboxUnavailable(f"派生出的 SID 不合法：{path}")
    return sid


def _sid_to_text(sid: SID) -> str:
    text = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(ctypes.byref(sid), ctypes.byref(text)):
        raise SandboxUnavailable(f"ConvertSidToStringSidW 失败：{ctypes.get_last_error()}")
    value = text.value or ""
    _kernel32.LocalFree(text)
    return value


def _sid_from_text(text: str) -> SID:
    """把 SID 文本转成**自带内存**的 ``SID``。

    **不能用** ``cast(pointer, POINTER(SID)).contents``：那是"指向 ``ConvertStringSidToSidW`` 缓冲的
    **视图**"，``LocalFree`` 之后就是悬垂指针（2026-10-14 实测：单跑绿、与别的用例合跑随机
    ``SetEntriesInAclW 87``——被释放的内存被别人复用了；更坏的情形是把垃圾当 SID **静默授出去**）。
    所以按 ``GetLengthSid`` 的真实长度**整块拷进自己的结构体**。

    Raises:
        SandboxUnavailable: 转换失败，或 SID 长于 Windows 支持的最大容量。
    """
    pointer = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW(text, ctypes.byref(pointer)):
        raise SandboxUnavailable(f"ConvertStringSidToSidW 失败：{ctypes.get_last_error()}")
    try:
        length = int(_advapi32.GetLengthSid(pointer))
        if length <= 0:
            raise SandboxUnavailable(f"GetLengthSid 失败：{ctypes.get_last_error()}")
        if length > ctypes.sizeof(SID):
            raise SandboxUnavailable(
                f"SID 长于本模块结构体（{length} > {ctypes.sizeof(SID)}）：{text}"
            )
        sid = SID()
        ctypes.memmove(ctypes.byref(sid), pointer, length)
        return sid
    finally:
        _kernel32.LocalFree(pointer)


def _current_dacl(path: str) -> tuple[ctypes.c_void_p, ctypes.c_void_p]:
    """读对象的当前 DACL；返回 ``(acl 指针, 承载它的安全描述符)``——**两个都要 LocalFree**。

    为什么要读：``SetEntriesInAclW(…, OldAcl=NULL, …)`` 产出的是**只含新条目**的 ACL，接着
    ``SetNamedSecurityInfoW`` 会**整体替换**目录 DACL——那会抹掉别人（以及继承来的）ACE，真工作区上
    不可接受，也会连带去掉子进程需要的其它权限项（2026-10-14 的首版实现正是这么写的，测试因此暴露：
    授权成功后受限进程仍写不进去）。正确做法是"读现有 + 追加/撤销 + 写回"。
    """
    acl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    code = _advapi32.GetNamedSecurityInfoW(
        path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
        ctypes.byref(acl), None, ctypes.byref(descriptor),
    )
    if code:
        raise SandboxUnavailable(f"GetNamedSecurityInfoW 失败：{code}（路径 {path}）")
    return acl, descriptor


def _write_dacl(path: str, old_acl: ctypes.c_void_p, entry: _EXPLICIT_ACCESS_W) -> None:
    """把 ``entry`` 追加/撤销到 ``old_acl`` 上，再写回 ``path``。"""
    merged = ctypes.c_void_p()
    code = _advapi32.SetEntriesInAclW(1, ctypes.byref(entry), old_acl, ctypes.byref(merged))
    if code:
        raise SandboxUnavailable(f"SetEntriesInAclW 失败：{code}")
    try:
        code = _advapi32.SetNamedSecurityInfoW(
            path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, merged, None
        )
    finally:
        _kernel32.LocalFree(merged)
    if code:
        raise SandboxUnavailable(f"SetNamedSecurityInfoW 失败：{code}（路径 {path}）")


def _access_entry(
    sid_text: str, mode: int, *, inheritance: int = OBJECT_INHERIT_ACE,
    permissions: int = GENERIC_READ | GENERIC_WRITE | 0x00010000,
) -> _EXPLICIT_ACCESS_W:
    sid = _sid_from_text(sid_text)
    entry = _EXPLICIT_ACCESS_W()
    entry.grfAccessPermissions = permissions
    entry.grfAccessMode = mode
    entry.grfInheritance = inheritance
    entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
    entry.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
    entry.Trustee.ptstrName = ctypes.cast(ctypes.byref(sid), ctypes.c_void_p)
    # SID 结构要活到 SetEntriesInAclW 调用之后：挂到 entry 上（调用方持有 entry）。
    entry._sid_holder = sid
    return entry


def _set_access(path: str, entry: _EXPLICIT_ACCESS_W) -> None:
    old_acl, descriptor = _current_dacl(path)
    try:
        _write_dacl(path, old_acl, entry)
    finally:
        _kernel32.LocalFree(descriptor)


def _set_object_access(path: str, entry: _EXPLICIT_ACCESS_W) -> None:
    """Merge a noninheritable ACE without propagating existing ACEs to descendants."""
    if entry.grfInheritance:
        raise ValueError("object-only access must not inherit")
    old_acl, descriptor = _current_dacl(path)
    merged = ctypes.c_void_p()
    try:
        code = _advapi32.SetEntriesInAclW(1, ctypes.byref(entry), old_acl, ctypes.byref(merged))
        if code:
            raise SandboxUnavailable(f"SetEntriesInAclW 失败：{code}")
        # SECURITY_DESCRIPTOR (absolute) is 40 bytes on x64, 20 bytes on x86.
        security = ctypes.create_string_buffer(40)
        if not _advapi32.InitializeSecurityDescriptor(security, 1):
            raise SandboxUnavailable(f"InitializeSecurityDescriptor 失败：{ctypes.get_last_error()}")
        if not _advapi32.SetSecurityDescriptorDacl(security, True, merged, False):
            raise SandboxUnavailable(f"SetSecurityDescriptorDacl 失败：{ctypes.get_last_error()}")
        # SetNamedSecurityInfo eagerly reapplies inherited ACEs throughout the tree,
        # even when the added ACE does not inherit. SetFileSecurity changes only this object.
        if not _advapi32.SetFileSecurityW(path, DACL_SECURITY_INFORMATION, security):
            raise SandboxUnavailable(f"SetFileSecurityW 失败：{ctypes.get_last_error()}（路径 {path}）")
    finally:
        if merged.value:
            _kernel32.LocalFree(merged)
        _kernel32.LocalFree(descriptor)


def _grant_ancestor_metadata(path: Path, user_sid: str) -> None:
    """Allow LUA path canonicalization through owner-only ancestors, without writes."""
    metadata = 0x001200A0  # READ_CONTROL, SYNCHRONIZE, READ_ATTRIBUTES, TRAVERSE
    for ancestor in path.parents:
        if _dacl_has_access(str(ancestor), user_sid, ace_type="A", permissions=metadata,
                            require_inheritance=False):
            continue
        owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
        code = _advapi32.GetNamedSecurityInfoW(
            str(ancestor), SE_FILE_OBJECT, 1, ctypes.byref(owner), None, None, None,
            ctypes.byref(descriptor),
        )
        try:
            if code or not owner.value:
                continue
            actual = _sid_to_text(ctypes.cast(owner, ctypes.POINTER(SID)).contents)
            if actual == user_sid:
                _set_object_access(str(ancestor), _access_entry(
                    user_sid, GRANT_ACCESS, inheritance=0, permissions=metadata,
                ))
        finally:
            if descriptor.value:
                _kernel32.LocalFree(descriptor)


def _protected_targets(root: Path, onerror: Callable[[OSError], None]) -> Iterator[Path]:
    """扫描已校验的可写根：复用固定边界、枚举类型与每个候选的实际路径。"""
    context = ProtectedPathContext.current()
    for directory, names, files in os.walk(root, followlinks=False, onerror=onerror):
        current = Path(directory)
        candidates = [(name, True) for name in names]
        candidates.extend((name, False) for name in files if name.casefold() in PROTECTED_SUBPATH_NAMES)
        for name, is_directory in candidates:
            item = current / name
            if item.is_symlink():
                if is_directory:
                    names.remove(name)
                continue
            # os.walk 已给出目录类型，不再 is_dir；解析一次同时用于 junction 与保护判断。
            resolved = item.resolve()
            if is_directory and str(resolved) != os.path.abspath(item):
                names.remove(name)
                continue
            if context.reason_for_resolved(resolved):
                if is_directory:
                    names.remove(name)
                yield item


def grant_write(path: str, sid_text: str) -> GrantReport:
    """授权可写根：根上写一条**可继承** ACE，Windows 自行把它传播到整棵子树。

    **与 2026-10-15 之前的行为差异（用户裁定"照 dsh"）：新目录立即继承可写**，不再需要
    "下一次调用重新扫描后才可写"。换来的是**不再每次调用遍历整棵目录树**：旧实现逐目录写
    ACE（`OBJECT_INHERIT_ACE` 只传给文件，所以每个目录都得自己写一条），实测 6787 个目录的
    工作区上**每条命令**都要付 45 秒左右。dsh 用同一个取舍（`sandbox-windows-acl/README.zh.md`
    的 :55/:102：根上可继承 ACE + 精确 ACE 已在时跳过重写，因为"应用该 ACE 会把相同的 ACE
    急切地重新传播到整棵树——大型工作区上以分钟计"）。

    三层顺序，每一层都有自己的跳过条件：

    1. **进程内缓存**（:data:`_MATERIALIZED_ROOTS`）：同一进程里同一个根只物化一次；
    2. **幂等检查**（:func:`_dacl_mentions_sid`）：根已有该 SID 的完整可继承读写允许项就**不写**——
       跨重启复用常驻 ACE 靠的就是这一步（不写就不会触发急切的整树重传播）；
    3. 真要写时才写，并接受一次性的急切传播。

    **受保护子树仍然单独加拒绝项**：可继承的授权 ACE 会把写权限一并传进 `.git`、凭据目录、
    系统目录，所以这一层不能省。拒绝项也是常驻的，但"哪些子树受保护"要扫一次（每进程每根一次）。
    无法修改 ACL 的普通文件若通过真实令牌只读核验，则保留原 ACL，并随报告交给策略层持续复核。

    Returns:
        :class:`GrantReport`：跳过了哪些目录与原因（调用方**必须**把它说出去，不许静默）。

    Raises:
        SandboxUnavailable: 可写根无法扫描/授权，或无法建立/核验受保护目标的写保护。
    """
    root = Path(path).resolve()
    if protected_target_reason(root):
        raise SandboxUnavailable(f"禁止物化受保护可写根：{root}")
    cache_key = os.path.normcase(str(root))
    if cache_key in _MATERIALIZED_ROOTS:
        return _MATERIALIZED_ROOTS[cache_key]
    user_sid = _current_user_sid_text()
    _grant_ancestor_metadata(root, user_sid)
    skipped: list[SkippedDirectory] = []
    read_only_files: list[str] = []
    inheritable = CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE

    # ① 根：两条可继承 ACE（沙箱 SID + 当前用户；受限令牌不能依赖 OWNER RIGHTS 的隐式所有者权限），
    #    掩码是**读+写**：只写会让沙箱内的命令读不到工作区（`dir` / 管道 / 跑测试全失败），
    #    而读侧本来就不隔离，补读不新增能力。
    #    已有该 SID 的完整可继承读写授权才跳过：重复应用会再次触发急切传播。
    if not _dacl_mentions_sid(str(root), sid_text):
        try:
            _set_access(str(root), _access_entry(user_sid, GRANT_ACCESS, inheritance=inheritable))
            _set_access(str(root), _access_entry(sid_text, GRANT_ACCESS, inheritance=inheritable))
        except (OSError, SandboxUnavailable) as exc:
            # 可写根授权不了 ⇒ 沙箱给不出任何写边界：失败闭合，绝不假装有。
            raise SandboxUnavailable(f"可写根无法授权：{root}（{_skip_reason(exc)}）") from exc

    # ② 受保护子树：扫一次只为**找到它们**，不逐目录授权。
    def onerror(error: OSError) -> None:
        target = Path(str(error.filename or root))
        if _same_directory(target, root):
            raise SandboxUnavailable(f"可写根无法扫描：{root}（{error}）") from error
        skipped.append(SkippedDirectory(str(target), _skip_reason(error)))

    for item in _protected_targets(root, onerror):
        if not _dacl_denies_write(str(item), sid_text):
            try:
                _set_access(str(item), _access_entry(
                    sid_text, DENY_ACCESS,
                    inheritance=inheritable,
                    permissions=GENERIC_WRITE | 0x00010000,  # DELETE 也受保护
                ))
            except (OSError, SandboxUnavailable) as exc:
                # 跨账户缓存文件可能没有 WRITE_DAC，且根的允许项并未传播进去。
                # 不吞掉错误：只有普通文件的实际写/删除/改权限访问均被拒才可保留原 ACL。
                if item.is_file():
                    try:
                        verify_read_only_files((str(item),), writable_sids=(sid_text,))
                    except (OSError, SandboxUnavailable) as verification:
                        raise SandboxUnavailable(
                            f"受保护文件无法建立写拒绝，且只读核验失败：{item}"
                            f"（{_skip_reason(exc)}；{verification}）"
                        ) from verification
                    read_only_files.append(str(item))
                    continue
                # 根的允许项已传播到子树；拒绝项失败不代表“这处写不进去”。
                # 不返回可执行请求，不缓存失败，下一次仍须重新核实保护。
                raise SandboxUnavailable(
                    f"受保护子树无法建立写拒绝，已阻止执行：{item}（{_skip_reason(exc)}）"
                ) from exc
    report = GrantReport(skipped=tuple(skipped), read_only_files=tuple(read_only_files))
    _MATERIALIZED_ROOTS[cache_key] = report
    return report


def grant_read(path: str) -> None:
    """为只读档补齐普通用户的读权限，不添加可写 SID 或写 ACE。"""
    root = str(Path(path).resolve())
    user_sid = _current_user_sid_text()
    _grant_ancestor_metadata(Path(root), user_sid)
    if not _dacl_has_access(root, user_sid, ace_type="A", permissions=GENERIC_READ):
        _set_access(root, _access_entry(
            user_sid, GRANT_ACCESS, inheritance=CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
            permissions=GENERIC_READ,
        ))


def _dacl_sddl(path: str) -> str | None:
    """该对象的 DACL 的 SDDL 文本；读不到返回 ``None``。

    两个幂等检查都基于它：拿文本做判断足够回答"要不要再写一次"，而少一层手写结构体偏移就少一层
    ABI 风险。**读不到一律当"还没写过"**——这条检查失败的代价是"多写一次"，不是"该授权的没授权"。
    """
    try:
        _acl, descriptor = _current_dacl(path)
    except (OSError, SandboxUnavailable):
        return None
    sddl = ctypes.c_wchar_p()
    length = wintypes.ULONG()
    try:
        ok = _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, DACL_SECURITY_INFORMATION, ctypes.byref(sddl), ctypes.byref(length)
        )
        if not ok or not sddl.value:  # pragma: no cover - 极少数无法转换的描述符
            return None
        return sddl.value
    finally:
        if sddl:
            _kernel32.LocalFree(sddl)
        _kernel32.LocalFree(descriptor)


def _dacl_mentions_sid(path: str, sid_text: str) -> bool:
    """精确 SID 是否已有可继承读写允许项；仅提到 SID 不代表授权成立。"""
    return _dacl_has_access(path, sid_text, ace_type="A", permissions=GENERIC_READ | GENERIC_WRITE | 0x00010000)


def _dacl_denies_write(path: str, sid_text: str) -> bool:
    """该对象的 DACL 里是否已经有**针对这条 SID 的拒绝项**（**受保护子树**的幂等判断用）。

    为什么不能沿用"提到过这条 SID"（2026-10-15 修）：可写根上那条可继承的**允许** ACE 会传播到
    受保护子树里，于是子树 DACL 也"提到"了这个 SID——只看提到，就会把"还没加拒绝项"判成"已经加过"，
    凭据目录于是只拿到继承来的**可写**权限（实测：`.git` 下的旧凭据能被改写，而用例此前是因为
    "连工作区都读不到"才恰好没暴露）。

    校验精确 SID、无条件拒绝类型、写/删除掩码与继承范围；读拒绝或条件拒绝不能充当写保护。
    """
    return _dacl_has_access(path, sid_text, ace_type="D", permissions=GENERIC_WRITE | 0x00010000)


def _file_rights(mask: int) -> int:
    # SDDL 可使用通用位或映射后的文件权限；按同一文件对象权限集合比较。
    if mask & 0x10000000:  # GENERIC_ALL
        mask |= 0x001F01FF
    if mask & GENERIC_READ:
        mask |= 0x00120089  # FILE_GENERIC_READ
    if mask & GENERIC_WRITE:
        mask |= 0x00120116  # FILE_GENERIC_WRITE
    return mask & 0x0FFFFFFF


def _dacl_has_access(path: str, sid_text: str, *, ace_type: str, permissions: int,
                     require_inheritance: bool = True) -> bool:
    sddl = _dacl_sddl(path)
    if sddl is None:
        return False
    rights = {"GA": 0x10000000, "GR": GENERIC_READ, "GW": GENERIC_WRITE,
              "GX": 0x20000000, "FA": 0x001F01FF, "FR": 0x00120089,
              "FW": 0x00120116, "FX": 0x001200A0, "SD": 0x00010000,
              "RC": 0x00020000, "WD": 0x00040000, "WO": 0x00080000}
    effective = inherited = 0
    for body in re.findall(r"\(([^()]*)\)", sddl):
        fields = body.split(";")
        if len(fields) != 6 or fields[0] != ace_type or fields[5] != sid_text or fields[3] or fields[4]:
            continue
        flags = fields[1]
        try:
            if fields[2].lower().startswith("0x"):
                mask = int(fields[2], 16)
            else:
                mask = 0
                for index in range(0, len(fields[2]), 2):
                    mask |= rights[fields[2][index:index + 2]]
        except (ValueError, KeyError):
            continue  # 未识别的权限不作为幂等成功依据。
        # Windows 可能把同一次授权拆成“当前对象映射权限”和“仅继承通用权限”两条 ACE。
        if "IO" not in flags:
            effective |= _file_rights(mask)
        if "OI" in flags and "CI" in flags and "NP" not in flags:
            inherited |= _file_rights(mask)
    required = _file_rights(permissions)
    return effective & required == required and (
        not require_inheritance or not Path(path).is_dir() or inherited & required == required
    )


def _same_directory(left: Path, right: Path) -> bool:
    """两个路径是不是同一个目录（大小写、分隔符、符号链接都按 Windows 口径归一）。"""
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except OSError:  # pragma: no cover - realpath 对已存在路径不抛
        return str(left) == str(right)


def _skip_reason(error: BaseException) -> str:
    """跳过原因：先给用户能懂的分类，再附原始信息（便于核对，但不作为唯一说明）。

    "拒绝访问"就是实证里的那一类——目录属主不是当前账户（或被受控文件夹访问拦下），当前账户
    没有 WRITE_DAC，于是 `SetNamedSecurityInfoW` 被拒。三种来源都要认出来，否则这句话会以原始
    错误码的形式摆给用户，他没法自救：
    ``SandboxUnavailable("SetNamedSecurityInfoW 失败：5（路径 …）")``（我们自己的 Win32 包装）、
    ``OSError`` 的 ``winerror``（Windows 上的文件系统调用）、``errno`` 5/13（跨平台的同一含义）。
    """
    text = str(error)
    denied = (
        getattr(error, "winerror", None) == 5
        or getattr(error, "errno", None) in (5, 13)
        or "失败：5（" in text
        or "[WinError 5]" in text
    )
    if denied:
        return "当前账户没有修改该目录权限的权限（Windows 错误 5：拒绝访问）"
    return f"{type(error).__name__}：{text}"


def revoke_write(path: str, sid_text: str) -> None:
    """撤销 ``sid_text`` 在 ``path`` 上的可写授权（临时目录用；工作区 ACE 常驻、不撤）。

    与授权对称：**只在根上撤**——子目录里的 ACE 是从根继承来的，根上那条去掉后由系统一并移除。
    ACE 不存在时是幂等空操作（合并结果与原来等价）。
    """
    root = os.path.normcase(os.path.realpath(path))
    _MATERIALIZED_ROOTS.pop(root, None)
    _set_access(path, _access_entry(sid_text, REVOKE_ACCESS))


# ---- 受限令牌与进程 ------------------------------------------------------------


def _current_user_sid_text() -> str:
    """仅用于可写根的普通用户 ACE，不进入 restricting SID 列表。"""
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise SandboxUnavailable(f"OpenProcessToken 失败：{ctypes.get_last_error()}")
    try:
        size = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(token, 1, buffer, size.value, ctypes.byref(size)):
            raise SandboxUnavailable(f"GetTokenInformation 失败：{ctypes.get_last_error()}")
        sid = ctypes.cast(buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents.Sid
        return _sid_to_text(ctypes.cast(sid, ctypes.POINTER(SID)).contents)
    finally:
        _kernel32.CloseHandle(token)


def _current_logon_sid_pointer() -> tuple[ctypes.c_void_p, ctypes.Array]:
    """从 TokenGroups 读取 SE_GROUP_LOGON_ID，绝不使用 TokenUser 作为写授权来源。"""
    token = wintypes.HANDLE()
    access = (
        TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT
        | TOKEN_ADJUST_SESSIONID
    )
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), access, ctypes.byref(token)):
        raise SandboxUnavailable(f"OpenProcessToken 失败：{ctypes.get_last_error()}")
    size = wintypes.DWORD()
    _advapi32.GetTokenInformation(token, 2, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not _advapi32.GetTokenInformation(token, 2, buffer, size.value, ctypes.byref(size)):
        _kernel32.CloseHandle(token)
        raise SandboxUnavailable(f"GetTokenInformation 失败：{ctypes.get_last_error()}")
    _kernel32.CloseHandle(token)
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    groups = ctypes.cast(
        ctypes.addressof(buffer) + _TOKEN_GROUPS.Groups.offset,
        ctypes.POINTER(SID_AND_ATTRIBUTES),
    )
    for index in range(count):
        if groups[index].Attributes & 0xC0000000 == 0xC0000000:
            return ctypes.c_void_p(groups[index].Sid), buffer
    raise SandboxUnavailable("当前令牌缺少登录 SID（SE_GROUP_LOGON_ID）")


def _set_default_dacl(token: int, logon: ctypes.c_void_p) -> None:
    """让子进程创建的内核对象能被登录 SID 重新打开，避免 DLL 初始化失败。

    修改的仅是新受限令牌的 TokenDefaultDacl；不向磁盘目录添加登录 SID 的写权限。
    """
    size = wintypes.DWORD()
    _advapi32.GetTokenInformation(token, 6, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not _advapi32.GetTokenInformation(token, 6, buffer, size.value, ctypes.byref(size)):
        raise SandboxUnavailable(f"读取 TokenDefaultDacl 失败：{ctypes.get_last_error()}")
    old_acl = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
    entry = _EXPLICIT_ACCESS_W()
    entry.grfAccessPermissions = 0x10000000  # GENERIC_ALL，仅用于令牌默认内核对象 DACL
    entry.grfAccessMode = GRANT_ACCESS
    entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
    entry.Trustee.ptstrName = logon
    merged = ctypes.c_void_p()
    code = _advapi32.SetEntriesInAclW(1, ctypes.byref(entry), old_acl, ctypes.byref(merged))
    if code:
        raise SandboxUnavailable(f"合并 TokenDefaultDacl 失败：{code}")
    try:
        if not _advapi32.SetTokenInformation(token, 6, ctypes.byref(merged), ctypes.sizeof(merged)):
            raise SandboxUnavailable(f"设置 TokenDefaultDacl 失败：{ctypes.get_last_error()}")
    finally:
        _kernel32.LocalFree(merged)


def restricted_token(*, writable_sids: tuple[str, ...] = ()) -> int:
    """构造受限令牌：``WRITE_RESTRICTED`` + restricting =「登录 SID + Everyone + 各可写 SID」。

    登录 SID 配合 TokenDefaultDacl 允许子进程访问自身新建的内核对象；
    Everyone 的已有授权边界见模块说明。

    Returns:
        令牌句柄（调用方负责 ``CloseHandle``）。
    """
    logon, logon_buffer = _current_logon_sid_pointer()
    everyone = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW("S-1-1-0", ctypes.byref(everyone)):
        raise SandboxUnavailable(f"取 Everyone SID 失败：{ctypes.get_last_error()}")

    holding = [logon_buffer, everyone]
    entries = [SID_AND_ATTRIBUTES(logon, 0), SID_AND_ATTRIBUTES(everyone, 0)]
    for text in (*writable_sids, NATIVE_RUNTIME_GUARD_SID):
        sid = _sid_from_text(text)
        sid_buffer = ctypes.create_string_buffer(bytes(sid), ctypes.sizeof(SID))
        holding.append(sid_buffer)
        entries.append(
            SID_AND_ATTRIBUTES(ctypes.cast(sid_buffer, ctypes.c_void_p), 0)
        )

    array = (SID_AND_ATTRIBUTES * len(entries))(*entries)

    token = wintypes.HANDLE()
    access = (
        TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT
        | TOKEN_ADJUST_SESSIONID
    )
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), access, ctypes.byref(token)):
        raise SandboxUnavailable(f"OpenProcessToken 失败：{ctypes.get_last_error()}")
    restricted = wintypes.HANDLE()
    ok = _advapi32.CreateRestrictedToken(
        token,
        DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED,
        0,
        None,
        0,
        None,
        len(entries),
        array,
        ctypes.byref(restricted),
    )
    _kernel32.CloseHandle(token)
    _kernel32.LocalFree(everyone)
    restricted_handle = restricted.value
    if not ok or restricted_handle is None:
        raise SandboxUnavailable(f"CreateRestrictedToken 失败：{ctypes.get_last_error()}")
    try:
        _set_default_dacl(restricted_handle, logon)
    except BaseException:
        _kernel32.CloseHandle(restricted)
        raise
    del holding  # 令牌已建好，缓冲区可以释放
    return restricted_handle


def _access_allowed(path: str, token: int, permissions: int) -> bool:
    """让 Windows 核验指定访问位；不打开写句柄、不写文件，也不改变线程身份。"""
    descriptor = ctypes.c_void_p()
    code = _advapi32.GetNamedSecurityInfoW(
        path, SE_FILE_OBJECT, 0x7, None, None, None, None, ctypes.byref(descriptor),
    )  # OWNER | GROUP | DACL；AccessCheck 要求完整的 owner/group。
    if code:
        raise SandboxUnavailable(f"读取访问检查描述符失败：{code}（路径 {path}）")
    try:
        mapping = _GENERIC_MAPPING(0x00120089, 0x00120116, 0x001200A0, 0x001F01FF)
        size = wintypes.DWORD(1024)
        for _ in range(2):
            privileges = ctypes.create_string_buffer(size.value)
            granted = wintypes.DWORD()
            allowed = wintypes.BOOL()
            if _advapi32.AccessCheck(
                descriptor, token, permissions, ctypes.byref(mapping), privileges,
                ctypes.byref(size), ctypes.byref(granted), ctypes.byref(allowed),
            ):
                return bool(allowed.value)
            error = ctypes.get_last_error()
            if error != 122 or not 0 < size.value <= 65536:  # ERROR_INSUFFICIENT_BUFFER
                raise SandboxUnavailable(f"AccessCheck 失败：{error}（路径 {path}）")
        raise SandboxUnavailable(f"AccessCheck 权限缓冲区反复变化：{path}")
    finally:
        _kernel32.LocalFree(descriptor)


def verify_read_only_files(paths: tuple[str, ...], *, writable_sids: tuple[str, ...]) -> None:
    """无法设置拒绝项的普通文件必须在本次完整受限令牌下保持不可变更。

    每个位单独检查，不能用一次 GENERIC_WRITE 失败推断所有变更都被拒。
    同时检查父目录 FILE_DELETE_CHILD，避免通过父目录删除/替换该文件。
    目录不适用此回退（其后代可能另有授权）；任何 API 错误均阻止执行。
    """
    if not paths:
        return
    primary = restricted_token(writable_sids=writable_sids)
    impersonation = wintypes.HANDLE()
    try:
        if not _advapi32.DuplicateToken(primary, 2, ctypes.byref(impersonation)):
            raise SandboxUnavailable(f"DuplicateToken 失败：{ctypes.get_last_error()}")
        token = impersonation.value
        if token is None:
            raise SandboxUnavailable("DuplicateToken 未返回令牌")
        for path in dict.fromkeys(paths):
            target = Path(path)
            if not target.is_file() or target.is_symlink():
                raise SandboxUnavailable(f"只读核验目标不是普通文件：{path}")
            for permission in (0x2, 0x4, 0x10, 0x100, 0x10000, 0x40000, 0x80000):
                if _access_allowed(path, token, permission):
                    raise SandboxUnavailable(f"受保护文件仍可变更：{path}（访问位 {permission:#x}）")
            if _access_allowed(str(target.parent), token, 0x40):
                raise SandboxUnavailable(f"父目录仍允许删除受保护文件：{path}")
    finally:
        if impersonation.value:
            _kernel32.CloseHandle(impersonation)
        _kernel32.CloseHandle(primary)


@dataclass(frozen=True)
class SandboxRun:
    """一次受限执行的结果（只报事实，不伪造"已停止/成功"）。"""

    exit_code: int
    stdout: bytes
    stderr: bytes
    job_error: int | None = None


def _attach_kill_on_close_job(process_handle: int) -> int | None:
    """把进程装进 ``KILL_ON_JOB_CLOSE`` 的 Job；失败返回 Win32 错误码（不致命，如实上报）。

    **句柄故意不关**：它随持有它的进程（runner）一起被系统关闭，那一刻 Job 里的整棵进程树被收走——
    "runner 死了，子孙也不留"就是靠这一条，不依赖 ``taskkill`` 的尽力而为。
    """
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        return ctypes.get_last_error()
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(
        job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return ctypes.get_last_error()
    if not _kernel32.AssignProcessToJobObject(job, process_handle):
        return ctypes.get_last_error()
    _LIVE_JOBS.append(int(job))
    return None


def _release_completed_jobs() -> None:
    """命令正常结束后保留主动启动的后台服务；异常退出仍由 Job 清理整棵树。"""
    for job in tuple(_LIVE_JOBS):
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not _kernel32.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise SandboxUnavailable(f"无法保留后台进程：{ctypes.get_last_error()}")
        _kernel32.CloseHandle(job)
        _LIVE_JOBS.remove(job)


def sandbox_temp_dir(workspace_root: str | os.PathLike[str]) -> Path:
    """该工作区**专用**的可写临时目录（沙箱内命令的 `TMP`/`TEMP` 指向这里）。

    为什么需要它：写限制只认单独授权的位置，连 `%TEMP%` 都写不进去——而很多命令要写临时文件，
    没有这一格它们会成片失败（**如实失败**，不是静默降级，所以可用性上必须补）。

    位置在系统临时目录下，按工作区路径派生一个稳定子目录名：不同工作区互不干扰，同一个工作区每次
    拿到同一个路径（授权是落在目录上的 ACL，路径漂移会让整树传播每次重做）。**建目录与授权由调用方
    负责**（见 :meth:`local_agent.security.policy.SecurityPolicy.shell_sandbox_request`）。
    """
    digest = hashlib.sha256(str(Path(workspace_root)).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "local-agent-sandbox" / digest


def is_supported() -> bool:
    """当前进程能不能用本机制：Windows 且两块 Win32 库都取到了。"""
    return sys.platform == "win32" and _kernel32 is not None and _advapi32 is not None


def runner_argv(command: list[str], *, writable_sids: tuple[str, ...] = ()) -> list[str]:
    """把 ``command`` 包成"经本模块 runner 启动"的 argv。

    **只换"怎么起"**：调用方给的 argv 原样放在 ``--`` 之后，一个字符都不改，因此工具契约、结果形状、
    退出码语义全部不变（§20.1 的核心约束）。runner 自己失败时以 127 退出并打 ``windows-acl-run:``，
    调用方据此能分清"runner 失败"与"命令被沙箱拒绝"。
    """
    from local_agent.security.win_ipc import prepare_bundle

    ipc_directory = prepare_bundle()
    prefix = ([sys.executable, "--internal-win-sandbox-run"] if getattr(sys, "frozen", False)
              else [sys.executable, "-I", "-m", "local_agent.security.win_sandbox"])
    prefix += ["--ipc-dir", str(ipc_directory)]
    for sid in writable_sids:
        prefix += ["--sid", sid]
    return [*prefix, "--", *(str(item) for item in command)]


def main(argv: list[str] | None = None) -> int:
    """argv 前缀 runner：``python -m local_agent.security.win_sandbox [--sid <SID>]… -- <argv…>``。

    形态与 ``bwrap`` / ``landlock-run`` / ``sandbox-exec`` 同类：``process_guard`` 只是在子进程命令的
    argv **前面**插这条前缀，因此**工具契约与结果形状一字不变**（§20.1 的核心约束），换的只是"怎么起"。

    - 退出码：镜像被包装命令的退出码；runner 自身失败（拿不到令牌、起不来进程）**以 127 退出**并打
      ``windows-acl-run: <详情>``——按 dsh 的约定，127 这个标记让调用方**永远不会把 runner 失败**误读成
      "命令被沙箱拒绝"；更关键的是**不裸跑**（那是静默降级）。
    - stdout/stderr：把被包装命令的输出原样转发，父进程看到的就是命令自己的输出。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    ipc_directory: Path | None = None
    if args and args[0] == "--ipc-dir":
        if len(args) < 2:
            print("windows-acl-run: --ipc-dir 后面缺少路径", file=sys.stderr)
            return 127
        ipc_directory = Path(args[1])
        del args[:2]
    sids: list[str] = []
    while args and args[0] == "--sid":
        args.pop(0)
        if not args:
            print("windows-acl-run: --sid 后面缺少 SID", file=sys.stderr)
            return 127
        sids.append(args.pop(0))
    if args and args[0] == "--":
        args.pop(0)
    if not args:
        print("windows-acl-run: 缺少要执行的命令（用法：--sid <SID> -- <argv…>）", file=sys.stderr)
        return 127
    try:
        def forward(data: bytes) -> None:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

        run = run_sandboxed(args, writable_sids=tuple(sids), on_stdout=forward,
                            ipc_directory=ipc_directory)
    except SandboxUnavailable as exc:
        print(f"windows-acl-run: {exc}", file=sys.stderr)
        return 127
    if run.job_error is not None:
        # 如实上报：Job 没装上意味着"runner 死了子孙还在"，但命令本身已经跑完，不能因此谎报失败。
        print(f"windows-acl-run: 无法装 Job Object（{run.job_error}）", file=sys.stderr)
    # stderr 走自己的通道（形状与不进沙箱时一致）；`run.stderr` 只在"拿不到 stderr 句柄"的兜底路径
    # 下才会有内容（那时它已被并进 stdout，这里为空）。
    if run.stderr:
        sys.stderr.buffer.write(run.stderr)
        sys.stderr.buffer.flush()
    return run.exit_code


def _stderr_target(fallback: wintypes.HANDLE) -> wintypes.HANDLE:
    """子进程的 stderr 该往哪写：**runner 自己的 stderr 句柄**，拿不到就退回 ``fallback``。

    2026-10-15 之前这里直接把 ``hStdError`` 指到 stdout 那条管道，于是"沙箱内"的命令 stderr 全落进
    stdout——结果形状与不进沙箱时不一致，而模块自己声称"工具契约与结果形状一字不变（§20.1）"。
    真实后果：``python -B -m unittest`` 的 "Ran N tests … OK" 本来写在 stderr，受控代码工作流与
    workflow_eval 都据它判定"测试真的跑过"，在默认档下因此永远判失败。

    为什么不另开一条管道：父进程（``process_guard``）本来就是分开收集 runner 的 stdout/stderr 的，
    把 runner 的 stderr 直接继承下去即可，既不用第二条管道也不用读线程（单管道顺序读到 EOF 的实现
    在多管道下会死锁）。诊断信息（"无法装 Job Object"）本来就走 stderr，与命令自身的 stderr 同路。
    """
    return _inherited_standard_handle(STD_ERROR_HANDLE, fallback)


def _inherited_standard_handle(kind: int, fallback: wintypes.HANDLE) -> wintypes.HANDLE:
    """继承 runner 的标准通道；stdin 必须保持 ProcessRegistry 的 DEVNULL/EOF 契约。"""
    handle = _kernel32.GetStdHandle(kind)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return fallback
    # 标准句柄常常是不可继承的（父进程怎么给的由父进程决定）：这里显式打开继承位，失败就退回。
    if not _kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT):
        return fallback
    return handle


def run_sandboxed(
    command: list[str], *, writable_sids: tuple[str, ...],
    on_stdout: Callable[[bytes], None] | None = None,
    ipc_directory: Path | None = None,
) -> SandboxRun:
    """在受限令牌下起一个进程，用**匿名管道**收 stdout，stderr 继承 runner 的 stderr（v2 实测可用）。

    ``SandboxRun.stderr`` 因此**恒为空**：stderr 不经过这条管道，而是被被包装进程直接写进 runner 的
    stderr（父进程分开收集——``main`` 也照此转发）。形状与不进沙箱时一致。

    Raises:
        SandboxUnavailable: 建令牌、建管道或起进程失败——**不降级裸跑**。
    """
    from local_agent.security import win_ipc

    directory = ipc_directory if ipc_directory is not None else win_ipc.prepare_bundle()
    token = restricted_token(writable_sids=writable_sids)
    attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
    read_end = wintypes.HANDLE()
    write_end = wintypes.HANDLE()
    if not _kernel32.CreatePipe(
        ctypes.byref(read_end), ctypes.byref(write_end), ctypes.byref(attributes), 0
    ):
        _kernel32.CloseHandle(token)
        raise SandboxUnavailable(f"CreatePipe 失败：{ctypes.get_last_error()}")
    _kernel32.SetHandleInformation(read_end, HANDLE_FLAG_INHERIT, 0)

    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(_STARTUPINFOW)
    startup.dwFlags = STARTF_USESTDHANDLES
    startup.hStdInput = _inherited_standard_handle(STD_INPUT_HANDLE, wintypes.HANDLE())
    startup.hStdOutput = write_end
    startup.hStdError = _stderr_target(write_end)
    info = _PROCESS_INFORMATION()
    # **必须按 Windows 引号规则拼**：直接 `" ".join(argv)` 会把带空格的参数拆散——真实踩到过：
    # `python -c "import subprocess, sys; ..."` 的代码串被空格切开，子进程报 `SyntaxError:
    # Expected one or more names after 'import'`。`list2cmdline` 就是 MSVCRT 那套规则的实现。
    command_line = ctypes.create_unicode_buffer(windows_command_line(command))
    ok = _advapi32.CreateProcessAsUserW(
        token, None, command_line, None, None, True,
        CREATE_NO_WINDOW | CREATE_SUSPENDED, None, None,
        ctypes.byref(startup), ctypes.byref(info),
    )
    _kernel32.CloseHandle(write_end)
    _kernel32.CloseHandle(token)
    if not ok:
        error = ctypes.get_last_error()
        _kernel32.CloseHandle(read_end)
        raise SandboxUnavailable(f"CreateProcessAsUserW 失败：{error}")

    # **挂起态起、装完 Job 再放行**（2026-10-14 实测修正）。原顺序是"起来之后再 Assign"，那段窗口里
    # 子进程已经在跑、甚至已派生出孙进程——孙进程落在 Job 之外，"runner 死了子孙还在"于是实测复现。
    # 挂起态把这个窗口关死：子进程一行代码都没执行，Job 已经装好。
    job_error = _attach_kill_on_close_job(info.hProcess)
    try:
        win_ipc.attach(int(info.dwProcessId), info.hProcess, directory)
    except BaseException:
        _kernel32.TerminateProcess(info.hProcess, 127)
        _kernel32.CloseHandle(info.hThread)
        _kernel32.CloseHandle(info.hProcess)
        _kernel32.CloseHandle(read_end)
        raise
    if _kernel32.ResumeThread(info.hThread) == 0xFFFFFFFF:
        error = ctypes.get_last_error()
        _kernel32.TerminateProcess(info.hProcess, 127)
        _kernel32.CloseHandle(info.hThread)
        _kernel32.CloseHandle(info.hProcess)
        _kernel32.CloseHandle(read_end)
        # 拿不回线程就不放行：进程仍在挂起、且在 Job 里，Job 句柄随本进程退出而关闭 → 被收走。
        raise SandboxUnavailable(f"ResumeThread 失败：{error}")

    chunks: list[bytes] = []
    buffer = ctypes.create_string_buffer(4096)
    read = wintypes.DWORD()
    while True:
        available = wintypes.DWORD()
        if not _kernel32.PeekNamedPipe(read_end, None, 0, None, ctypes.byref(available), None):
            break
        if available.value:
            if not _kernel32.ReadFile(read_end, buffer, min(4096, available.value),
                                      ctypes.byref(read), None):
                break
            data = buffer.raw[:read.value]
            if on_stdout is None:
                chunks.append(data)
            else:
                on_stdout(data)
        elif _kernel32.WaitForSingleObject(info.hProcess, 0) == 0:
            break
        else:
            time.sleep(0.02)
    _kernel32.CloseHandle(read_end)
    _kernel32.WaitForSingleObject(info.hProcess, INFINITE)
    code = wintypes.DWORD()
    _kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    _kernel32.CloseHandle(info.hThread)
    _kernel32.CloseHandle(info.hProcess)
    if writable_sids:
        # 仅在正常跑完直接命令后解除退出清理。只读子任务仍在 runner 退出时收走后代。
        _release_completed_jobs()
    return SandboxRun(exit_code=int(code.value), stdout=b"".join(chunks), stderr=b"", job_error=job_error)


if __name__ == "__main__":  # pragma: no cover - 由 process_guard 以前缀方式调用
    sys.exit(main())
