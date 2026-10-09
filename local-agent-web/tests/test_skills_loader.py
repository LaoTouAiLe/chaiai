"""技能加载器的用例（v0.5 T55；契约 §16.5、工具定义 §6.12）。

全部用临时目录，**不碰本机真实技能**：真实目录的内容会变，拿它做断言等于把用例挂在别人的文件上。
这里守的是契约里写死的每一条边界——越界、名字、编码、截断、保留命名空间、不递归。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from local_agent.config import SkillsConfig
from local_agent.context.skills_loader import (
    SKILL_NAME_PATTERN,
    USER_ROOTS,
    discover_skills,
    load_skill_body,
    strip_frontmatter,
)

SKILL_TEMPLATE = """---
name: {name}
description: {description}
{extra}---

# {name}

正文第一行。
"""


def _write_skill(root: Path, name: str, *, description: str = "做一件事", extra: str = "") -> Path:
    """在 `root/<name>/SKILL.md` 写下一条技能，返回文件路径。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        SKILL_TEMPLATE.format(name=name, description=description, extra=extra), encoding="utf-8"
    )
    return path


def _config(tmp_path: Path, **overrides: Any) -> SkillsConfig:
    """默认"开着一个自定义根"的配置；`home` 会被注入到空的临时目录，免得读到本机技能。"""
    values: dict[str, Any] = {
        "enabled": True,
        "include_user_skills": False,
        "user_dirs": [str(tmp_path / "custom")],
    }
    values.update(overrides)
    return SkillsConfig(**values)


def test_disabled_returns_empty_catalog(tmp_path: Path) -> None:
    """关掉开关就是一个空目录——不是 None，也不去扫任何根。"""
    _write_skill(tmp_path / "custom", "release-checklist")
    catalog = discover_skills(_config(tmp_path, enabled=False), workspace_root=tmp_path, home=tmp_path)
    assert catalog.skills == ()
    assert catalog.render_listing(8_000) == ("", False)


def test_directory_bundle_and_flat_markdown_are_both_discovered(tmp_path: Path) -> None:
    """目录包 `<name>/SKILL.md` 与扁平 `<name>.md` 都收（照 DSH 的形态）。"""
    root = tmp_path / "custom"
    _write_skill(root, "release-checklist")
    (root / "db-migration.md").write_text(
        "---\nname: db-migration\ndescription: 数据迁移手册\n---\n\n正文。\n", encoding="utf-8"
    )

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert [skill.name for skill in catalog.skills] == ["db-migration", "release-checklist"]


def test_lower_rank_wins_on_duplicate_name(tmp_path: Path) -> None:
    """同名先到者赢（rank 小优先），并且记一条诊断——不静默丢。"""
    custom = tmp_path / "custom"
    user = tmp_path / ".agents" / "skills"
    _write_skill(custom, "code-review", description="自定义根里的那一份")
    _write_skill(user, "code-review", description="个人级里的那一份")

    catalog = discover_skills(
        _config(tmp_path, include_user_skills=True),
        workspace_root=tmp_path,
        home=tmp_path,
    )
    assert catalog.get("code-review") is not None
    assert catalog.by_name["code-review"].description == "自定义根里的那一份"
    assert any("技能名重复" in item for item in catalog.diagnostics)


def test_user_roots_are_read_when_enabled_and_skip_reserved_namespace(tmp_path: Path) -> None:
    """个人级根打开时被读；`.system`（产品自带）永远是保留命名空间。"""
    user = tmp_path / ".agents" / "skills"
    _write_skill(user, "team-review")
    _write_skill(user / ".system", "builtin-review")

    catalog = discover_skills(
        _config(tmp_path, include_user_skills=True), workspace_root=tmp_path, home=tmp_path
    )
    assert [skill.name for skill in catalog.skills] == ["team-review"]


def test_marketplace_cache_is_not_in_the_default_roots() -> None:
    """市场缓存目录**不在**默认根里：写成常量断言，避免哪天顺手加进去。"""
    assert all("skills-marketplace" not in relative for _rank, relative, _source in USER_ROOTS)


def test_non_kebab_case_directory_is_rejected(tmp_path: Path) -> None:
    """名字必须是 kebab-case（小写字母/数字/连字符）。"""
    root = tmp_path / "custom"
    _write_skill(root, "My_Skill")

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert catalog.skills == ()
    assert any("kebab-case" in item for item in catalog.diagnostics)
    assert not SKILL_NAME_PATTERN.match("My_Skill")


def test_directory_name_must_match_frontmatter_name(tmp_path: Path) -> None:
    """目录名与 frontmatter 名不一致时**拒绝整条**——避免"改 A 却加载 B"。"""
    root = tmp_path / "custom"
    directory = root / "release-checklist"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: release-check\ndescription: 名字不一致\n---\n\n正文。\n", encoding="utf-8"
    )

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert catalog.skills == ()
    assert any("与位置不一致" in item for item in catalog.diagnostics)


def test_missing_description_is_rejected(tmp_path: Path) -> None:
    """没有 description 就等于清单里没有它，那它永远无法被选中——拒绝并说明。"""
    directory = tmp_path / "custom" / "no-desc"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: no-desc\n---\n\n正文。\n", encoding="utf-8")

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert catalog.skills == ()
    assert any("description" in item for item in catalog.diagnostics)


def test_missing_frontmatter_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "custom" / "no-front"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("只有正文，没有 frontmatter。\n", encoding="utf-8")

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert catalog.skills == ()
    assert any("frontmatter" in item for item in catalog.diagnostics)


def test_chinese_description_survives_the_utf8_read(tmp_path: Path) -> None:
    """中文描述必须原样读出。

    2026-10-09 实测：本机（GBK 默认编码）用系统编码去读 `TRAE-code-review` 的中文描述，得到的是
    `浠呭綋鐢ㄦ埛鏄庣‘瑕佹眰…` 这种"看着像字、其实是错字"的结果，而且不抛异常——同一类缺陷见 §6.11。
    """
    root = tmp_path / "custom"
    _write_skill(root, "taiwan-review", description="仅当用户明确要求做代码审查时使用，返回结构化问题。")

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert catalog.by_name["taiwan-review"].description == (
        "仅当用户明确要求做代码审查时使用，返回结构化问题。"
    )


def test_nested_skill_files_are_not_discovered(tmp_path: Path) -> None:
    """不支持递归 `**/SKILL.md`：嵌套一层就不算技能（无边界嵌套发现不做）。"""
    nested = tmp_path / "custom" / "bundle" / "inner"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: inner\ndescription: 嵌套的\n---\n正文。\n", encoding="utf-8"
    )

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert catalog.skills == ()


def test_symlink_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    """解析符号链接之后跑出发现根 → 拒绝（必须在任何内容读取之前）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: escaped\ndescription: 根外内容\n---\n正文。\n", encoding="utf-8"
    )
    root = tmp_path / "custom"
    root.mkdir()
    link = root / "escaped"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 取决于权限
        pytest.skip(f"无法创建符号链接：{type(exc).__name__}")

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert catalog.skills == ()
    assert any("超出发现根" in item for item in catalog.diagnostics)


def test_disable_model_invocation_keeps_it_out_of_the_model_catalog(tmp_path: Path) -> None:
    """`disable-model-invocation: true` 的技能不进模型清单，取它也当"不存在"。"""
    root = tmp_path / "custom"
    _write_skill(root, "manual-only", extra="disable-model-invocation: true\n")

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    assert catalog.by_name["manual-only"].model_invocable is False
    assert catalog.model_visible == ()
    assert catalog.get("manual-only") is None


def test_allowed_tools_maps_to_capabilities_and_drops_unknown(tmp_path: Path) -> None:
    """`allowed-tools` 映射到能力名（只表达"它自己要用的"）；认不出的丢弃并记诊断。"""
    root = tmp_path / "custom"
    _write_skill(
        root,
        "review-flow",
        extra="allowed-tools: Read, Grep, Glob, Frobnicate\n",
    )

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    skill = catalog.by_name["review-flow"]
    assert skill.declared_capabilities == ("files_read", "code_search")
    assert any("allowed-tools" in item and "Frobnicate" in item for item in catalog.diagnostics)


def test_unknown_frontmatter_keys_stay_opaque(tmp_path: Path) -> None:
    """白名单之外的字段作为 opaque metadata 保留，不进领域模型。"""
    root = tmp_path / "custom"
    _write_skill(root, "with-meta", extra="license: MIT\nversion: 1.2.0\n")

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    metadata = catalog.by_name["with-meta"].metadata
    assert metadata == {"license": "MIT", "version": "1.2.0"}


def test_listing_has_only_name_and_description(tmp_path: Path) -> None:
    """清单**只有名字与描述**：不含正文、不含绝对路径、不含来源层（§16.5、§15.8）。"""
    root = tmp_path / "custom"
    _write_skill(root, "release-checklist", description="发布前检查清单")

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    text, truncated = catalog.render_listing(8_000)
    assert text == "- release-checklist：发布前检查清单"
    assert truncated is False
    assert str(tmp_path) not in text
    assert "正文第一行" not in text


def test_listing_truncation_is_marked(tmp_path: Path) -> None:
    """清单超限按 §6.7 口径截断并如实标注。"""
    root = tmp_path / "custom"
    _write_skill(root, "long-one", description="很长的描述" * 40)

    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    text, truncated = catalog.render_listing(60)
    assert truncated is True
    assert "内容已截断" in text


def test_load_body_strips_frontmatter_and_rereads_every_time(tmp_path: Path) -> None:
    """正文不含 frontmatter；**每次重新读盘**（改了立刻生效，不缓存）。"""
    root = tmp_path / "custom"
    path = _write_skill(root, "release-checklist")
    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)
    skill = catalog.by_name["release-checklist"]

    body, truncated = load_skill_body(skill, max_chars=20_000)
    assert body.startswith("# release-checklist")
    assert "description" not in body.splitlines()[0]
    assert truncated is False

    path.write_text(
        "---\nname: release-checklist\ndescription: 发布前检查清单\n---\n\n改过的正文。\n",
        encoding="utf-8",
    )
    body_again, _ = load_skill_body(skill, max_chars=20_000)
    assert "改过的正文" in body_again


def test_load_body_truncation_is_marked(tmp_path: Path) -> None:
    root = tmp_path / "custom"
    _write_skill(root, "big-one")
    path = root / "big-one" / "SKILL.md"
    path.write_text(
        "---\nname: big-one\ndescription: 很长的正文\n---\n\n" + ("内容" * 200) + "\n",
        encoding="utf-8",
    )
    catalog = discover_skills(_config(tmp_path), workspace_root=tmp_path, home=tmp_path)

    body, truncated = load_skill_body(catalog.by_name["big-one"], max_chars=100)
    assert truncated is True
    assert "内容已截断" in body


def test_unreadable_skill_file_reports_instead_of_raising(tmp_path: Path) -> None:
    """读不到时不抛异常、也不假装是空技能。"""
    from local_agent.context.skills_loader import SkillSummary

    missing = SkillSummary(
        name="gone",
        description="已经不在了",
        root=tmp_path,
        path=tmp_path / "custom" / "gone" / "SKILL.md",
        source="自定义根",
        rank=200,
    )
    body, truncated = load_skill_body(missing, max_chars=100)
    assert "无法读取" in body
    assert truncated is False


def test_strip_frontmatter_handles_missing_closing_marker() -> None:
    """没有闭合的 `---`：按"没有 frontmatter"处理，正文照给。"""
    assert strip_frontmatter("---\nname: x\n没有闭合") == "---\nname: x\n没有闭合"
    assert strip_frontmatter("直接就是正文") == "直接就是正文"
    assert strip_frontmatter("---\nname: x\n---\n\n正文") == "正文"


def test_relative_user_dir_is_rejected_at_config_time(tmp_path: Path) -> None:
    """`user_dirs` 只接受绝对路径：相对路径在**配置加载/构造时**就失败并说明原因。"""
    with pytest.raises(ValueError, match="只接受绝对路径"):
        SkillsConfig(enabled=True, user_dirs=["relative/skills"])


def test_missing_root_is_harmless(tmp_path: Path) -> None:
    """根不存在（没装那个工具）不是错误：跳过它，其余根照常工作。"""
    root = tmp_path / "custom"
    _write_skill(root, "keep-me")

    catalog = discover_skills(
        _config(tmp_path, user_dirs=[str(tmp_path / "custom"), str(tmp_path / "does-not-exist")]),
        workspace_root=tmp_path,
        home=tmp_path / "no-such-home",
    )
    assert [skill.name for skill in catalog.skills] == ["keep-me"]
