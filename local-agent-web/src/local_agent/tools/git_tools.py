"""Git 工具：只读的 status / diff / log 与受控提交（架构 §6.11、§16.4；v0.5 T54）。

四条硬边界（契约 §16.4，实现必须与它逐条对应）：

1. **作用域是工作区，不是仓库**：工作区常常是仓库的**子目录**（本机 ``local-agent-web`` 的仓库根就在
   它的上一级）。所以所有调用都用 ``git -C <workspace_root>`` 并以工作区相对路径限定，结果只覆盖工作区；
   仓库根在工作区之上时在结果与 ``note`` 里写明，**不让人把"我看到的 diff"当成"仓库的全部改动"**。
2. **任何路径都不出现 ``git push``**：不接受任意子命令，argv 由代码用闭集参数拼出（路径、条数、message），
   一律 ``shell=False``；路径统一放在 ``--`` 之后（模型给的值永远不会被当成开关解析），另有一道
   :func:`_assert_safe` 兜底扫描，防止以后有人把模型输入拼进参数位。
3. **缺 ``git`` 就明确失败**：构造期用 ``shutil.which`` 判定（见 :func:`git_availability`）；缺失时工具
   **不绑定给模型**（能力不进有效集合），能力状态里给出"缺什么、怎么装"的可照做提示。
4. **提交必须人工审批**：审批由安全门（``security.policy``）判定，本模块只负责"叫得动 git 并如实汇报"；
   在 ``main`` / ``master`` 上提交需要显式确认；提交**不加** ``--no-verify``（尊重仓库自身 hook），
   hook 失败按"提交未成功"如实报告，不重试、不绕过。

子进程走**可注入的 runner**（默认是真实实现）：测试因此完全不依赖机器上装没装 git，也不碰真实仓库。
结果模型定义在本模块（T53 把 Web 的结果放在 ``result_types.py``）：那里正被另一处改动编辑，先不混入，
待其落地后再统一搬家——这是一处**已知的暂时偏离**，已记在计划里。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, Field

from local_agent.exceptions import SecurityError
from local_agent.security.path_guard import resolve_within_workspace
from local_agent.security.policy import SecurityPolicy

#: 单次 git 调用的默认超时（秒）。超时按"失败"如实报告，不重试。
DEFAULT_TIMEOUT_SECONDS = 20.0
#: 单次输出默认上限（字符）。触达上限必须置 ``truncated`` 并在 ``note`` 里写明结果不完整（§6.7 口径）。
DEFAULT_MAX_OUTPUT_CHARS = 20_000
#: 提交信息长度上限：够写清一次提交，又不至于把上下文烧掉。
MESSAGE_MAX_CHARS = 2000
#: 提交信息里不允许出现的控制字符（换行除外——多行说明是正常用法）。
_FORBIDDEN_MESSAGE_CHARS = ("\x00",)
#: 默认分支：在上面提交需要模型显式确认（契约 §16.4）。
DEFAULT_BRANCHES = ("main", "master")
#: 永远不该出现在 argv 里的开关与子命令（含"改环境"与远程写操作）。代码自己用的前缀见 _assert_safe。
_FORBIDDEN_TOKENS = (
    "push",
    "--force",
    "-f",
    "--amend",
    "--no-verify",
    "--git-dir",
    "--work-tree",
    "--exec-path",
    "--upload-pack",
    "--receive-pack",
    "remote",
    "fetch",
    "pull",
    "reset",
    "checkout",
    "switch",
    "merge",
    "rebase",
    "cherry-pick",
    "filter-branch",
    "gc",
    "clean",
    "stash",
    "apply",
    "am",
    "daemon",
    "config",
)
#: 代码自己的参数前缀（非模型可控）：让非 ASCII 路径不被转义，中文文件名才看得懂。
_CODE_PREFIX = ("-c", "core.quotepath=false")


class GitError(Exception):
    """Git 调用被边界拒绝或失败；消息面向模型，可直接作为工具失败原因。"""


@dataclass(frozen=True)
class GitRun:
    """runner 的返回：退出码、标准输出、标准错误与是否超时。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


#: runner 契约：``(args, cwd, timeout_seconds) -> GitRun``；``args`` **不含** 开头的 ``git`` 与 ``-C``。
GitRunner = Callable[[Sequence[str], Path, float], GitRun]


@dataclass(frozen=True)
class GitAvailability:
    """``git`` 可执行文件是否可用，以及不可用时给用户看的可照做提示。"""

    available: bool
    hint: str | None = None


def git_availability(*, which: Callable[[str], str | None] = shutil.which) -> GitAvailability:
    """判定 ``git`` 是否存在（构造期调用一次，不在每次工具调用里重复探测）。"""
    if which("git"):
        return GitAvailability(available=True)
    return GitAvailability(
        available=False,
        hint=(
            "未找到 git 可执行文件：请安装 Git（Windows: https://git-scm.com/download/win ，"
            "macOS: `brew install git` ，Debian/Ubuntu: `sudo apt install git`）后重启本应用；"
            # 刻意不点工具名：这段提示会出现在**页面**的能力状态区块，那里只讲能力不讲工具（§16.6）。
            "安装前 Git 相关能力不可用。"
        ),
    )


def _decode(data: bytes) -> str:
    """git 的输出按 **UTF-8** 解码，不按系统首选编码。

    2026-10-09 真实跑出来的缺陷：本机（Windows + GBK）用 ``locale.getpreferredencoding()`` 解码，
    中文提交信息变成 ``璁板繂搴撹矾寰勬寜 base_dir``——git 的路径与提交信息本来就是 UTF-8
    （``i18n.logOutputEncoding`` 默认 UTF-8），按 GBK 解释不会报错，只会**静默给出错字**。
    所以这里固定 UTF-8；解不出来的字节用 ``replace`` 如实标出，不偷偷换编码。
    """
    return data.decode("utf-8", errors="replace")


def run_git(args: Sequence[str], cwd: Path, timeout_seconds: float) -> GitRun:
    """默认 runner：``git -C <cwd> <args>``，``shell=False``，超时即终止。"""
    argv = ["git", "--literal-pathspecs", "-C", str(cwd), *args]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            shell=False,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or b""
        return GitRun(
            exit_code=-1,
            stdout=_decode(partial),
            stderr=f"超时：{timeout_seconds:g} 秒内没有结束",
            timed_out=True,
        )
    except OSError as exc:
        raise GitError(f"无法启动 git：{type(exc).__name__}: {exc}") from exc
    return GitRun(
        exit_code=completed.returncode,
        stdout=_decode(completed.stdout),
        stderr=_decode(completed.stderr),
    )


def _assert_safe(args: Sequence[str]) -> None:
    """兜底扫描：argv 里出现远程写操作或"改环境"的开关就拒绝（契约 §16.4 的"闭集"）。

    允许开头由**代码**放一个 :data:`_CODE_PREFIX`（``-c core.quotepath=false``）：它决定路径怎么显示，
    不是模型能控制的东西。除此之外任何 ``push`` / ``--git-dir`` / ``--no-verify`` 一律拒绝——
    即使它们今天不可能由参数拼出来，这道检查也让"以后有人改成拼接字符串"这条路走不通。
    """
    rest = list(args)
    if tuple(rest[: len(_CODE_PREFIX)]) == _CODE_PREFIX:
        rest = rest[len(_CODE_PREFIX) :]
    for token in rest:
        # 既要挡 `--amend`，也要挡 `--git-dir=/x` 这种**带等号**的写法（只比对整串会漏掉它）。
        if token in _FORBIDDEN_TOKENS or any(
            token.startswith(f"{forbidden}=") for forbidden in _FORBIDDEN_TOKENS if forbidden.startswith("-")
        ):
            raise GitError(f"拒绝执行：参数里不允许出现 {token!r}（契约 §16.4 的参数闭集）")


class _Git:
    """一次调用里的公共部分：runner、工作区根、超时与输出上限。"""

    def __init__(
        self,
        policy: SecurityPolicy,
        *,
        runner: GitRunner,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> None:
        self.policy = policy
        self.workspace_root = Path(policy.workspace_root)
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def call(self, *args: str, code_prefix: bool = True) -> GitRun:
        """执行一次 git 调用；失败（非零退出或超时）抛 :class:`GitError`。"""
        argv = [*_CODE_PREFIX, *args] if code_prefix else list(args)
        _assert_safe(argv)
        run = self.runner(argv, self.workspace_root, self.timeout_seconds)
        if run.timed_out:
            raise GitError(
                f"git {' '.join(args[:2])} 超时（{self.timeout_seconds:g} 秒）：{run.stderr.strip() or '已终止'}"
            )
        if run.exit_code != 0:
            detail = (run.stderr or run.stdout).strip() or f"退出码 {run.exit_code}"
            raise GitError(f"git {' '.join(args[:2])} 失败：{detail}")
        return run

    def repo_root(self) -> Path:
        """仓库根；工作区不是仓库（或其内部）时抛 :class:`GitError`。

        这里**不走** :meth:`call`：非仓库是"常见的正常情形"，需要的是可照做的提示，
        而不是一句 ``git rev-parse ... 失败``。
        """
        run = self.runner(["rev-parse", "--show-toplevel"], self.workspace_root, self.timeout_seconds)
        if run.timed_out:
            raise GitError(f"git rev-parse 超时（{self.timeout_seconds:g} 秒）：{run.stderr.strip()}")
        text = run.stdout.strip()
        if run.exit_code != 0 or not text:
            detail = (run.stderr or run.stdout).strip() or f"退出码 {run.exit_code}"
            raise GitError(
                "当前工作区不是 Git 仓库：请在该目录执行 `git init`，或把工作区切到仓库内部。"
                f"（git 原话：{detail}）"
            )
        return Path(text)

    def relative(self, path: str) -> str:
        """把工作区内的路径规范化成 git 命令用的相对路径（posix，禁止逃逸）。"""
        raw = (path or ".").strip() or "."
        resolved = resolve_within_workspace(
            raw, base_dir=self.workspace_root, workspace_root=self.workspace_root
        )
        relative = resolved.relative_to(self.workspace_root.resolve()).as_posix()
        if raw.startswith(":") or relative.startswith(":"):
            raise SecurityError("Git 路径必须是普通文件路径，不接受 pathspec magic")
        if relative in (".", ""):
            return "."
        if relative == ".git" or relative.startswith(".git/"):
            raise SecurityError("拒绝操作 Git 元数据目录（.git）")
        return relative

    def clip(self, text: str) -> tuple[str, bool, str | None]:
        """按上限截断，并给出"结果不完整"的说明（§6.7 口径）。"""
        if len(text) <= self.max_output_chars:
            return text, False, None
        note = (
            f"输出超过 {self.max_output_chars} 字符已截断：结果**不完整**。"
            "缩小 path 范围或减少条数后再看，不要据此认为没有别的改动。"
        )
        return text[: self.max_output_chars], True, note


# ---- 结果模型 --------------------------------------------------------------


class GitStatusEntry(BaseModel):
    """一条工作区状态：``code`` 是 porcelain 的两字符状态（如 ``M ` / ``??``）。"""

    code: str
    path: str
    original_path: str | None = None


class GitStatusResult(BaseModel):
    """``git_status`` 的返回结构（只覆盖**工作区**）。"""

    repo_root: str
    workspace: str
    #: 仓库根是否在**工作区之上**：为真时结果只是仓库的一部分，``note`` 会说明。
    repo_root_is_ancestor: bool = False
    branch: str | None = None
    entries: list[GitStatusEntry] = Field(default_factory=list)
    clean: bool = True
    truncated: bool = False
    note: str | None = None


class GitDiffResult(BaseModel):
    """``git_diff`` 的返回结构（只覆盖**工作区**）。"""

    path: str
    staged: bool = False
    empty: bool = False
    text: str = ""
    truncated: bool = False
    note: str | None = None


class GitLogEntry(BaseModel):
    """一条提交记录（不含文件清单，需要时用 ``git_diff`` 看具体改动）。"""

    commit: str
    short: str
    author: str
    date: str
    subject: str


class GitLogResult(BaseModel):
    """``git_log`` 的返回结构（只覆盖**工作区**）。"""

    path: str
    limit: int
    entries: list[GitLogEntry] = Field(default_factory=list)
    truncated: bool = False
    note: str | None = None


class GitCommitResult(BaseModel):
    """``git_commit`` 的返回结构。

    ``requires_confirmation=True`` 表示**停在默认分支的额外确认**上：本次没有执行任何 git 写操作，
    模型应向用户确认后再带 ``confirm_on_default_branch=True`` 重试。
    """

    branch: str | None = None
    committed: bool = False
    files: list[str] = Field(default_factory=list)
    commit: str | None = None
    message: str = ""
    requires_confirmation: bool = False
    note: str | None = None


# ---- 只读三件套 ------------------------------------------------------------


def _state(policy: SecurityPolicy, **kwargs: object) -> _Git:
    options: dict[str, object] = {
        "runner": run_git,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
    }
    options.update(kwargs)
    return _Git(policy, **options)  # type: ignore[arg-type]


def _scope_note(repo_root: Path, workspace: Path) -> str | None:
    if repo_root == workspace.resolve():
        return None
    return (
        f"本工作区是仓库 `{repo_root.as_posix()}` 的子目录：以下结果**只覆盖工作区路径**，"
        "不代表仓库其它位置的改动。"
    )


def _parse_status_z(text: str) -> list[GitStatusEntry]:
    """解析 ``git status --porcelain=v1 -z``：``-z`` 下路径不做引号转义，中文名原样可见。"""
    fields = [item for item in text.split("\0") if item]
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        original: str | None = None
        if code[0] in ("R", "C") and index < len(fields):
            original = fields[index]
            index += 1
        entries.append(GitStatusEntry(code=code, path=path, original_path=original))
    return entries


def status(
    policy: SecurityPolicy,
    *,
    runner: GitRunner = run_git,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> GitStatusResult:
    """工作区状态（staged / unstaged / untracked），限定在工作区路径内。"""
    git = _state(
        policy, runner=runner, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars
    )
    repo_root = git.repo_root()
    branch_run = git.runner(
        ["rev-parse", "--abbrev-ref", "HEAD"], git.workspace_root, git.timeout_seconds
    )
    branch = branch_run.stdout.strip() or None
    run = git.call("status", "--porcelain=v1", "-z", "--", ".")
    entries = _parse_status_z(run.stdout)
    text, truncated, clip_note = git.clip(run.stdout)
    notes = [note for note in (_scope_note(repo_root, git.workspace_root), clip_note) if note]
    if truncated:
        # 截断后按行解析不可靠：只保留能完整解析的部分。
        entries = _parse_status_z(text.rsplit("\0", 1)[0] + "\0")
    return GitStatusResult(
        repo_root=repo_root.as_posix(),
        workspace=git.workspace_root.as_posix(),
        repo_root_is_ancestor=repo_root != git.workspace_root.resolve(),
        branch=branch,
        entries=entries,
        clean=not entries,
        truncated=truncated,
        note=" ".join(notes) or None,
    )


def diff(
    policy: SecurityPolicy,
    *,
    path: str = ".",
    staged: bool = False,
    runner: GitRunner = run_git,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> GitDiffResult:
    """工作区／暂存区的差异文本（默认未暂存的改动）。"""
    git = _state(
        policy, runner=runner, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars
    )
    # 先校验路径（越界/`.git` 立即失败，且不产生任何 git 调用），再问仓库。
    relative = git.relative(path)
    repo_root = git.repo_root()
    args = ["diff"]
    if staged:
        args.append("--cached")
    args.extend(["--", relative])
    run = git.call(*args)
    text, truncated, clip_note = git.clip(run.stdout)
    notes = [
        note
        for note in (
            _scope_note(repo_root, git.workspace_root),
            "没有差异。" if not run.stdout.strip() else None,
            clip_note,
        )
        if note
    ]
    return GitDiffResult(
        path=relative,
        staged=staged,
        empty=not run.stdout.strip(),
        text=text,
        truncated=truncated,
        note=" ".join(notes) or None,
    )


def log(
    policy: SecurityPolicy,
    *,
    limit: int = 10,
    path: str = ".",
    runner: GitRunner = run_git,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> GitLogResult:
    """最近若干次提交（提交号、作者、日期、标题），限定在工作区路径内。"""
    if not 1 <= limit <= 100:
        raise GitError(f"limit 必须在 1~100 之间（收到 {limit}）")
    git = _state(
        policy, runner=runner, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars
    )
    relative = git.relative(path)
    repo_root = git.repo_root()
    run = git.call(
        "log",
        f"-n{limit}",
        "--date=short",
        "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1e",
        "--",
        relative,
    )
    entries: list[GitLogEntry] = []
    for chunk in run.stdout.split("\x1e"):
        parts = chunk.strip("\n").split("\x1f")
        if len(parts) != 5:
            continue
        entries.append(
            GitLogEntry(commit=parts[0], short=parts[1], author=parts[2], date=parts[3], subject=parts[4])
        )
    notes = [
        note
        for note in (
            _scope_note(repo_root, git.workspace_root),
            "该路径下还没有提交记录。" if not entries else None,
        )
        if note
    ]
    return GitLogResult(path=relative, limit=limit, entries=entries, note=" ".join(notes) or None)


# ---- 受控提交 --------------------------------------------------------------


def commit(
    policy: SecurityPolicy,
    *,
    paths: Sequence[str],
    message: str,
    confirm_on_default_branch: bool = False,
    runner: GitRunner = run_git,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> GitCommitResult:
    """把**显式列出**的工作区内路径提交到当前分支。

    只做四件事：``add -- <paths>`` → 确认确实有暂存内容 → ``commit -m <message> -- <paths>`` →
    ``log -1`` 回读。不做 amend / force / rebase / 分支切换，也不会出现 ``push``。
    argv 里路径一律跟在 ``--`` 之后，所以形如 ``--amend`` 的路径名只会被当成路径（并在解析时被拒）。
    """
    text = message.strip()
    if not text:
        raise GitError("提交信息不能为空")
    if len(text) > MESSAGE_MAX_CHARS:
        raise GitError(f"提交信息过长：{len(text)} 字符，上限 {MESSAGE_MAX_CHARS}")
    for char in _FORBIDDEN_MESSAGE_CHARS:
        if char in text:
            raise GitError("提交信息里包含不允许的控制字符")
    if not paths:
        raise GitError("必须显式列出要提交的路径（不支持 git add -A）")

    git = _state(
        policy, runner=runner, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars
    )
    branch_run = git.runner(
        ["rev-parse", "--abbrev-ref", "HEAD"], git.workspace_root, git.timeout_seconds
    )
    branch = branch_run.stdout.strip() or None
    if branch in DEFAULT_BRANCHES and not confirm_on_default_branch:
        return GitCommitResult(
            branch=branch,
            committed=False,
            files=[],
            requires_confirmation=True,
            note=(
                f"当前在默认分支 `{branch}` 上：这会改动主分支历史。"
                "请先向用户说明「要提交到哪个分支、包含哪些文件」，得到确认后再以 "
                "confirm_on_default_branch=true 重试。本次**没有执行任何 git 写操作**。"
            ),
        )

    relatives = [git.relative(item) for item in paths]
    git.call("add", "--", *relatives)
    staged = git.runner(
        ["diff", "--cached", "--quiet", "--", *relatives], git.workspace_root, git.timeout_seconds
    )
    if staged.exit_code == 0:
        return GitCommitResult(
            branch=branch,
            committed=False,
            files=relatives,
            note="这些路径没有可提交的改动（已全部与暂存区一致）：没有创建提交。",
        )
    git.call("commit", "-m", text, "--", *relatives)
    head = git.call("log", "-1", "--pretty=format:%H%x1f%s")
    parts = head.stdout.strip().split("\x1f")
    return GitCommitResult(
        branch=branch,
        committed=True,
        files=relatives,
        commit=parts[0] if parts else None,
        message=text,
        note=f"已提交到 `{branch}`（未执行任何推送；需要推送请自行操作）。",
    )


# ---- 工具装配 --------------------------------------------------------------

_STATUS_DOC = (
    "查看**当前工作区**的 Git 状态（分支、已暂存/未暂存/未跟踪的条目）。"
    "只覆盖工作区路径：工作区是仓库的子目录时，结果里会写明这一点，不要把它当成仓库的全部改动。"
)
_DIFF_DOC = (
    "查看**当前工作区**的差异文本。默认看未暂存的改动，`staged=true` 看已暂存的改动；"
    "`path` 限定范围（相对工作区，默认整个工作区）。输出过长会被截断并在 note 里说明。"
)
_LOG_DOC = (
    "查看**当前工作区**最近若干次提交（提交号、作者、日期、标题）。"
    "`limit` 1~100、`path` 限定范围。需要看某次改动的内容时用 git_diff，不要靠提交标题推断。"
)
_COMMIT_DOC = (
    "把**显式列出**的工作区内路径提交到当前分支（不支持 git add -A，也不会推送）。"
    "必须提供 `paths` 与 `message`；在 main/master 上提交会先返回"
    "“需要额外确认”，得到用户同意后再以 `confirm_on_default_branch=true` 重试。"
    "提交是否执行由人工审批决定，未获批准时不要反复重发。"
)


def build_git_tools(
    policy: SecurityPolicy,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    runner: GitRunner | None = None,
) -> list[BaseTool]:
    """构造四个 Git 工具。

    与 ``web_tools`` 同一口径：**工具总是被构造**（能力覆盖校验要求登记的工具都在注册表里），
    "能不能用"由三处决定——``git.enabled``（关闭时能力不进有效集合）、``git`` 可执行文件是否存在
    （见 :func:`git_availability`，缺失时能力同样不进有效集合）与 Profile 的 ``git_read`` / ``git_write``。
    """
    call_runner = run_git if runner is None else runner

    def _state_kwargs() -> dict[str, object]:
        return {
            "runner": call_runner,
            "timeout_seconds": timeout_seconds,
            "max_output_chars": max_output_chars,
        }

    @langchain_tool("git_status", description=_STATUS_DOC)
    def git_status() -> str:
        """查看当前工作区的 Git 状态。"""
        return status(policy, **_state_kwargs()).model_dump_json()  # type: ignore[arg-type]

    @langchain_tool("git_diff", description=_DIFF_DOC)
    def git_diff(path: str = ".", staged: bool = False) -> str:
        """查看工作区差异（默认未暂存的改动）。"""
        return diff(policy, path=path, staged=staged, **_state_kwargs()).model_dump_json()  # type: ignore[arg-type]

    @langchain_tool("git_log", description=_LOG_DOC)
    def git_log(limit: int = 10, path: str = ".") -> str:
        """查看最近若干次提交。"""
        return log(policy, limit=limit, path=path, **_state_kwargs()).model_dump_json()  # type: ignore[arg-type]

    @langchain_tool("git_commit", description=_COMMIT_DOC)
    def git_commit(
        paths: list[str], message: str, confirm_on_default_branch: bool = False
    ) -> str:
        """提交显式列出的文件（需要人工审批）。"""
        return commit(
            policy,
            paths=paths,
            message=message,
            confirm_on_default_branch=confirm_on_default_branch,
            **_state_kwargs(),  # type: ignore[arg-type]
        ).model_dump_json()

    return [git_status, git_diff, git_log, git_commit]
