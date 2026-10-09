"""AGENTS 规则文件的扫描与合并（架构 §7.1、开发文档 T04）。

扫描规则：

1. 从当前工作目录向上找最近的一个 ``.git``（目录或文件），它就是 Git 根目录。
2. 扫描上界始终是 ``workspace.root``：Git 根目录在工作区外时以工作区为上界，
   没有 Git 仓库时同样以工作区为上界。
3. 合并顺序为 Git 根 → 逐级向下到当前目录；越近的目录优先级越高，排在后面
   （``precedence.closer_directory_wins`` 为 false 时反转该顺序）。
4. 同一目录存在覆盖文件时替换该目录的基础文件（``precedence.override_wins`` 为
   false 时两份都加载，基础文件在前）。

   约定：文件名含 ``.override`` 的视为覆盖文件（架构 §7.1-3 的
   ``AGENTS.override.md`` / ``AGENTS.md``）。

5. 单文件超过 ``agents.max_file_chars``、累计超过 ``agents.max_total_chars`` 时，
   保留头尾并插入截断标记。

安全边界：位于 ``workspace.root`` 之外的文件一律不读取；``current_cwd`` 不在工作区内时
不加载任何文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from local_agent.config import AgentsConfig
from local_agent.context.token_budget import estimate_tokens

TRUNCATION_MARKER = "…（内容已截断，省略 {omitted} 字符）…"
_SECTION_HEADER = "### 规则文件：{path}"


@dataclass(frozen=True)
class AgentsContext:
    """合并后的 AGENTS 规则文本及其来源。"""

    text: str
    files: tuple[Path, ...] = ()
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        """是否没有加载到任何规则内容。"""
        return not self.text.strip()

    @property
    def estimated_tokens(self) -> int:
        """合并文本的估算 token 数。"""
        return estimate_tokens(self.text)


def load_agents(
    config: AgentsConfig,
    *,
    workspace_root: Path,
    current_cwd: Path,
) -> AgentsContext:
    """按工作区边界扫描并合并 AGENTS 规则文件。

    Args:
        config: ``agents`` 配置节。
        workspace_root: 工作区根目录，扫描上界。
        current_cwd: 当前工作目录，扫描起点。

    Returns:
        合并后的文本与来源文件；没有可加载文件时 ``text`` 为空串。
    """
    if not config.enabled:
        return AgentsContext(text="")

    directories = _directories_to_scan(config, workspace_root, current_cwd)
    if not directories:
        return AgentsContext(text="")

    sections: list[str] = []
    files: list[Path] = []
    used_chars = 0
    truncated = False

    for directory in directories:
        for path in _candidate_files(directory, config):
            remaining = config.max_total_chars - used_chars
            if remaining <= 0:
                truncated = True
                break
            content, file_truncated = _read_truncated(
                path, min(config.max_file_chars, remaining)
            )
            truncated = truncated or file_truncated
            section = _SECTION_HEADER.format(path=_display_path(path, workspace_root))
            sections.append(f"{section}\n{content}".rstrip())
            used_chars += len(section) + len(content)
            files.append(path)

    if not sections:
        return AgentsContext(text="")

    return AgentsContext(text="\n\n".join(sections), files=tuple(files), truncated=truncated)


def _directories_to_scan(
    config: AgentsConfig,
    workspace_root: Path,
    current_cwd: Path,
) -> list[Path]:
    """计算需要扫描的目录，顺序即合并顺序。"""
    chain = _ancestor_chain(current_cwd, workspace_root)
    if not chain:
        return []

    git_root = _find_git_root(chain)
    directories = chain if git_root is None else chain[chain.index(git_root) :]
    if not config.precedence.closer_directory_wins:
        directories = list(reversed(directories))
    return directories


def _ancestor_chain(start: Path, boundary: Path) -> list[Path]:
    """返回 ``boundary`` 到 ``start`` 的目录链（含两端）；``start`` 不在其内时返回空。"""
    start_path = _normalized(start)
    boundary_path = _normalized(boundary)
    if start_path != boundary_path and boundary_path not in start_path.parents:
        return []

    chain = [start_path]
    while chain[-1] != boundary_path:
        parent = chain[-1].parent
        if parent == chain[-1]:
            return []  # 理论上不会发生：boundary_path 一定是祖先
        chain.append(parent)
    return list(reversed(chain))


def _find_git_root(chain: list[Path]) -> Path | None:
    """在目录链中从近到远找最近带 ``.git`` 的目录。"""
    for directory in reversed(chain):
        if (directory / ".git").exists():
            return directory
    return None


def _candidate_files(directory: Path, config: AgentsConfig) -> list[Path]:
    """该目录下要加载的文件：覆盖文件优先，否则加载基础文件。"""
    overrides = [name for name in config.filenames if ".override" in name]
    bases = [name for name in config.filenames if ".override" not in name]

    for name in overrides:
        path = directory / name
        if path.is_file():
            if config.precedence.override_wins:
                return [path]
            return [*_existing(directory, bases), path]
    return _existing(directory, bases)


def _existing(directory: Path, names: list[str]) -> list[Path]:
    return [directory / name for name in names if (directory / name).is_file()]


def _read_truncated(path: Path, max_chars: int) -> tuple[str, bool]:
    """读取文件；超长时保留头尾并插入截断标记；读不到时给出可读说明。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"（该文件无法读取：{type(exc).__name__}）", False
    if len(text) <= max_chars:
        return text, False
    return truncate_middle(text, max_chars), True


def truncate_middle(text: str, max_chars: int) -> str:
    """保留头部与尾部，中间用截断标记代替。

    公开给技能加载复用（v0.5 T55）：AGENTS 与技能正文的截断必须是**同一套口径与同一条标记**，
    否则同一份内容在两处会给出不同的"省略了多少字符"，那就成了两套截断实现。
    """
    if max_chars <= 0:
        return TRUNCATION_MARKER.format(omitted=len(text))
    head_chars = max_chars // 2
    tail_chars = max_chars - head_chars
    omitted = len(text) - max_chars
    marker = TRUNCATION_MARKER.format(omitted=omitted)
    return f"{text[:head_chars]}\n{marker}\n{text[len(text) - tail_chars :]}"


def _display_path(path: Path, workspace_root: Path) -> str:
    """展示用路径：尽量用工作区相对路径，避免把绝对路径写进提示。"""
    root = _normalized(workspace_root)
    resolved = _normalized(path)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _normalized(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - 极少数无法解析的路径
        return path.absolute()
