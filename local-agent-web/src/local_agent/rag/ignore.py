"""``.gitignore`` 规则（架构 §6.7 / §4.3；v0.6 T63 第二片）。

**为什么需要它**：检索遍历此前只跳过一份**静态**目录清单（:data:`SKIPPED_DIRS`，本模块里）。
2026-10-10 实测本机工作区：12828 个候选文件（4.4 GB），其中 `local-agent-web/eval/`（**gitignored** 的
产物目录）就占 8138 个——`glob="*.py"` 时前 2000 个候选**全部**来自它，`src/`、`tests/` 一个文件都
扫不到。往静态清单里一个个补目录名治标不治本（清单会腐烂，而且别人的仓库叫什么名字我们不知道）。

成熟工具的默认语义是**尊重 `.gitignore`**（ripgrep / ag / fd 都是这样，Claude Code 的 Grep 也走
ripgrep），所以这里照做。

三条边界（写进契约）：

- **只影响枚举**：`grep` / `glob` / 按需 RAG 的遍历会跳过被忽略的路径；**显式点名**一个文件
  （`grep path=<file>`、`read_file`）不受影响——"你点名要看的，就给你看"（同 ripgrep 的取向）。
- **没有 `.gitignore` 的工作区**：规则为空，行为与从前**逐字相同**。
- **不自己实现 gitignore 语法**：用 :mod:`pathspec`（black / pre-commit 用的同一套实现）。嵌套
  `.gitignore` 按目录栈处理：**更深层的规则优先**，`!` 可以反选；被忽略的**目录**整个剪掉，
  这与 git 自己的口径一致（"父目录被排除时，无法再包含其中的文件"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

#: 规则栈的深度上限：目录嵌套再深也不该让"往上找 `.gitignore`"无界增长。
MAX_IGNORE_LAYERS = 32

#: 遍历时直接跳过的目录（不区分大小写）；隐藏目录另有一层判断。
#:
#: 2026-09-30 起**公开**：`tools/search_tools.py` 的 `grep` / `glob` 复用同一份清单；
#: v0.6 T63 第三片起**搬到这里**（口径模块）——`rag/walk.py` 与按需 RAG 都用它，
#: `rag/search.py` 不再自带一份。两处各留一份必然悄悄分叉，而"跳过哪些目录"直接决定检索结果
#: 是否可信，不能有两套。
SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".idea",
        ".vscode",
        "site-packages",
        "dist",
        "build",
    }
)


@dataclass(frozen=True)
class _Layer:
    """一层规则：某个目录里的 `.gitignore`（模式相对**那个目录**解释，同 git）。"""

    directory: Path
    spec: GitIgnoreSpec


def load_spec(directory: Path) -> GitIgnoreSpec | None:
    """读一个目录里的 `.gitignore`；没有、读不到或内容为空时返回 `None`。"""
    target = directory / ".gitignore"
    try:
        if not target.is_file():
            return None
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        # 读不到就当没有规则：检索不能因为一个坏文件整体失败（与"跳过读不了的文件"同一取向）。
        return None
    return GitIgnoreSpec.from_lines(lines)


class IgnoreRules:
    """按目录栈评估 `.gitignore`；与 `os.walk` 的 top-down 遍历对齐使用。

    调用方每进入一个目录调一次 :meth:`sync`（幂等），再把每个候选路径交给 :meth:`ignores`。
    """

    def __init__(self, base: Path) -> None:
        self._base = Path(base)
        self._stack: list[_Layer] = []

    @property
    def layers(self) -> int:
        """当前规则层数（测试与诊断用）。"""
        return len(self._stack)

    def sync(self, directory: Path) -> None:
        """把规则栈对齐到 ``directory``：弹到公共祖先，再逐层压入更深目录的 `.gitignore`。"""
        target = Path(directory)
        try:
            relative = target.relative_to(self._base).parts
        except ValueError:
            # 不在基准目录下（理论上不会发生：遍历从基准开始）：清空栈，退回"没有规则"。
            self._stack.clear()
            return
        chain: list[Path] = [self._base]
        for part in relative:
            chain.append(chain[-1] / part)
        # 公共前缀：栈里与目标链一致的部分保留
        common = 0
        while (
            common < len(self._stack)
            and common < len(chain)
            and self._stack[common].directory == chain[common]
        ):
            common += 1
        del self._stack[common:]
        for item in chain[common:]:
            if len(self._stack) >= MAX_IGNORE_LAYERS:
                break
            spec = load_spec(item)
            if spec is not None:
                self._stack.append(_Layer(directory=item, spec=spec))

    def ignores(self, path: Path, *, is_dir: bool) -> bool:
        """``path`` 是否被忽略（同 git：更深层的规则说了算）。"""
        verdict = False
        for layer in self._stack:
            try:
                relative = path.relative_to(layer.directory).as_posix()
            except ValueError:  # pragma: no cover - 调用方保证在基准目录下
                continue
            if is_dir:
                # 目录带尾斜杠，`build/` 这类模式才匹配得上（git 的同一约定）。
                relative = f"{relative}/"
            # 三态：`None` = 这一层没意见（不能用 `match_file` 的布尔值代替——它把"没匹配"与
            # "被 `!` 反选"压成同一个 `False`，嵌套时会把外层的忽略抹掉）。
            include = layer.spec.check_file(relative).include
            if include is not None:
                verdict = include
        return verdict
