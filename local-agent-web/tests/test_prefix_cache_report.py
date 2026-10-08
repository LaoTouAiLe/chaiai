"""T51 缓存前缀报告与 Profile 指纹（契约 §16.1）。

验收观测量是**逐调用缓存读的回落次数**（目标 0~1 次），不是命中率——命中率会被轮次长度稀释，
回落次数才直接对应"前缀被重写了几次"。这里用合成记录把口径钉住，并验证：

- 回落能被归因到**具体变了哪一段**（工具 Schema / 系统提示 / 历史前缀 / 动态上下文）；
- "未上报缓存字段"不算回落，也不补零（未知不伪装成 0）；
- 工具 Schema 指纹在**同一 Profile 内稳定、跨 Profile 不同**——换 Profile 就是换前缀，属预期变化。

`scripts/` 不是包，脚本按路径加载（同 ``tests/test_memory.py`` 对 ``scripts/init_db.py`` 的做法）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from local_agent.capabilities import DEFAULT_PROFILE, PROFILES, tool_names_for
from local_agent.config import AppConfig
from local_agent.llm.usage_recording import tools_fingerprint
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.tool_registry import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, relative: str) -> ModuleType:
    spec: ModuleSpec | None = importlib.util.spec_from_file_location(
        name, PROJECT_ROOT / relative
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report_module() -> ModuleType:
    return _load_script("local_agent_report_prefix_cache", "scripts/report_prefix_cache.py")


def _call(
    index: int,
    *,
    read: int | None,
    uncached: int | None,
    tools: str = "tools-1",
    system: str = "system-1",
    history: str = "history-1",
    dynamic: str | None = "dynamic-1",
    reported: bool = True,
    purpose: str = "tool_decision",
) -> dict[str, Any]:
    return {
        "model_call_id": f"run:model:{index}",
        "purpose": purpose,
        "status": "ok",
        "cache_metrics_reported": reported,
        "actual_cache_read_tokens": read,
        "actual_uncached_input_tokens": uncached,
        "tools_schema_fingerprint": tools,
        "system_prompt_fingerprint": system,
        "history_prefix_fingerprint": history,
        "dynamic_context_fingerprint": dynamic,
    }


def _payload(calls: list[dict[str, Any]], *, profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
    return {
        "profile": profile,
        "profile_tool_names": list(tool_names_for("tool", PROFILES[profile])),
        "turns": [{"turn": 1, "model_calls": calls}],
    }


def test_growing_cache_read_is_not_a_fallback(report_module: ModuleType) -> None:
    payload = _payload(
        [
            _call(1, read=0, uncached=1200),
            _call(2, read=900, uncached=300),
            _call(3, read=1200, uncached=280),
        ]
    )

    report = report_module.build_report(payload)

    assert report["summary"]["fallbacks"] == 0
    assert report["summary"]["prefix_resets"] == 0
    assert report["verdict"] == "ok"


def test_fallback_is_attributed_to_the_segment_that_changed(report_module: ModuleType) -> None:
    payload = _payload(
        [
            _call(1, read=400, uncached=1200),
            _call(2, read=1000, uncached=300),
            # 第 3 次调用历史前缀被重写（同一 Profile 内的意外变化）→ 缓存读回落
            _call(3, read=300, uncached=1400, history="history-2"),
        ]
    )

    report = report_module.build_report(payload)

    assert report["summary"]["fallbacks"] == 1
    fallback = report["fallbacks"][0]
    assert fallback["from"] == 1000 and fallback["to"] == 300
    assert fallback["changed"] == ["历史前缀"]
    text = report_module.format_report(report)
    assert "回落明细" in text and "历史前缀" in text


def test_profile_switch_shows_up_as_a_prefix_reset_not_as_an_unexplained_drop(
    report_module: ModuleType,
) -> None:
    """工具 Schema 变了 = 换了 Profile：报告把它计为前缀重置，且归因写明是哪一段。"""
    payload = _payload(
        [
            _call(1, read=500, uncached=1000, tools="tools-coder"),
            _call(2, read=900, uncached=200, tools="tools-researcher", system="system-2"),
        ]
    )

    report = report_module.build_report(payload)

    assert report["summary"]["prefix_resets"] == 1
    assert report["rows"][1]["changed"] == ["工具Schema", "系统提示"]


def test_unreported_cache_fields_are_unknown_not_zero(report_module: ModuleType) -> None:
    payload = _payload(
        [
            _call(1, read=900, uncached=300),
            _call(2, read=None, uncached=None, reported=False),
        ]
    )

    report = report_module.build_report(payload)

    assert report["summary"]["unknown_cache_calls"] == 1
    assert report["summary"]["fallbacks"] == 0
    assert report["rows"][1]["cache_read"] is None


def test_threshold_exceeded_returns_exit_code_three(
    report_module: ModuleType, tmp_path: Path
) -> None:
    payload = _payload(
        [
            _call(1, read=1000, uncached=100),
            _call(2, read=200, uncached=900, history="history-2"),
            _call(3, read=900, uncached=100, history="history-3"),
            _call(4, read=100, uncached=900, history="history-4"),
        ]
    )
    result_path = tmp_path / "baseline_result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    code = report_module.main(["--result", str(result_path), "--max-fallbacks", "1"])

    assert code == 3


def test_missing_result_file_is_a_clean_failure(report_module: ModuleType, tmp_path: Path) -> None:
    assert report_module.main(["--result", str(tmp_path / "nope.json")]) == 2


def test_tools_fingerprint_is_stable_within_profile_and_distinct_across_profiles(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """工具 Schema 指纹：同 Profile 稳定（前缀可缓存）、跨 Profile 不同（换档即换前缀）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    registry = ToolRegistry.build(policy)

    fingerprints = {
        name: tools_fingerprint(registry.tools_for("tool", capabilities))
        for name, capabilities in PROFILES.items()
    }

    assert all(value is not None for value in fingerprints.values())
    assert len(set(fingerprints.values())) == len(PROFILES), "三档 Profile 的工具 Schema 必须互不相同"
    # 重复取用得到同一指纹：这正是不破坏缓存的前提
    assert tools_fingerprint(registry.tools_for("tool", PROFILES["coder"])) == fingerprints["coder"]
    assert tools_fingerprint(registry.tools_for("tool", PROFILES["researcher"])) == fingerprints[
        "researcher"
    ]
    # 闲聊模式的只读子集与各 Profile 都不同：模式也参与前缀
    assert tools_fingerprint(registry.tools_for("chat")) not in set(fingerprints.values())
    assert tools_fingerprint([]) is None
