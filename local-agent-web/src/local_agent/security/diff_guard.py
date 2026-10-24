"""diff 预览、变更范围校验与自定义 Patch 应用器（架构 §6.3、§11.2）。

T06 只需要"生成预览 + 统计变更行数"，用于免审上限判定与反思阶段的机械校验；
T22 在这里加 Patch 输入模式、冲突检测与 hunk 校验。**不引入 `unidiff`**（开发文档 §9.1
未批准该依赖），解析与应用全部用标准库自己实现，且必须是有界操作。

两条硬口径：

- **解析（:func:`parse_patch`）只看 patch 自身**：格式、计数、顺序、重叠；不接触目标文件。
  安全判定（免审/审批）只需要这一步的结果，因此可以在读文件之前完成。
- **应用（:func:`apply_patch`）对着真实文件内容做严格校验**：按 `@@` 声明的旧行号定位，
  上下文行与删除行逐字一致；不做模糊搜索、不容忍行号漂移，不一致即冲突并拒绝。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Literal, cast

from local_agent.exceptions import FileOperationError

DEFAULT_CONTEXT_LINES = 3

#: patch 文本长度上限（字符）：解析必须是有界操作，不能拿超长文本做无上限处理（架构 §6.3）。
MAX_PATCH_CHARS = 200_000

#: 单次 patch 允许的 hunk 数上限（架构 §6.3：限制单次修改规模）。
MAX_PATCH_HUNKS = 200

#: hunk 行的前缀标记：上下文、删除、新增。
Mark = Literal[" ", "-", "+"]

_MARKS = (" ", "-", "+")

#: hunk 头：``@@ -旧起始[,旧行数] +新起始[,新行数] @@``（计数省略时为 1）。
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: 明确**不支持**的 Git 扩展 diff 头（架构 §6.3：T22 只接受标准 hunk）。
_UNSUPPORTED_HEADERS = (
    "diff ",
    "index ",
    "new file mode",
    "deleted file mode",
    "old mode ",
    "new mode ",
    "similarity index",
    "rename from",
    "rename to",
    "copy from",
    "copy to",
    "Binary files",
    "GIT binary patch",
)

#: 不能作为 edit_file 目标的文件头（新建/删除文件语义）。
_NULL_TARGETS = ("/dev/null", "dev/null")


def build_unified_diff(
    old_text: str,
    new_text: str,
    *,
    path: str = "file",
    context_lines: int = DEFAULT_CONTEXT_LINES,
) -> str:
    """生成 unified diff 文本（用户看到的审计产物统一用它）。

    Args:
        old_text: 修改前的内容。
        new_text: 修改后的内容。
        path: 展示用的文件名，出现在 diff 头。
        context_lines: 上下文行数。

    Returns:
        unified diff 文本；没有差异时为空串。换行统一为 ``\\n`` 便于展示。
    """
    diff_lines = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context_lines,
    )
    text = "".join(line if line.endswith("\n") else f"{line}\n" for line in diff_lines)
    return text.replace("\r\n", "\n")


def count_diff_lines(diff_text: str) -> tuple[int, int]:
    """从 unified diff 文本数出 ``(新增行数, 删除行数)``。

    文件头（``---`` / ``+++``）与 hunk 头（``@@``）不是内容行，不能计入——
    变更面板上显示的 ``+N −M`` 必须与实际改动一致，否则用户会拿它核对行数。
    """
    additions = deletions = 0
    for raw in diff_text.splitlines():
        if raw.startswith(("+++", "---", "@@")):
            continue
        if raw.startswith("+"):
            additions += 1
        elif raw.startswith("-"):
            deletions += 1
    return additions, deletions


def count_changed_lines(old_text: str, new_text: str) -> int:
    """变更行数 = 新增行数 + 删除行数（不含上下文行）。

    用于判断一次修改是否需要人工审批（架构 §6.3、§8 的 ``security.large_edit``）。
    """
    matcher = difflib.SequenceMatcher(
        None, old_text.splitlines(), new_text.splitlines(), autojunk=False
    )
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += (i2 - i1) + (j2 - j1)
    return changed


def diff_is_empty(old_text: str, new_text: str) -> bool:
    """新旧内容是否完全一致。"""
    return old_text == new_text


def edit_input_mode(old_text: object, new_text: object, patch: object) -> str:
    """判定 ``edit_file`` 本次使用哪种输入模式：``"replace"`` 或 ``"patch"``。

    两种模式互斥（架构 §6.3）：同时给出或都为空都拒绝。安全判定与工具执行**共用这一个**
    函数，避免两处各写一套互斥规则而分叉。非字符串（模型传了数字等）按"未提供"处理。

    Raises:
        FileOperationError: 两种模式同时给出，或都没有可执行内容。
    """
    old = old_text if isinstance(old_text, str) else ""
    new = new_text if isinstance(new_text, str) else ""
    raw_patch = patch if isinstance(patch, str) else ""
    if raw_patch.strip():
        if old or new:
            raise FileOperationError(
                "edit_file 的 patch 模式与 old_text/new_text 不能同时使用："
                "请只用 patch（unified diff），或改用精确串替换"
            )
        return "patch"
    if not old or not new:
        raise FileOperationError(
            "edit_file 需要提供 old_text 与 new_text（精确串替换，默认模式），"
            "或提供 patch（unified diff）"
        )
    return "replace"


@dataclass(frozen=True)
class HunkLine:
    """hunk 里的一行：前缀标记 + 去掉前缀的内容（不含换行）。"""

    mark: Mark
    text: str


@dataclass(frozen=True)
class PatchHunk:
    """一个校验通过的 hunk（架构 §6.3）。"""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[HunkLine, ...]

    @property
    def insert_index(self) -> int:
        """该 hunk 在旧文件里的 0 基起始行号。

        计数为 0 的纯插入 hunk 按 GNU 约定：行号表示"插入点之前的那一行"，因此 0 基下标
        就是 ``old_start``（``@@ -0,0 +1,2 @@`` 表示从文件开头插入）。
        """
        return self.old_start - 1 if self.old_count else self.old_start

    @property
    def removed_lines(self) -> int:
        return sum(1 for line in self.lines if line.mark == "-")

    @property
    def added_lines(self) -> int:
        return sum(1 for line in self.lines if line.mark == "+")


@dataclass(frozen=True)
class PatchPlan:
    """解析后的 patch：只描述"要改什么"，不含任何文件内容。"""

    hunks: tuple[PatchHunk, ...]
    header_names: tuple[str, ...]

    @property
    def changed_lines(self) -> int:
        """删除行数 + 新增行数：与 :func:`count_changed_lines` 同一口径，用于免审上限判定。"""
        return sum(hunk.removed_lines + hunk.added_lines for hunk in self.hunks)


def parse_patch(patch: str) -> PatchPlan:
    """解析 unified diff 文本，做**纯结构**校验（架构 §6.3 的 ``patch`` 模式）。

    校验内容：长度上限、非空、hunk 头格式、行前缀、``@@`` 声明行数与内容是否相符、
    hunk 数量上限、hunk 顺序与重叠。文件头只做结构性与 basename 记录，与目标文件的
    一致性在 :func:`apply_patch` 里校验。

    Raises:
        FileOperationError: 空 patch、格式不受支持、计数不符、hunk 乱序或重叠。
    """
    if len(patch) > MAX_PATCH_CHARS:
        raise FileOperationError(
            f"patch 过长（{len(patch)} 字符，上限 {MAX_PATCH_CHARS}）：请缩小改动范围后重试"
        )
    if not patch.strip():
        raise FileOperationError(
            "patch 为空：请提供 unified diff，或改用 old_text/new_text 精确串替换"
        )

    lines = _patch_lines(patch)
    header_names: list[str] = []
    index = 0
    total = len(lines)

    # 1) 前导：只允许文件头与空行；Git 扩展头与其它内容一律拒绝。
    while index < total and not lines[index].startswith("@@"):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith(("--- ", "+++ ")):
            header_names.append(line[4:].strip())
            index += 1
            continue
        if any(line.startswith(prefix) for prefix in _UNSUPPORTED_HEADERS):
            raise FileOperationError(
                f"不支持的 diff 头（只接受标准 hunk 头）：{line.strip()[:60]}；"
                "Git 扩展 diff 头与二进制差异不在支持范围内（架构 §6.3）"
            )
        raise FileOperationError(
            "不是有效的 unified diff：文件头之后应当是 @@ 开头的 hunk 头，"
            f"实际是 {line.strip()[:60]!r}"
        )

    # 2) 逐个 hunk：头、行前缀、计数。
    hunks: list[PatchHunk] = []
    while index < total:
        header = lines[index]
        match = _HUNK_HEADER.match(header)
        if match is None:
            raise FileOperationError(
                f"hunk 头格式非法：{header.strip()[:80]!r}（应为 @@ -l[,s] +l[,s] @@）"
            )
        if len(hunks) >= MAX_PATCH_HUNKS:
            raise FileOperationError(f"hunk 数超过上限 {MAX_PATCH_HUNKS}，已拒绝")
        old_start = int(match.group(1))
        old_count = _count(match.group(2))
        new_start = int(match.group(3))
        new_count = _count(match.group(4))
        index += 1

        body: list[HunkLine] = []
        while index < total and not lines[index].startswith("@@"):
            raw = lines[index]
            if raw.startswith("\\"):
                raise FileOperationError(
                    "patch 含 '\\ No newline at end of file' 标记："
                    "本项目不支持无末尾换行的写回（架构 §6.3）"
                )
            mark = raw[:1]
            if mark not in _MARKS:
                raise FileOperationError(
                    "hunk 体内出现无前缀行（空上下文行也必须以单个空格开头，不要去掉行首空格）："
                    f"{raw[:60]!r}"
                )
            body.append(HunkLine(mark=cast("Mark", mark), text=raw[1:]))
            index += 1

        actual_old = sum(1 for item in body if item.mark != "+")
        actual_new = sum(1 for item in body if item.mark != "-")
        if actual_old != old_count or actual_new != new_count:
            raise FileOperationError(
                f"hunk 计数与内容不符（声明 -{old_count}/+{new_count}，"
                f"实际 -{actual_old}/+{actual_new}）：{header.strip()[:60]!r}"
            )
        hunks.append(
            PatchHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=tuple(body),
            )
        )

    if not hunks:
        raise FileOperationError("patch 里没有任何 hunk：空 Patch 不能应用")

    # 3) 顺序与重叠：hunk 必须按旧行号升序且互不重叠。
    previous_end = 0
    for position, hunk in enumerate(hunks):
        start = hunk.insert_index
        if start < 0 or (position > 0 and start < previous_end):
            raise FileOperationError(
                f"hunk 顺序或位置非法（乱序/重叠/越界）：@@ -{hunk.old_start},{hunk.old_count} @@"
            )
        previous_end = start + hunk.old_count

    return PatchPlan(hunks=tuple(hunks), header_names=tuple(header_names))


def apply_patch(original: str, plan: PatchPlan, *, file_name: str) -> str:
    """把解析好的 patch 应用到文件内容上（架构 §6.3 的 ``patch`` 模式）。

    - 文件头（若给出）只校验 basename 与目标文件名一致：本项目的 diff 预览只写文件名，
      全等校验会误拒子目录里的文件；
    - 严格按 `@@` 的旧行号定位，上下文行与删除行逐字一致；偏移、越界、不匹配都算冲突；
    - 匹配忽略 CRLF 的 ``\\r``，写回保留每一行原有的结束符；
    - 新增行一律以换行结束（patch 的"\\ No newline"标记本项目不支持，已在上一步拒绝）。

    Raises:
        FileOperationError: 文件头不一致、定位越界、上下文不匹配、应用后内容未变化。
    """
    for name in plan.header_names:
        basename = _header_basename(name)
        if basename in _NULL_TARGETS:
            raise FileOperationError(
                "patch 的文件头是 /dev/null（新建或删除文件）：edit_file 只修改已有文件"
            )
        if basename != file_name:
            raise FileOperationError(
                f"patch 的文件头指向 {basename}，与目标文件 {file_name} 不一致：已拒绝应用"
            )

    result = _split_lines(original)
    # 新增行的结束符按文件风格取：CRLF 文件里加的行也用 CRLF；空文件按 LF。
    default_terminator = "\r\n" if any(term == "\r\n" for _, term in result) else "\n"
    offset = 0

    for hunk in plan.hunks:
        index = hunk.insert_index + offset
        end = index + hunk.old_count
        if index < 0 or end > len(result):
            raise FileOperationError(
                f"patch 定位越界（@@ -{hunk.old_start},{hunk.old_count} @@）："
                f"目标文件当前只有 {len(result)} 行，patch 与当前文件不匹配"
            )

        cursor = index
        for item in hunk.lines:
            if item.mark == "+":
                continue
            actual = result[cursor][0] if cursor < len(result) else "<文件结束>"
            if cursor >= len(result) or actual != item.text:
                raise FileOperationError(
                    f"patch 上下文不匹配（第 {cursor + 1} 行）：期望 {item.text!r}，"
                    f"实际 {actual!r}；文件已被改动或 patch 针对的是另一个版本，"
                    "请重新读取文件后再生成 patch"
                )
            cursor += 1

        replacement: list[tuple[str, str]] = []
        cursor = index
        for item in hunk.lines:
            if item.mark == "+":
                replacement.append((item.text, default_terminator))
            elif item.mark == " ":
                replacement.append(result[cursor])
                cursor += 1
            else:  # "-"：被删除，不进入结果
                cursor += 1
        result[index:end] = replacement
        offset += hunk.new_count - hunk.old_count

    # 只有文件的最后一行允许"没有结束符"（原本没有末尾换行、且 patch 没碰它）。
    # 这类行被插入的行推到中间时必须补上换行，否则拼接会把两行粘在一起。
    result = [
        (content, terminator or default_terminator) for content, terminator in result[:-1]
    ] + result[-1:]

    updated = _join_lines(result)
    if updated == original:
        raise FileOperationError("patch 应用后内容没有变化，已拒绝写入")
    return updated


def _patch_lines(patch: str) -> list[str]:
    """把 patch 文本拆成行，并去掉 CRLF 的 ``\\r``（匹配时忽略 CR，架构 §6.3）。

    末尾的空行全部丢掉：它们只能是排版残留，不可能是 hunk 体内有意义的行——
    真正的空上下文行必须以单个空格开头（hunk 体内的无前缀空行仍然拒绝）。
    """
    lines = [raw.removesuffix("\r") for raw in patch.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _count(raw: str | None) -> int:
    """hunk 头里的行数：省略时按 1（unified diff 约定）。"""
    return 1 if raw is None else int(raw)


def _header_basename(name: str) -> str:
    """取 diff 文件头的 basename，兼容 ``a/`` ``b/`` 前缀、目录与 ``\\t`` 时间戳。"""
    raw = name.strip().split("\t", 1)[0].split(" ", 1)[0]
    return raw.replace("\\", "/").rsplit("/", 1)[-1]


def _split_lines(text: str) -> list[tuple[str, str]]:
    """拆成 ``(内容, 结束符)`` 列表，结束符是 ``""`` / ``"\\n"`` / ``"\\r\\n"``。

    内容里不含换行符：这样匹配天然忽略 CRLF 差异，写回时又能一行一行地保留原有结束符，
    不会因为应用 patch 而把整个文件的行尾风格改掉（架构 §6.3）。没有末尾换行的文件只有
    最后一行带空结束符，patch 不碰它时原样保留。
    """
    if not text:
        return []
    lines: list[tuple[str, str]] = []
    start = 0
    for index, char in enumerate(text):
        if char != "\n":
            continue
        segment = text[start:index]
        if segment.endswith("\r"):
            lines.append((segment[:-1], "\r\n"))
        else:
            lines.append((segment, "\n"))
        start = index + 1
    if start < len(text):
        lines.append((text[start:], ""))
    return lines


def _join_lines(lines: list[tuple[str, str]]) -> str:
    """:func:`_split_lines` 的逆操作。"""
    return "".join(content + terminator for content, terminator in lines)
