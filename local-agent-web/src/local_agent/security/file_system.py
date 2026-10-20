"""文件工具的统一策略后端；可信进程内检查，不声称提供 OS 级读隔离。

与 Shell 共用本轮 SecurityPolicy。读取允许显式本机外部路径，保留敏感文件与委派范围限制；变更在实际 I/O
前重新解析目标、复核档位与批准。原子发布前再查目标身份和文件版本，缩小检查与使用间的
时间窗，但不能防御宿主进程在系统调用之间恶意替换路径。任意命令仍由原生写侧沙箱约束。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from local_agent.exceptions import FileOperationError, SecurityError, WorkspaceBoundaryError
from local_agent.security.path_guard import is_sensitive_path, is_within, resolve_within_workspace
from local_agent.trash import move_to_trash

if TYPE_CHECKING:
    from local_agent.security.policy import SecurityPolicy


@dataclass(frozen=True)
class DeletedTarget:
    path: Path
    kind: Literal["file", "dir"]
    files: int
    directories: int
    #: 是否移进了回收站（2026-10-20，安全删除开启时）；为 `True` 时 `trash_path` 是它现在的位置。
    trashed: bool = False
    trash_path: str | None = None


class FileSystemSandbox:
    """统一承载文件工具的读、枚举、新建、替换与删除；策略始终来自所属请求。"""

    def __init__(self, policy: SecurityPolicy) -> None:
        self.policy = policy
        self._mutation_lock = threading.RLock()

    def check_cancelled(self) -> None:
        token = self.policy.cancellation
        if token is not None and token.cancelled:
            raise SecurityError(token.refusal_reason())

    def resolve(self, path: str | Path) -> Path:
        self.check_cancelled()
        return self.policy.resolve_path(str(path))

    def directory(self, path: str | Path) -> Path:
        target = self.resolve(path)
        if not target.is_dir():
            raise FileOperationError(f"目标不是目录或不存在：{target.as_posix()}")
        return target

    def search_root(self, path: str) -> Path:
        """默认从工作区检索；明确的本机外部绝对路径与文件读取共用解析策略。"""
        target = self.resolve(path)
        if not target.is_dir() and not target.is_file():
            raise ValueError(f"检索路径不存在（既不是目录也不是文件）：{path}")
        return target

    def read_bytes(self, path: str | Path, *, max_bytes: int | None = None) -> bytes:
        self.check_cancelled()
        target = self.policy.resolve_readable_file(str(path))
        with target.open("rb") as handle:
            data = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        self.check_cancelled()
        return data

    def read_prefix_and_sha256(self, path: str | Path, max_bytes: int) -> tuple[bytes, str, bool]:
        """有界保留正文前缀，流式计算完整文件版本；取消时停止后续读取。"""
        self.check_cancelled()
        target = self.policy.resolve_readable_file(str(path))
        digest = hashlib.sha256()
        prefix = bytearray()
        total = 0
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                self.check_cancelled()
                digest.update(chunk)
                total += len(chunk)
                remaining = max_bytes - len(prefix)
                if remaining > 0:
                    prefix.extend(chunk[:remaining])
        self.check_cancelled()
        return bytes(prefix), digest.hexdigest(), total > max_bytes

    def iter_bytes(self, path: str | Path) -> Iterator[bytes]:
        """在统一路径策略下逐块读取；每块复核取消，不把大文件整体放进内存。"""
        self.check_cancelled()
        target = self.policy.resolve_readable_file(str(path))
        with target.open("rb") as handle:
            while chunk := handle.read(65536):
                self.check_cancelled()
                yield chunk
        self.check_cancelled()

    def children(self, path: str | Path, limit: int) -> tuple[list[Path], bool]:
        target = self.directory(path)
        items: list[Path] = []
        try:
            for child in target.iterdir():
                self.check_cancelled()
                items.append(child)
                if len(items) > limit:
                    return items, True
        except OSError:
            # 保留列目录原来的不可读子目录处理，不吞权限拒绝和取消。
            pass
        return items, False

    def stat(self, path: str | Path) -> os.stat_result:
        """元数据也校验路径策略；委派范围限制不能借链接或 stat 绕过。"""
        return self.resolve(path).stat()

    def exists(self, path: str | Path) -> bool:
        return self.resolve(path).exists()

    def is_directory(self, path: str | Path) -> bool:
        try:
            return self.resolve(path).is_dir()
        except WorkspaceBoundaryError:
            return False

    def entry(self, path: Path) -> tuple[Literal["file", "dir", "symlink", "other"], os.stat_result | None]:
        self.directory(path.parent)
        linked = path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
        try:
            info = self.stat(path)
        except (OSError, WorkspaceBoundaryError):
            return ("symlink" if linked else "other"), None
        kind: Literal["file", "dir", "symlink", "other"] = "other"
        if linked:
            kind = "symlink"
        elif stat.S_ISDIR(info.st_mode):
            kind = "dir"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        return kind, info

    def search_files(self, root: Path) -> Iterator[Path]:
        from local_agent.rag.walk import iter_candidate_files

        checked = self.search_root(str(root))
        if checked.is_file():
            if not checked.is_symlink() and not is_sensitive_path(checked):
                yield checked
            return
        ignore_root = self.policy.workspace_root if is_within(checked, self.policy.workspace_root) else checked
        for candidate in iter_candidate_files(checked, workspace_root=ignore_root):
            self.check_cancelled()
            # 允许明确读取外部路径，不等于允许搜索跟随链接离开本次选定的根。
            fresh = resolve_within_workspace(str(candidate), base_dir=checked, workspace_root=checked)
            if not is_sensitive_path(fresh):
                yield fresh

    def mutation_target(self, tool_name: str, arguments: Mapping[str, object]) -> Path:
        from local_agent.security.approval import action_fingerprint

        self.check_cancelled()
        if tool_name not in {"write_file", "edit_file", "delete_file"}:
            raise SecurityError(f"不支持的文件变更：{tool_name}")
        raw = str(arguments.get("path") or "")
        target = self.policy.resolve_writable_path(
            raw, verb={"write_file": "写入", "edit_file": "修改", "delete_file": "删除"}[tool_name],
        )
        decision = self.policy.classify_tool(tool_name, arguments)
        if not decision.allowed and (
            not decision.requires_approval or not self.policy.is_granted(
                action_fingerprint(tool_name, arguments, policy=self.policy)
            )
        ):
            if decision.reason_kind == "input":
                raise FileOperationError(decision.reason)
            raise SecurityError(decision.reason)
        return target

    def _recheck(self, expected: Path, tool_name: str, arguments: Mapping[str, object]) -> None:
        if self.mutation_target(tool_name, arguments) != expected:
            raise SecurityError("文件目标已变化：请重新读取并重新发起操作")

    def create_file(self, arguments: Mapping[str, object], data: bytes) -> tuple[Path, list[str]]:
        with self._mutation_lock:
            target = self.mutation_target("write_file", arguments)
            self._check_size(data)
            created = self._create_parents(target.parent, arguments)
            self._recheck(target, "write_file", arguments)
            _create_new_file(target, data)
            return target, created

    def replace_file(
        self, tool_name: str, arguments: Mapping[str, object], data: bytes,
        *, expected_sha256: str | None = None,
    ) -> Path:
        if tool_name not in {"write_file", "edit_file"}:
            raise SecurityError("原子替换只接受写入或编辑动作")
        if tool_name == "write_file" and not arguments.get("overwrite"):
            raise SecurityError("替换已有文件必须显式设置 overwrite=true 并获得批准")
        if tool_name == "edit_file" and not expected_sha256:
            raise FileOperationError("修改已有文件必须提供 expected_sha256：请先读取文件")
        with self._mutation_lock:
            target = self.mutation_target(tool_name, arguments)
            if tool_name == "write_file":
                self._check_size(data)
            original_mode = stat.S_IMODE(target.stat().st_mode)
            descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                os.chmod(temporary, original_mode)
                self._recheck(target, tool_name, arguments)
                if expected_sha256 is not None:
                    _, current, _ = self.read_prefix_and_sha256(target, 0)
                    if current != expected_sha256:
                        raise FileOperationError("expected_sha256 与当前文件不一致：文件已被改动，请重新读取后再修改")
                os.replace(temporary, target)
            except BaseException:
                # 不追随被宿主并发替换的父目录去删除其他位置的同名文件。
                if temporary.resolve() == temporary:
                    temporary.unlink(missing_ok=True)
                raise
            return target

    def _check_size(self, data: bytes) -> None:
        maximum = self.policy.workspace.max_file_size_bytes
        if len(data) > maximum:
            raise FileOperationError(f"内容 {len(data)} 字节超过上限 {maximum} 字节，已拒绝写入")

    def _create_parents(self, directory: Path, arguments: Mapping[str, object]) -> list[str]:
        from local_agent.security.path_guard import display_path

        missing: list[Path] = []
        cursor = directory
        while not cursor.exists():
            missing.append(cursor)
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        created: list[str] = []
        for item in reversed(missing):
            self.mutation_target("write_file", arguments)
            checked = self.policy.resolve_writable_path(str(item))
            if checked != item:
                raise SecurityError("父目录目标已变化：请重新发起写入")
            checked.mkdir(exist_ok=True)
            created.append(display_path(checked, self.policy.workspace_root))
        return created

    def delete(self, arguments: Mapping[str, object]) -> DeletedTarget:
        with self._mutation_lock:
            target = self.mutation_target("delete_file", arguments)
            if target == self.policy.workspace_root.resolve():
                raise SecurityError("禁止删除工作区根目录")
            if not target.exists():
                raise SecurityError(f"目标不存在：{target.as_posix()}")
            if not target.is_dir():
                self._recheck(target, "delete_file", arguments)
                return self._remove(target, "file", 1, 0)
            if not arguments.get("recursive"):
                raise SecurityError("删除目录必须显式设置 recursive=true")

            files = directories = 0

            def fail_scan(error: OSError) -> None:
                raise error

            # 先检查完整范围再删除；父目录获批不能连带删除凭据、Git 元数据或外部链接目标。
            for directory, names, leaves in os.walk(target, followlinks=False, onerror=fail_scan):
                for name in [*names, *leaves]:
                    self.check_cancelled()
                    item = Path(directory) / name
                    checked = self.policy.resolve_writable_path(str(item))
                    if checked != item:
                        raise SecurityError("递归删除包含路径链接，已拒绝删除")
                    if checked.is_dir():
                        directories += 1
                    else:
                        files += 1
            self._recheck(target, "delete_file", arguments)
            return self._remove(target, "dir", files, directories)

    def _remove(
        self, target: Path, kind: Literal["file", "dir"], files: int, directories: int
    ) -> DeletedTarget:
        """真正移除目标：安全删除开着就**移进回收站**，否则按旧行为删掉（2026-10-20）。

        回收站里保留原有结构（目录整体移动），所以"拖回去"就能恢复；移动失败会抛错，
        绝不回退成直接删除——用户开着这个开关，要的就是"宁可失败也别丢"。
        """
        if self.policy.safe_delete:
            moved = move_to_trash(target, root=self.policy.trash_root)
            return DeletedTarget(
                target, kind, files, directories, trashed=True, trash_path=str(moved)
            )
        if kind == "dir":
            shutil.rmtree(target)
        else:
            target.unlink()
        return DeletedTarget(target, kind, files, directories)

    def result_path(self, path: Path) -> Path:
        """显式检索落盘的内部目录；不接受任意用户文件作为缓存清理目标。"""
        self.policy.require_result_spill()
        root = self.policy.workspace_root.resolve()
        owned = root / ".local-agent" / "results"
        if owned.resolve() != owned:
            raise SecurityError("检索结果目录不能通过链接重定向")
        resolve_within_workspace(str(path), base_dir=root, workspace_root=root)
        checked = resolve_within_workspace(str(path), base_dir=owned, workspace_root=owned)
        return self.policy.resolve_writable_path(str(checked))

    def describe_delete(self, path: Path, recursive: bool, limit: int) -> str:
        target = self.resolve(path)
        if not target.exists():
            return "目标不存在"
        if target.is_file():
            return f"文件，{self.stat(target).st_size} 字节"
        if not recursive:
            return "目录（未开启递归，不会删除）"
        files = directories = 0
        for directory, names, leaves in os.walk(target, followlinks=False):
            for name in [*names, *leaves]:
                item = Path(directory) / name
                self.check_cancelled()
                if self.is_directory(item):
                    directories += 1
                else:
                    files += 1
                if files + directories >= limit:
                    return (
                        f"目录，至少含 {files} 个文件与 {directories} 个子目录"
                        "（已达扫描上限，影响范围为有界摘要；递归删除需要二次确认）"
                    )
        return f"目录，含 {files} 个文件与 {directories} 个子目录（递归删除需要二次确认）"


def _create_new_file(path: Path, data: bytes) -> None:
    """以 O_EXCL 原子新建，检查后出现的文件也不会被覆盖。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise FileOperationError(f"文件已存在：{path.as_posix()}。写入过程中它被创建或改动过，已拒绝覆盖（改现有文件请用 edit_file）") from exc
    except OSError as exc:
        raise FileOperationError(f"创建文件失败（{type(exc).__name__}）：{exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except BaseException:
        if path.resolve() == path:
            path.unlink(missing_ok=True)
        raise
