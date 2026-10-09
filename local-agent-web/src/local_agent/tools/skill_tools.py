"""技能工具（架构 §6.12、§16.5；v0.5 T55）。

两个工具，都是**只读**（登记在 `security.policy.READ_ONLY_TOOLS`，不需要人工审批）：

- `skill_list`：取清单——**只有名字与一句描述**，不含正文、路径与来源；
- `skill_load`：按名字取正文（去掉 frontmatter）。

四条硬边界（契约里写死的，不是这里的自由发挥）：

1. **读取只发生在发现根内**：符号链接解析之后仍须落在根内，越界在**任何内容读取之前**拒绝。
   发现逻辑全在 :mod:`local_agent.context.skills_loader`，本模块不自己拼路径。
2. **每次调用重新发现、重新读盘**：正文**不缓存**——技能改了要立刻生效；名字与当前清单不再匹配时
   按"已不可用"如实报告，**不返回上一份内容**。
3. **技能里的脚本只作为资源**：本模块从不执行它们；要执行必须走 `shell` 能力与人工审批。
4. **不枚举技能目录**：只报"存在哪些资源目录"（`references/` / `scripts/` / `assets/`），不给文件清单——
   清单越界既费上下文，又容易让人以为资源都能直接读（它们在**工作区之外**时读文件工具打不开）。

结果模型定义在本模块：`tools/result_types.py` 正被另一处改动编辑（与 T54 的 `git_tools` 同一处理），
先不混入——这是一处**已知的暂时偏离**，已记在计划里。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, Field

from local_agent.config import SkillsConfig
from local_agent.context.skills_loader import SkillCatalog, discover_skills, load_skill_body

#: 技能目录里可能带的资源目录（**只报存在性，不枚举文件**）。
RESOURCE_DIRS: tuple[str, ...] = ("references", "scripts", "assets")

#: 清单为空时的说明：说清"为什么没有"，而不是给一个空数组让人猜。
_EMPTY_NOTE = (
    "当前没有发现任何技能：可能是开关关着（skills.enabled）、配置的发现根不存在，"
    "或那些目录里没有合法的技能（每个技能需要一个 SKILL.md，且 frontmatter 的 name 要与目录名一致）。"
)

_LIST_DOC = (
    "列出本机可用的**技能**（只有名字与一句描述）。技能是用户机器上已有的工作手册，"
    "正文要按需取——先看清单判断哪一条与当前任务有关，再用 skill_load 取它的正文。"
    "清单为空时会说明原因（开关、发现根或格式）。"
)
_LOAD_DOC = (
    "取一个技能的**正文**。`name` 必须是清单里出现过的名字。"
    "正文过长会被截断并标注；技能自带的脚本**不会被本工具执行**（它只作为文本资源）。"
    "取不到时如实报告是「没有这个技能」还是「已不可用」，不要臆造内容。"
)


class SkillEntry(BaseModel):
    """清单里的一条（名字 + 描述，不含路径）。"""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class SkillListResult(BaseModel):
    """`skill_list` 的结果。"""

    skills: list[SkillEntry] = Field(default_factory=list)
    #: 为空、被截断或发现过程有问题时的说明（一律说清原因）。
    note: str | None = None


class SkillLoadResult(BaseModel):
    """`skill_load` 的结果。"""

    name: str = Field(min_length=1)
    #: 技能正文（已去 frontmatter）；取不到时为空串，原因在 ``note`` 里。
    content: str = ""
    #: 该技能目录里存在的资源目录名（只报存在性）。
    resources: list[str] = Field(default_factory=list)
    note: str | None = None


def build_skill_tools(
    config: SkillsConfig,
    *,
    workspace_root: Path | None = None,
    current_cwd: Path | None = None,
    home: Path | None = None,
) -> list[BaseTool]:
    """构造两个技能工具。

    与 `web_tools` / `git_tools` 同一口径：**工具总是被构造**（能力覆盖校验要求登记的工具都在注册表里），
    "能不能用"由能力层决定——`skills.enabled` 关闭时该能力不进有效集合，工具对模型不可见。

    Args:
        config: ``skills`` 配置节（发现根与两个上限都在里面）。
        workspace_root: 工作区根（本轮只用于**将来**的项目级发现，见 §16.5）。
        current_cwd: 当前工作目录（同上）。
        home: 用户主目录；默认 :func:`Path.home`（测试注入用）。
    """
    root = Path.cwd() if workspace_root is None else workspace_root

    def _catalog() -> SkillCatalog:
        return discover_skills(
            config, workspace_root=root, current_cwd=current_cwd, home=home
        )

    @langchain_tool("skill_list", description=_LIST_DOC)
    def skill_list() -> str:
        """列出可用的技能（名字与描述）。"""
        catalog = _catalog()
        skills, truncated = catalog.visible_within_budget(config.max_list_chars)
        entries = [SkillEntry(name=skill.name, description=skill.description) for skill in skills]
        notes: list[str] = []
        if not entries:
            notes.append(_EMPTY_NOTE)
        elif truncated:
            notes.append(
                f"清单过长已截断（共 {len(catalog.model_visible)} 个技能，本次只列出前 {len(entries)} 个）；"
                "需要看全就在配置里调大 skills.max_list_chars。"
            )
        if catalog.diagnostics:
            notes.append(f"发现过程中有 {len(catalog.diagnostics)} 条被跳过的记录（格式或越界），不在清单里。")
        return SkillListResult(skills=entries, note=" ".join(notes) or None).model_dump_json()

    @langchain_tool("skill_load", description=_LOAD_DOC)
    def skill_load(name: str) -> str:
        """取一个技能的正文。"""
        catalog = _catalog()
        skill = catalog.get(name)
        if skill is None:
            return SkillLoadResult(name=name, note=_missing_note(name, catalog)).model_dump_json()

        content, truncated = load_skill_body(skill, max_chars=config.max_skill_chars)
        notes: list[str] = []
        if truncated:
            notes.append(
                f"正文超过上限（skills.max_skill_chars={config.max_skill_chars}）已截断，"
                "省略部分请看标记；需要全文请让用户提供或调大上限。"
            )
        resources = _resource_dirs(skill.path)
        if resources:
            notes.append(
                f"该技能自带资源目录：{'、'.join(resources)}。它们**只作为文本资源**，本工具不会执行其中的脚本；"
                "资源不在工作区内时读文件工具也打不开，需要内容请让用户提供。"
            )
        return SkillLoadResult(
            name=skill.name, content=content, resources=list(resources), note=" ".join(notes) or None
        ).model_dump_json()

    return [skill_list, skill_load]


def _missing_note(name: str, catalog: SkillCatalog) -> str:
    """取不到技能时说清是哪一种情况（**不泄露路径**）。"""
    if name in catalog.by_name:
        return (
            f"技能 {name} 被标记为只允许用户手动调用（disable-model-invocation），本档不能加载它。"
        )
    available = "、".join(skill.name for skill in catalog.model_visible)
    if not available:
        return f"没有名为 {name} 的技能；当前清单是空的。{_EMPTY_NOTE}"
    return f"没有名为 {name} 的技能。当前可用的是：{available}。"


def _resource_dirs(skill_path: Path) -> tuple[str, ...]:
    """技能目录里存在的资源目录名（**只报存在性，不枚举文件**）。"""
    directory = skill_path.parent
    found: list[str] = []
    for name in RESOURCE_DIRS:
        try:
            if (directory / name).is_dir():
                found.append(name)
        except OSError:  # pragma: no cover - 权限等极端情况
            continue
    return tuple(found)
