"""技能工具的用例（v0.5 T55；契约 §6.12、§16.5）。

守三件事：① 清单只给"名字 + 描述"（不给正文、不给路径）；② 取不到时说清是哪一种情况；
③ 只读、不需要审批，且**每次调用重新读盘**（技能改了要立刻生效）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from local_agent.config import AppConfig, SkillsConfig
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.skill_tools import build_skill_tools

SKILL_MD = """---
name: {name}
description: {description}
---

# {name}

这是 {name} 的正文。
"""


def _tools(tmp_path: Path, **config: Any) -> dict[str, BaseTool]:
    """构造工具；`home` 注入临时目录，免得读到本机真实技能。"""
    settings = SkillsConfig(enabled=True, include_user_skills=False, user_dirs=[str(tmp_path)], **config)
    built = build_skill_tools(settings, home=tmp_path / "empty-home")
    return {tool.name: tool for tool in built}


def _write(root: Path, name: str, description: str = "做一件事", extra: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# {name}\n\n这是 {name} 的正文。\n",
        encoding="utf-8",
    )
    return path


def _payload(result: Any) -> dict[str, Any]:
    loaded = json.loads(result if isinstance(result, str) else str(result))
    assert isinstance(loaded, dict)
    return loaded


def test_skill_list_returns_name_and_description_only(tmp_path: Path) -> None:
    """清单只有名字与描述：**不含正文、不含路径**（§16.5、§15.8 的字段门槛）。"""
    _write(tmp_path, "release-checklist", "发布前检查清单")
    tools = _tools(tmp_path)

    payload = _payload(tools["skill_list"].invoke({}))

    assert payload["skills"] == [{"name": "release-checklist", "description": "发布前检查清单"}]
    assert payload["note"] is None
    serialized = json.dumps(payload, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "正文" not in serialized


def test_skill_list_explains_an_empty_catalog(tmp_path: Path) -> None:
    """清单为空要说清原因，不是给一个空数组让人猜。"""
    tools = _tools(tmp_path)

    payload = _payload(tools["skill_list"].invoke({}))

    assert payload["skills"] == []
    assert "没有发现任何技能" in payload["note"]


def test_skill_load_returns_the_body_and_notes_resources(tmp_path: Path) -> None:
    """取正文：正文不含 frontmatter；资源目录**只报存在性**，并说明脚本不会被执行。"""
    _write(tmp_path, "release-checklist", "发布前检查清单")
    (tmp_path / "release-checklist" / "references").mkdir()
    (tmp_path / "release-checklist" / "scripts").mkdir()
    (tmp_path / "release-checklist" / "scripts" / "run.py").write_text("# 不该被执行\n", encoding="utf-8")
    tools = _tools(tmp_path)

    payload = _payload(tools["skill_load"].invoke({"name": "release-checklist"}))

    assert payload["name"] == "release-checklist"
    assert payload["content"].startswith("# release-checklist")
    assert "description" not in payload["content"].splitlines()[0]
    assert payload["resources"] == ["references", "scripts"]
    assert "run.py" not in json.dumps(payload, ensure_ascii=False)  # 不枚举目录内容
    assert "不会执行其中的脚本" in payload["note"]


def test_skill_load_reports_an_unknown_name_with_the_available_list(tmp_path: Path) -> None:
    """取不到时如实报告，并列出当前可用的名字（不泄露路径）。"""
    _write(tmp_path, "release-checklist")
    tools = _tools(tmp_path)

    payload = _payload(tools["skill_load"].invoke({"name": "nope"}))

    assert payload["content"] == ""
    assert "没有名为 nope 的技能" in payload["note"]
    assert "release-checklist" in payload["note"]


def test_skill_load_refuses_a_manual_only_skill(tmp_path: Path) -> None:
    """`disable-model-invocation` 的技能对模型不可见，取它时**说清是哪一种情况**。"""
    _write(tmp_path, "manual-only", extra="disable-model-invocation: true\n")
    tools = _tools(tmp_path)

    listing = _payload(tools["skill_list"].invoke({}))
    assert listing["skills"] == []

    payload = _payload(tools["skill_load"].invoke({"name": "manual-only"}))
    assert "只允许用户手动调用" in payload["note"]


def test_skill_load_reads_a_resource_file(tmp_path: Path) -> None:
    """按需读技能目录内的资源（正文里引用它，模型才有办法取到）。"""
    _write(tmp_path, "release-checklist", "发布前检查清单")
    refs = tmp_path / "release-checklist" / "references"
    refs.mkdir()
    (refs / "steps.md").write_text("第一步：先跑发布闸门测试。\n", encoding="utf-8")
    tools = _tools(tmp_path)

    payload = _payload(
        tools["skill_load"].invoke({"name": "release-checklist", "resource": "references/steps.md"})
    )

    assert payload["resource"] == "references/steps.md"
    assert "第一步：先跑发布闸门测试" in payload["content"]
    assert "不会执行它" in payload["note"]


def test_skill_load_refuses_a_resource_outside_the_skill(tmp_path: Path) -> None:
    """越界资源（`..`/绝对路径）在读取之前被拒，正文与内容都是空的。"""
    _write(tmp_path, "release-checklist")
    _write(tmp_path, "another-skill")
    tools = _tools(tmp_path)

    for resource in ("../another-skill/SKILL.md", str(tmp_path / "x.md")):
        payload = _payload(
            tools["skill_load"].invoke({"name": "release-checklist", "resource": resource})
        )
        assert payload["content"] == ""
        assert "不允许出现 `..`" in payload["note"] or "不接受绝对路径" in payload["note"]


def test_skill_load_reports_a_missing_resource_and_a_directory(tmp_path: Path) -> None:
    _write(tmp_path, "release-checklist")
    refs = tmp_path / "release-checklist" / "references"
    refs.mkdir()
    tools = _tools(tmp_path)

    missing = _payload(
        tools["skill_load"].invoke({"name": "release-checklist", "resource": "references/nope.md"})
    )
    assert missing["content"] == "" and "没有" in missing["note"]

    directory = _payload(
        tools["skill_load"].invoke({"name": "release-checklist", "resource": "references"})
    )
    assert directory["content"] == "" and "是一个目录" in directory["note"]


def test_skill_load_rereads_the_file_every_call(tmp_path: Path) -> None:
    """正文**不缓存**：技能改了，下一次调用就取到新内容（§16.5）。"""
    path = _write(tmp_path, "release-checklist")
    tools = _tools(tmp_path)

    first = _payload(tools["skill_load"].invoke({"name": "release-checklist"}))
    path.write_text(
        "---\nname: release-checklist\ndescription: 发布前检查清单\n---\n\n改过的正文。\n",
        encoding="utf-8",
    )
    second = _payload(tools["skill_load"].invoke({"name": "release-checklist"}))

    assert "改过的正文" in second["content"]
    assert second["content"] != first["content"]


def test_skill_body_truncation_is_reported(tmp_path: Path) -> None:
    """正文超上限：截断并说明（不假装内容完整）。"""
    _write(tmp_path, "big-one")
    path = tmp_path / "big-one" / "SKILL.md"
    path.write_text(
        "---\nname: big-one\ndescription: 很长的正文\n---\n\n" + ("内容" * 300) + "\n",
        encoding="utf-8",
    )
    tools = _tools(tmp_path, max_skill_chars=500)

    payload = _payload(tools["skill_load"].invoke({"name": "big-one"}))

    assert "内容已截断" in payload["content"]
    assert "已截断" in payload["note"]


def test_skill_tools_are_read_only_and_need_no_approval(tmp_path: Path) -> None:
    """两个技能工具都是只读：安全门直接放行，不需要人工审批（§6.12）。"""
    policy = SecurityPolicy.from_config(
        AppConfig(), workspace_root=tmp_path, environ={}
    )

    for name in ("skill_list", "skill_load"):
        decision = policy.classify_tool(name, {"name": "x"} if name == "skill_load" else {})
        assert decision.allowed is True
        assert decision.requires_approval is False


def test_tools_are_still_constructed_when_disabled(tmp_path: Path) -> None:
    """关掉开关时工具**仍然构造**（能力覆盖校验要求），只是能力不进有效集合、不绑给模型。"""
    settings = SkillsConfig(enabled=False)
    built: Sequence[BaseTool] = build_skill_tools(settings, home=tmp_path)

    assert [tool.name for tool in built] == ["skill_list", "skill_load"]


def test_registry_tools_see_the_same_skills_config_as_the_snapshot(
    app_config: AppConfig, tmp_path: Path, model_factory: Any
) -> None:
    """走**真实装配路径**：配置里开着的技能，必须能被注册表里的工具取到。

    这条守的是"两个消费方同源"。2026-10-09 的真实运行验收撞到过相反的结果：快照里**列出了**技能
    （它读的是真实配置），而 `skill_load` 说"没有这个技能"（工具拿到的是**默认关闭**的配置）——
    根因是 `build_runtime_context` 漏传 `skills=app_config.skills`。清单与工具看同一份配置，缺一不可。
    """
    from local_agent.graph import builder

    root = tmp_path / "skills-root"
    _write(root, "release-checklist", "发布前检查清单")
    config = app_config.model_copy(
        update={
            "skills": SkillsConfig(
                enabled=True, include_user_skills=False, user_dirs=[str(root)]
            )
        }
    )
    context = builder.build_runtime_context(
        app_config=config,
        model_adapter=model_factory(decision="tool", reply="好"),
        workspace_root=tmp_path,
        current_cwd=tmp_path,
    )
    registry = context.tool_registry
    assert registry is not None

    listing = _payload(registry.get("skill_list").invoke({}))
    assert [item["name"] for item in listing["skills"]] == ["release-checklist"]

    loaded = _payload(registry.get("skill_load").invoke({"name": "release-checklist"}))
    assert "release-checklist 的正文" in loaded["content"]
