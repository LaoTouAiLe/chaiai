"""技能的发现与加载（架构 §16.5、工具定义 §6.12；v0.5 T55）。

与 :mod:`local_agent.context.agents_loader` **并列同构**，目的相反：AGENTS 是"每轮都发的项目规则"
（进稳定前缀），技能是"**清单常驻、正文按需**"——清单进运行时上下文快照，正文由 `skill_load`
作为工具结果返回，两者都不进稳定前缀。

发现顺序（rank 由小到大，同名先到者赢；默认根是代码里的常量，见 §16.5 的表）：

===========  ================================================  ==================
rank         根                                                默认
===========  ================================================  ==================
100          `<workspace_root>/.agents/skills`（项目级）        **不读**（本轮不做）
200          `SkillsConfig.user_dirs`（只接受绝对路径）          空
300          `~/.agents/skills`                                读
400          `~/.codex/skills`（跳过 `.system`）                读
500          `~/.trae-cn/**`、`~/.kiro/skills`、`~/.workbuddy`   存在即读
===========  ================================================  ==================

**项目级本轮不读**：仓库可能是别人给的，读它等于把外部内容请进上下文。将来要开，必须先补"项目级
正文按外部内容处理（`untrusted`、不得自动注入记忆）"的判定，那是另一次契约修订——在那之前**不设
开关**（不造空转的配置项）。

**市场缓存（如 `~/.codebuddy/skills-marketplace/skills`）默认也不读**：本机实测该处有 295 个技能，
按"名称 + 描述"列清单约 6 万字符，一份就吃光 `agents.max_total_chars`。要读就写进 `user_dirs`。

安全与口径（每条都对应契约里的措辞）：

1. **只在发现根内读取**：候选路径解析**符号链接之后**仍须落在该根内，否则拒绝整条并记诊断。
2. **名字必须是 kebab-case**，且**目录名 / 扁平文件名必须与 frontmatter 的 `name` 一致**——
   不一致拒绝整条（避免"改 A 却加载 B"）。
3. **一律按 UTF-8 读**：本机（GBK）按系统编码去解会把中文技能描述读成"看着像字、其实是错字"
   （与 §6.11 的 git 编码缺陷同一类）。
4. **每轮开跑前重新发现，不装文件监视器**：代价是同一轮内新建的技能下一轮才可见。
5. 截断复用 :func:`local_agent.context.agents_loader.truncate_middle`（同一套标记与口径）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from local_agent.config import SkillsConfig
from local_agent.context.agents_loader import truncate_middle

#: 技能名（也是它的寻址名）：kebab-case，与 §16.5 写死的正则一致。
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: frontmatter 的**白名单**：其余键作为 opaque metadata 保留，不进领域模型（§16.5）。
KNOWN_KEYS = frozenset(
    {"name", "description", "allowed-tools", "disable-model-invocation", "user-invocable"}
)

#: 「厂商工具名 → 本项目能力名」映射（§16.5）。它只表达"这个技能自己要用的能力"，**只能收窄、不会扩权**；
#: 认不出的条目丢弃并记诊断（各厂商命名不同，宁可少认也不要猜）。
TOOL_CAPABILITY_ALIASES: Mapping[str, str] = {
    "read": "files_read",
    "read_file": "files_read",
    "list_dir": "files_read",
    "ls": "files_read",
    "glob": "code_search",
    "grep": "code_search",
    "search": "code_search",
    "edit": "files_write",
    "edit_file": "files_write",
    "write": "files_write",
    "write_file": "files_write",
    "delete_file": "files_write",
    "bash": "shell",
    "shell": "shell",
    "terminal": "shell",
    "run_shell": "shell",
    "web": "web",
    "web_search": "web",
    "web_fetch": "web",
    "fetch": "web",
    "plan": "plan",
    "update_plan": "plan",
    "git_status": "git_read",
    "git_diff": "git_read",
    "git_log": "git_read",
    "git_commit": "git_write",
    "skill": "skill",
    "skill_list": "skill",
    "skill_load": "skill",
}

#: 个人级默认根（rank 300/400/500）。写成"相对 home 的路径 + rank + 展示用来源名"。
USER_ROOTS: tuple[tuple[int, str, str], ...] = (
    (300, ".agents/skills", "个人级（.agents）"),
    (400, ".codex/skills", "个人级（Codex 兼容）"),
    (500, ".trae-cn/builtin/global/skills", "个人级（Trae 内置）"),
    (500, ".trae-cn/builtin_skills", "个人级（Trae 内置）"),
    (500, ".kiro/skills", "个人级（Kiro）"),
    (500, ".workbuddy/skills", "个人级（WorkBuddy）"),
)

#: 保留命名空间：产品自带技能，不由我们加载（§16.5）。
RESERVED_DIRS = frozenset({".system"})

#: 项目级根（本轮**不读**，仅用于说明与将来的开关）。
PROJECT_SKILL_DIR = ".agents/skills"


@dataclass(frozen=True)
class SkillSummary:
    """一条已发现的技能摘要（**清单里只用到 `name` 与 `description`**）。"""

    name: str
    description: str
    #: 发现它的根（内部用，不发给模型）。
    root: Path
    #: 技能文件路径（目录包是 `SKILL.md`，扁平技能是 `<name>.md`）。
    path: Path
    #: 发现它的来源层（中文展示名，仅诊断用）。
    source: str
    rank: int
    #: `disable-model-invocation: true` 的技能**不进模型清单**（§16.5）。
    model_invocable: bool = True
    #: `user-invocable` 的归一化结果（本轮只有模型侧，保留字段以备用户侧入口）。
    user_invocable: bool = True
    #: `allowed-tools` 映射出的能力名（**只能收窄，不改变本档有效能力集**）。
    declared_capabilities: tuple[str, ...] = ()
    #: 白名单之外的 frontmatter（opaque，不进领域模型）。
    metadata: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SkillCatalog:
    """一次发现的完整结果。"""

    skills: tuple[SkillSummary, ...] = ()
    #: 被拒绝或降级的条目说明（给用户/日志看，不进提示）。
    diagnostics: tuple[str, ...] = ()

    @property
    def by_name(self) -> dict[str, SkillSummary]:
        return {skill.name: skill for skill in self.skills}

    @property
    def model_visible(self) -> tuple[SkillSummary, ...]:
        """模型清单里可见的技能（跳过 `disable-model-invocation`）。"""
        return tuple(skill for skill in self.skills if skill.model_invocable)

    def get(self, name: str) -> SkillSummary | None:
        """按名字取摘要；`disable-model-invocation` 的技能对模型**不可见**，等同不存在。"""
        skill = self.by_name.get(name)
        if skill is None or not skill.model_invocable:
            return None
        return skill

    def visible_within_budget(self, max_chars: int) -> tuple[tuple[SkillSummary, ...], bool]:
        """按字符预算取清单条目；返回 ``(条目, 是否被截断)``。

        `skill_list` 工具与快照段落**共用这一条预算规则**：两处各算一遍的话，同一份配置会给出两种
        "清单有多长"。第一条永远保留（截断到"一条都不剩"等于把功能关了，却看不出来）。
        """
        chosen: list[SkillSummary] = []
        used = 0
        for skill in self.model_visible:
            cost = len(skill.name) + len(_one_line(skill.description)) + 4  # "- " + "：" + 换行
            if chosen and used + cost > max_chars:
                return tuple(chosen), True
            chosen.append(skill)
            used += cost
        return tuple(chosen), False

    def render_listing(self, max_chars: int) -> tuple[str, bool]:
        """渲染给模型的清单：**只有名字与描述**，不含正文、路径、来源（§16.5）。

        Returns:
            ``(文本, 是否截断)``；超限时如实标注省掉了几个技能（不假装清单是完整的）。
        """
        skills, truncated = self.visible_within_budget(max_chars)
        text = "\n".join(f"- {skill.name}：{_one_line(skill.description)}" for skill in skills)
        if truncated:
            text = f"{text}\n…（清单已截断，省略 {len(self.model_visible) - len(skills)} 个技能）…"
        return text, truncated


def discover_skills(
    config: SkillsConfig,
    *,
    workspace_root: Path,
    current_cwd: Path | None = None,
    home: Path | None = None,
) -> SkillCatalog:
    """扫描各发现根，返回合并后的技能目录。

    Args:
        config: ``skills`` 配置节。
        workspace_root: 工作区根（本轮只用于将来的项目级根与展示）。
        current_cwd: 当前工作目录（保留参数：项目级根开启时需要它来定"项目根"）。
        home: 用户主目录；默认 :func:`Path.home`（测试可注入）。

    Returns:
        技能目录；没有任何技能时是空目录（不是 `None`——"没有技能"是正常状态）。
    """
    if not config.enabled:
        return SkillCatalog()

    del current_cwd  # 项目级根本轮不读，参数保留以免将来改签名
    home_dir = Path.home() if home is None else home
    del workspace_root  # 同上：项目级根未启用，避免"看起来读了"的错觉

    diagnostics: list[str] = []
    winners: dict[str, SkillSummary] = {}
    seen_paths: set[Path] = set()

    for rank, root, source in _roots(config, home_dir):
        resolved_root = _resolved(root)
        if resolved_root is None or not resolved_root.is_dir():
            continue
        for candidate in _candidates(resolved_root, diagnostics):
            skill = _parse_candidate(
                candidate, resolved_root=resolved_root, rank=rank, source=source, diagnostics=diagnostics
            )
            if skill is None:
                continue
            key = _resolved(skill.path)
            if key is not None and key in seen_paths:
                continue  # 同一个文件被两个根覆盖（兼容链接）：只算一次
            if key is not None:
                seen_paths.add(key)
            existing = winners.get(skill.name)
            if existing is not None:
                # rank 小的先到先赢（§16.5）；同名并列时保留先到的并记一条诊断。
                diagnostics.append(
                    f"技能名重复：{skill.name}（保留 {existing.source}，忽略 {skill.source}）"
                )
                continue
            winners[skill.name] = skill

    return SkillCatalog(
        skills=tuple(winners[name] for name in sorted(winners)),
        diagnostics=tuple(diagnostics),
    )


def load_skill_body(skill: SkillSummary, *, max_chars: int) -> tuple[str, bool]:
    """读取技能正文（**去掉 frontmatter**），按 `max_chars` 截断。

    每次调用都重新读盘：**正文不缓存**（§16.5）——技能改了要立刻生效，缓存会让模型拿到旧内容。

    Returns:
        ``(正文, 是否截断)``；读不到时返回一句可读的说明（不抛异常，也不假装空技能）。
    """
    text, truncated, problem = _read_text_capped(skill.path, max_chars=max_chars, strip=True)
    if problem is not None:
        return f"（该技能文件无法读取：{problem}）", False
    return text, truncated


def load_resource_text(
    skill: SkillSummary, resource: str, *, max_chars: int
) -> tuple[str, bool, str | None]:
    """读取技能目录内的一个**资源文件**（§6.12、§16.5）。

    与正文同一条读取与截断路径（`truncate_middle`），差别只有两点：不做 frontmatter 剥离（资源通常不是技能
    正文），以及**先做越界判定再读**（:func:`resolve_resource`）。

    Returns:
        ``(文本, 是否截断, 给模型看的说明)``；说明非空时文本为空串。
    """
    resolution = resolve_resource(skill, resource)
    if not resolution.ok:
        return "", False, resolution.problem
    assert resolution.path is not None  # 上面已保证
    text, truncated, problem = _read_text_capped(resolution.path, max_chars=max_chars, strip=False)
    if problem is not None:
        return "", False, f"{resource.strip()}：{problem}"
    return text, truncated, None


def _read_text_capped(
    path: Path, *, max_chars: int, strip: bool
) -> tuple[str, bool, str | None]:
    """按 UTF-8 读文本并截断；失败时返回 `problem` 而不是抛异常或编内容。"""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "", False, "无法按 UTF-8 文本读取（可能不是文本文件）"
    except OSError as exc:
        return "", False, f"读取失败（{type(exc).__name__}）"
    body = strip_frontmatter(text) if strip else text
    if len(body) <= max_chars:
        return body, False, None
    return truncate_middle(body, max_chars), True, None


@dataclass(frozen=True)
class ResourceResolution:
    """一次资源路径解析的结果：要么给路径，要么给"为什么不给"。"""

    path: Path | None = None
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None


def resolve_resource(skill: SkillSummary, resource: str) -> ResourceResolution:
    """把 `resource`（技能目录内的相对路径）解析成一个可读文件；越界一律拒绝。

    这是本项目**唯一**一条读工作区之外文件的路径，所以判定顺序刻意是"先拒绝、再读"：

    1. 空、绝对路径、含 `..` 的路径直接拒绝（不尝试规范化后再判断——那种写法容易漏掉一处）；
    2. 解析**符号链接之后**仍须落在**该技能自己的目录**内（借道链接读到别处也算越界）；
    3. 目录不是文件（要读目录请让用户说明要哪个文件）。

    Returns:
        路径 + 无问题，或 ``problem`` 里一句给模型看的说明。
    """
    raw = resource.strip()
    if not raw:
        return ResourceResolution(problem="resource 不能为空。")
    candidate = Path(raw)
    if candidate.is_absolute():
        return ResourceResolution(
            problem="resource 必须是**该技能目录内的相对路径**（不接受绝对路径）。"
        )
    if ".." in candidate.parts:
        return ResourceResolution(problem="resource 不允许出现 `..`（不能用它跳出技能目录）。")

    skill_dir = skill.path.parent
    try:
        skill_root = skill_dir.resolve()
        target = (skill_dir / candidate).resolve()
    except OSError:  # pragma: no cover - 极少数无法解析的路径
        return ResourceResolution(problem="该路径无法解析，已拒绝。")

    if target != skill_root and skill_root not in target.parents:
        return ResourceResolution(
            problem="该路径不在这个技能的目录内（解析符号链接之后仍然越界），已拒绝。"
        )
    if not target.is_file():
        if target.is_dir():
            return ResourceResolution(problem=f"{raw} 是一个目录，不是文件；请指定目录里的某个文件。")
        return ResourceResolution(problem=f"这个技能目录里没有 {raw}。")
    return ResourceResolution(path=target)


def strip_frontmatter(text: str) -> str:
    """去掉开头那段 `---` 包裹的 frontmatter，返回正文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip("\n")
    return text  # 没有闭合的 `---`：按"没有 frontmatter"处理，正文照给


def _roots(config: SkillsConfig, home_dir: Path) -> list[tuple[int, Path, str]]:
    """按 rank 排好的发现根（不含本轮明确不读的那些）。"""
    roots: list[tuple[int, Path, str]] = [(200, Path(value), "自定义根") for value in config.user_dirs]
    if config.include_user_skills:
        roots.extend((rank, home_dir / relative, source) for rank, relative, source in USER_ROOTS)
    return sorted(roots, key=lambda item: item[0])


def _candidates(root: Path, diagnostics: list[str]) -> list[Path]:
    """该根下的候选技能文件：目录包 `<name>/SKILL.md` 与扁平 `<name>.md`（**不递归**）。"""
    found: list[Path] = []
    try:
        entries: Iterable[Path] = sorted(root.iterdir())
    except OSError as exc:
        diagnostics.append(f"技能根无法读取：{root.as_posix()}（{type(exc).__name__}）")
        return found
    for entry in entries:
        if entry.name in RESERVED_DIRS:
            continue  # 保留命名空间：产品自带技能，不由我们加载
        if entry.is_dir():
            skill_file = entry / "SKILL.md"
            if skill_file.is_file():
                found.append(skill_file)
            continue
        if entry.is_file() and entry.suffix.lower() == ".md":
            found.append(entry)
    return found


def _parse_candidate(
    path: Path,
    *,
    resolved_root: Path,
    rank: int,
    source: str,
    diagnostics: list[str],
) -> SkillSummary | None:
    """解析一个候选；不合法时记诊断并返回 `None`（**拒绝整条，不降级**）。"""
    resolved_path = _resolved(path)
    if resolved_path is None or resolved_root not in resolved_path.parents:
        # 解析符号链接之后跑到根外了：必须在**任何内容读取之前**拒绝（§16.5）。
        diagnostics.append(f"技能路径超出发现根，已拒绝：{path.name}（{source}）")
        return None

    display = f"{path.parent.name}/{path.name}" if path.name == "SKILL.md" else path.name
    raw = _read_text(resolved_path)
    if raw is None:
        diagnostics.append(f"技能文件无法读取：{display}（{source}）")
        return None

    result = _split_frontmatter(raw)
    if result is None:
        diagnostics.append(f"技能缺少可解析的 frontmatter，已拒绝：{display}（{source}）")
        return None
    front, _body = result

    name = str(front.get("name", "")).strip()
    description = _one_line(str(front.get("description", "")))
    expected = path.parent.name if path.name == "SKILL.md" else path.stem
    if not SKILL_NAME_PATTERN.match(expected):
        diagnostics.append(
            f"技能名不是 kebab-case（小写字母/数字/连字符），已拒绝：{display}（{source}）"
        )
        return None
    if name != expected:
        # 目录名与 frontmatter 名不一致会让"引用的名字"和"文件位置"分家，两侧都难查（§16.5）。
        diagnostics.append(
            f"技能名与位置不一致（目录/文件名 {expected} vs frontmatter name {name}），已拒绝：{display}"
        )
        return None
    if not description:
        diagnostics.append(f"技能缺少 description（清单靠它判断何时使用），已拒绝：{display}")
        return None

    declared, unknown = _declared_capabilities(front.get("allowed-tools"))
    if unknown:
        diagnostics.append(
            f"技能 {name} 的 allowed-tools 有认不出的条目（已丢弃）：{'、'.join(unknown)}"
        )

    return SkillSummary(
        name=name,
        description=description,
        root=resolved_root,
        path=resolved_path,
        source=source,
        rank=rank,
        model_invocable=not _truthy(front.get("disable-model-invocation")),
        user_invocable=_truthy_default_true(front.get("user-invocable")),
        declared_capabilities=declared,
        metadata={key: value for key, value in front.items() if key not in KNOWN_KEYS} or None,
    )


def _split_frontmatter(raw: str) -> tuple[dict[str, object], str] | None:
    """拆出 frontmatter 映射与正文；没有 frontmatter 或不是映射时返回 `None`。"""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() != "---":
            continue
        try:
            loaded = yaml.safe_load("\n".join(lines[1:index])) or {}
        except yaml.YAMLError:
            return None
        if not isinstance(loaded, dict):
            return None
        return loaded, "\n".join(lines[index + 1 :])
    return None


def _declared_capabilities(value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把 `allowed-tools` 映射成能力名；返回 (已识别的能力, 认不出的条目)。"""
    if value is None:
        return (), ()
    raw_items: Sequence[object]
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw_items = value
    else:
        return (), (str(value),)

    found: list[str] = []
    unknown: list[str] = []
    for item in raw_items:
        text = str(item).strip()
        if not text:
            continue
        capability = TOOL_CAPABILITY_ALIASES.get(text.lower())
        if capability is None:
            unknown.append(text)
        elif capability not in found:
            found.append(capability)
    return tuple(found), tuple(unknown)


def _truthy(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1", "on"}


def _truthy_default_true(value: object) -> bool:
    """`user-invocable` 省略时为真（§16.5 的白名单口径）。"""
    return True if value is None else _truthy(value)


def _read_text(path: Path) -> str | None:
    """**固定 UTF-8** 读取（不用系统首选编码）。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - 极少数无法解析的路径
        return None


def _one_line(text: str) -> str:
    """描述单行化：清单是一行一条，换行会把它撑成多行（也避免正文被塞进描述）。"""
    return " ".join(text.split())
