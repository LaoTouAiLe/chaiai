"""常驻记忆的文件存储：``MEMORY.md`` / ``USER.md``（架构 §10.7，2026-10-27）。

三条硬约束写在最前面，改实现前先读：

1. **正文是真源**：这两份文件就是常驻记忆的权威副本，不是库内记录的导出。因此读失败必须报错，
   不能当成"空"再回写（那会把用户已有内容抹掉）。
2. **上限只在一次写入的最终结果上校验**：溢出时拒绝并回传当前条目，由模型在同一个 batch 里
   删旧/缩短再添加；**加载时超限只告警、不截断**——"拒绝新增"与"删掉用户内容"是两件事。
3. **写入与读取都要能解释失败**：外部漂移（文件被手改成没有 ``§`` 分隔的整段）时拒绝覆盖并留
   ``.bak``；命中注入模式时拒绝写入；加载命中的条目只在**快照**里换成占位，原文留在文件里。

与参照实现的差异（如实记录）：锁只有**进程内**（``threading``）+ 原子替换。本项目是单服务进程
（§15.1 明确不支持多 worker），跨进程并发写只在"服务 + 同时跑 CLI"这种场景出现，代价是后者可能
覆盖前者的一次写入；没有引入 ``msvcrt``/``fcntl`` 的文件锁。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from local_agent.config import CuratedMemoryConfig
from local_agent.security import threat_scan

#: 条目分隔符：与参照实现一致，用段落符号而不是换行——条目可以多行。
ENTRY_DELIMITER: Final[str] = "\n§\n"

#: 注入时的块表头（带使用率，让模型知道还剩多少空间）。
BLOCK_HEADERS: Final[dict[str, str]] = {
    "memory": "MEMORY（我的笔记：环境、约定、教训）",
    "user": "USER PROFILE（用户画像：身份、偏好、风格）",
}

#: 目标 → 文件名。
_FILENAMES: Final[dict[str, str]] = {"memory": "MEMORY.md", "user": "USER.md"}

#: 进程内锁：按文件路径串行化"读-改-写"，避免两个并发写入各自基于旧快照覆盖对方。
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def resolve_directory(config: CuratedMemoryConfig) -> Path:
    """配置里的目录：绝对路径照用，相对路径拼在**数据根**上（§8 的口径）。

    正常路径下 ``api.container.resolve_data_paths`` 已经把它变成绝对路径；这里兜住"直接构造
    ``AppConfig`` 的脚本与测试"，否则它们会写进进程当前目录——换一个目录启动就换一套记忆。
    """
    from local_agent import app_paths  # 延迟导入：app_paths 会读环境变量，模块导入期不该触发

    candidate = Path(config.path).expanduser()
    return candidate if candidate.is_absolute() else app_paths.data_root() / candidate


@dataclass
class CuratedStore:
    """一份常驻记忆目录的读写句柄；``load()`` 后可用 :meth:`snapshot` 取冻结快照。"""

    config: CuratedMemoryConfig
    directory: Path
    #: 冻结快照（load 时渲染一次）；``None`` 表示还没加载或该 store 为空。
    _snapshot: dict[str, str] = field(default_factory=dict)
    #: load 时发现超限的告警文字（只记录，不改变行为）。
    warnings: list[str] = field(default_factory=list)

    # ── 基础读写 ──────────────────────────────────────────────

    def path_for(self, target: str) -> Path:
        """该 target 对应的文件路径。"""
        return self.directory / _FILENAMES[target]

    def enabled(self, target: str) -> bool:
        """该 store 是否打开：``memory`` 看 ``enabled``，``user`` 看 ``user_profile_enabled``。"""
        return self.config.user_profile_enabled if target == "user" else self.config.enabled

    def limit(self, target: str) -> int:
        """该 store 的字符上限。"""
        return self.config.user_char_limit if target == "user" else self.config.memory_char_limit

    def entries(self, target: str) -> list[str]:
        """磁盘上的当前条目（去重、去空、保持顺序）；文件不存在返回空列表。"""
        return list(dict.fromkeys(self._parse(self._read_raw(self.path_for(target))[0])))

    def usage(self, target: str) -> str:
        """``"1,474/2,200"`` 形式的用量。"""
        return f"{self._chars(target):,}/{self.limit(target):,}"

    def _chars(self, target: str, entries: list[str] | None = None) -> int:
        items = self.entries(target) if entries is None else entries
        return len(ENTRY_DELIMITER.join(items))

    @staticmethod
    def _read_raw(path: Path) -> tuple[str, bool]:
        """``(原文, 是否读成功)``；文件不存在算成功（空内容），存在但读不了算失败。"""
        if not path.exists():
            return "", True
        try:
            # utf-8-sig：Windows 记事本保存的 BOM 不会粘在第一条前面（否则那条永远匹配不上）。
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse(raw: str) -> list[str]:
        """按**完整分隔符**切分（只出现一个 ``§`` 的行不会被误切），逐条 strip 后去空。"""
        return [item for item in (part.strip() for part in raw.split(ENTRY_DELIMITER)) if item]

    @staticmethod
    def _write(path: Path, entries: list[str]) -> None:
        """原子写：临时文件 + ``os.replace``，读方永远看不到写了一半的文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(ENTRY_DELIMITER.join(entries))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    # ── 快照（进系统提示的冻结块）────────────────────────────

    def load(self) -> None:
        """读盘并渲染冻结快照。

        只在**会话首轮**与**压缩重建**时调用。渲染时做注入扫描，命中的条目在这个快照里换成占位
        （原文不动），这样"文件里有毒"与"模型每轮读到毒"被分开处理。
        """
        self._snapshot = {}
        self.warnings = []
        for target in ("memory", "user"):
            if not self.enabled(target):
                continue
            path = self.path_for(target)
            raw, ok = self._read_raw(path)
            if not ok:
                # 读不到就说明白，不要渲染一个"空记忆"冒充事实。
                self._snapshot[target] = (
                    f"[{BLOCK_HEADERS[target]} 无法读取：文件存在但读不出来，本轮按没有内容处理；"
                    "请检查权限或编码后重试。]"
                )
                continue
            entries = list(dict.fromkeys(self._parse(raw)))
            if (count := len(ENTRY_DELIMITER.join(entries))) > self.limit(target):
                self.warnings.append(
                    f"{path.name} 超过字符上限（{count:,}/{self.limit(target):,}）：条目仍然全部加载，"
                    "但新的写入会被拒绝，直到把它整理回上限之内。"
                )
            block = self._render(target, [self._sanitize(entry, path.name, target) for entry in entries])
            if block:
                self._snapshot[target] = block

    def snapshot(self, target: str) -> str | None:
        """冻结快照（不是实时状态）；空 store 返回 ``None``。"""
        return self._snapshot.get(target) or None

    def _sanitize(self, entry: str, filename: str, target: str) -> str:
        """命中注入模式的条目在快照里换成占位；原文保留在文件中供人工删除。"""
        if not entry or entry.startswith("[BLOCKED:"):
            return entry
        if (finding := threat_scan.scan(entry)) is None:
            return entry
        return threat_scan.block_message(finding, filename=filename, target=target)

    def _render(self, target: str, entries: list[str]) -> str:
        """``表头 + 使用率 + 条目``；没有条目返回空串（没有内容就不进 Prompt）。"""
        if not entries:
            return ""
        content = ENTRY_DELIMITER.join(entries)
        limit = self.limit(target)
        pct = min(100, int(len(content) / limit * 100)) if limit > 0 else 0
        bar = "═" * 46
        return f"{bar}\n{BLOCK_HEADERS[target]} [{pct}% — {len(content):,}/{limit:,} 字符]\n{bar}\n{content}"

    def render_live(self, target: str) -> str:
        """**实时**状态（重置后的回显用）；不进系统提示。"""
        return self._render(target, self.entries(target))

    # ── 写入 ─────────────────────────────────────────────────

    def add(self, target: str, content: str) -> dict[str, Any]:
        """追加一条；完全重复的内容按"已存在"成功返回，不新增第二条。"""
        text = content.strip()
        if not text:
            return _error("内容不能为空。", "missing_content")
        if (finding := threat_scan.scan(text)) is not None:
            return _error(_blocked(finding), "scan_blocked")

        def mutate(entries: list[str]) -> Any:
            if text in entries:
                return None, "这条内容已经在记忆里，未重复添加。"
            merged = [*entries, text]
            if (total := len(ENTRY_DELIMITER.join(merged))) > self.limit(target):
                return _over_limit(target, entries, total, len(text), self.limit(target))
            return merged, "已添加。"

        return self._mutate(target, mutate)

    def patch(self, target: str, old_text: str, new_text: str) -> dict[str, Any]:
        """段替换：在命中的那一条里把 ``old_text`` 换成 ``new_text``，条目其余内容原样保留。"""
        if not (old_text or "").strip():
            return _error("old_text 不能为空：要用它定位条目里的一段文字。", "missing_old_text")
        if new_text is None or not str(new_text).strip():
            return _error("new_text 不能为空；要删整条请用 remove。", "missing_content")
        if (finding := threat_scan.scan(str(new_text))) is not None:
            return _error(_blocked(finding), "scan_blocked")

        def mutate(entries: list[str]) -> Any:
            index, problem = self._locate(entries, old_text)
            if problem is not None:
                return problem
            before = entries[index]
            if before.count(old_text) > 1:
                return _error(
                    f"「{old_text}」在这一条里出现 {before.count(old_text)} 次；"
                    "请多带一点前后文，让它只出现一次。", "ambiguous")
            after = before.replace(old_text, str(new_text), 1)
            if after == before:
                return _error("这次替换没有改变任何内容。", "invalid_args")
            merged = [*entries[:index], after, *entries[index + 1:]]
            if (total := len(ENTRY_DELIMITER.join(merged))) > self.limit(target):
                return _over_limit(target, entries, total, len(after) - len(before), self.limit(target))
            return merged, "已修改。", {"patched_entry": {"before": before, "after": after}}

        return self._mutate(target, mutate)

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        """删除命中的那一条（整条）。"""
        if not (old_text or "").strip():
            return _error("old_text 不能为空：要用它定位要删的条目。", "missing_old_text")

        def mutate(entries: list[str]) -> Any:
            index, problem = self._locate(entries, old_text)
            if problem is not None:
                return problem
            removed = entries[index]
            merged = [*entries[:index], *entries[index + 1:]]
            return merged, "已删除。", {"removed_entry": removed}

        return self._mutate(target, mutate)

    def apply_batch(self, target: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """原子批量：任何一步失败就整批不落盘；上限只校验**最终结果**。

        这是"记忆满了还能一次改完"的关键：一次调用里既能删旧、又能加新，长度检查放在最后。
        """
        if not operations:
            return _error("operations 不能为空。", "invalid_args")

        def mutate(entries: list[str]) -> Any:
            working = list(entries)
            removed: list[str] = []
            for position, operation in enumerate(operations, start=1):
                action = str(operation.get("action") or "")
                content = str(operation.get("content") or operation.get("new_text") or "")
                old_text = str(operation.get("old_text") or "")
                if action == "add":
                    text = content.strip()
                    if not text:
                        return _abort(target, position, "add 需要 content。")
                    if (finding := threat_scan.scan(text)) is not None:
                        return _abort(target, position, _blocked(finding))
                    if text not in working:
                        working.append(text)
                    continue
                if action not in {"patch", "remove"}:
                    return _abort(target, position, f"未知 action「{action}」，只能用 add / patch / remove。")
                if not old_text.strip():
                    return _abort(target, position, f"{action} 需要 old_text。")
                index, problem = self._locate(working, old_text)
                if problem is not None:
                    return _abort(target, position, str(problem["error"]),
                                  str(problem.get("failure") or "no_match"))
                before = working[index]
                if action == "remove":
                    working = [*working[:index], *working[index + 1:]]
                    removed.append(before)
                    continue
                if not content.strip():
                    return _abort(target, position, "patch 需要 content（替换后的文字）。")
                if (finding := threat_scan.scan(content)) is not None:
                    return _abort(target, position, _blocked(finding))
                if before.count(old_text) > 1:
                    return _abort(target, position, f"「{old_text}」在这一条里出现多次，请多带前后文。")
                working = [*working[:index], before.replace(old_text, content, 1), *working[index + 1:]]
            if entries and not working:
                return _abort(
                    target, None,
                    "这一批会把库删空。要保留至少一条（合并成一条更短的），"
                    "确实要清空请用单条 remove 或 `memory notes reset`。",
                    "would_empty")
            if (total := len(ENTRY_DELIMITER.join(working))) > self.limit(target):
                return _abort(
                    target, None,
                    f"这一批执行后是 {total:,}/{self.limit(target):,} 字符，仍超出上限；"
                    "请在同一批里再删除或缩短一些条目。",
                    "over_budget")
            extra = {"removed_entries": removed} if removed else {}
            return working, f"已应用 {len(operations)} 个操作。", extra

        return self._mutate(target, mutate)

    def reset(self, target: str) -> None:
        """清空一个 store（人工操作，模型不可用）：删文件而不是写空文件。"""
        with _lock_for(self.path_for(target)):
            self.path_for(target).unlink(missing_ok=True)
            self._snapshot.pop(target, None)

    # ── 内部：定位、加锁、漂移 ────────────────────────────────

    def _locate(self, entries: list[str], old_text: str) -> tuple[int, dict[str, Any] | None]:
        """``(下标, None)`` 或 ``(-1, 错误)``；整条完全相等优先于子串匹配。"""
        needle = old_text.strip()
        exact = [index for index, entry in enumerate(entries) if entry == needle]
        matches = exact or [index for index, entry in enumerate(entries) if needle in entry]
        if len({entries[index] for index in matches}) > 1:
            return -1, _error(
                f"「{needle}」匹配到多条，请写得更具体一些。", "ambiguous",
                matches=[entries[index][:80] for index in matches])
        if not matches:
            return -1, _error(
                f"没有条目匹配「{needle}」；old_text 必须来自**一条**条目的原文"
                "（条目之间用 § 分隔，一次只改一条）。", "no_match", current_entries=entries)
        return matches[0], None

    def _mutate(self, target: str, mutate: Callable[[list[str]], Any]) -> dict[str, Any]:
        """加锁 → 读盘 → 漂移检查 → 变更函数 → 原子写 → 成功响应。

        变更函数的返回值只有三种：

        - ``dict``：直接回给模型（错误，或"不必写"的说明），**不落盘**；
        - ``(None, 说明)``：成功但不需要写（例如重复条目）；
        - ``(新条目, 说明, 额外字段)``：原子写盘后按成功返回。
        """
        path = self.path_for(target)
        with _lock_for(path):
            raw, ok = self._read_raw(path)
            if not ok:
                return _error(
                    f"拒绝写入 {path.name}：文件存在但读不出来（被占用、权限或编码问题）。"
                    "把读不出的文件当成空文件再保存，会把已有记忆覆盖掉——本次没有改动任何内容。",
                    "read_failed")
            if (backup := self._drift_backup(target, raw)) is not None:
                return _error(
                    f"拒绝写入 {path.name}：文件里有一段内容比整个库的上限还长，说明它是被手工写进去的"
                    f"整段文本（没有 § 分隔）。直接重写会丢掉那段内容，已另存快照到 {backup}。"
                    "请先把文件整理成 § 分隔的条目，或把多余内容移出去，再重试。",
                    "drift", drift_backup=backup)
            entries = list(dict.fromkeys(self._parse(raw)))
            outcome = mutate(entries)
            if isinstance(outcome, dict):
                return outcome
            new_entries, message = outcome[0], outcome[1]
            extra = outcome[2] if len(outcome) > 2 else {}
            if new_entries is not None:
                self._write(path, new_entries)
            return _ok(
                target, message,
                entries=entries if new_entries is None else new_entries,
                limit=self.limit(target), **extra)

    def _drift_backup(self, target: str, raw: str) -> str | None:
        """外部漂移：出现一条比**整个库上限**还长的条目 → 返回备份路径，否则 ``None``。

        为什么用这个信号：工具写入的条目永远不可能超过整库上限（写入前就挡住了），所以这种条目
        只可能来自手工编辑或命令行追加；按它覆盖就会静默丢内容。
        """
        limit = self.limit(target)
        if max((len(item) for item in self._parse(raw)), default=0) <= limit:
            return None
        path = self.path_for(target)
        backup = path.with_name(f"{path.name}.bak.{int(time.time())}")
        try:
            backup.write_text(raw, encoding="utf-8")
        except OSError:
            return f"{backup}（备份写失败，文件未改动）"
        return str(backup)


# ── 响应构造（成功/失败的字段保持一致，工具层不再自己拼）────────


def _ok(
    target: str, message: str, *, entries: list[str], limit: int, **extra: Any
) -> dict[str, Any]:
    """成功响应：带上用量与条数，**不带**条目清单（回显会诱导模型反复"再整理一次"）。

    ``extra`` 只放"用户需要知道丢了什么"的字段（被替换/删除的原文），不是给模型找活干的清单。
    """
    return {
        "success": True,
        "target": target,
        "message": message,
        "usage": f"{len(ENTRY_DELIMITER.join(entries)):,}/{limit:,}",
        "entry_count": len(entries),
        **extra,
    }


def _error(message: str, failure_class: str, **extra: Any) -> dict[str, Any]:
    """失败响应：``current_entries`` 只在"需要它才能继续"时由调用方补上。"""
    return {"success": False, "error": message, "failure": failure_class, **extra}


def _blocked(finding: str) -> str:
    return (
        f"这段内容命中「{threat_scan.finding_text(finding)}」，未写入：记忆会进入每一轮的系统提示，"
        "一条被污染的条目会跨会话持续生效。请改写后再存（如果这是用户自己的原文，"
        "请他本人直接编辑记忆文件）。"
    )


def _abort(
    target: str, position: int | None, message: str, failure: str = "invalid_args"
) -> dict[str, Any]:
    """批量失败：整批不落盘；错误里点明是第几个操作。

    ``failure`` 是**给工具层用的分类**（决定要不要计进"这一轮整合失败"的封顶）：
    ``over_budget`` / ``no_match`` 这类"可以靠重排再试"的与 ``invalid_args`` 分开。
    """
    prefix = f"Operation {position}: " if position is not None else ""
    response = _error(prefix + message + " 整批未应用（batch 是原子的）。", failure)
    response["target"] = target
    return response


def _over_limit(
    target: str, entries: list[str], total: int, added: int, limit: int
) -> dict[str, Any]:
    """超限：把当前条目回给模型，让它一次改成"删旧 + 加新"。"""
    return _error(
        f"这一条（{added} 字符）会让记忆变成 {total:,}/{limit:,} 字符，超出 {total - limit:,} 字符。"
        "请改成一个 `operations` 批量：在同一批里删除或缩短（patch）若干旧条目，同时添加这一条"
        "——上限只校验批量的最终结果。",
        "over_budget", current_entries=entries, usage=f"{total:,}/{limit:,}")
