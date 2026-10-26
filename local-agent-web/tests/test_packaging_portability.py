r""""发布物里不该有开发机的实情"这条规矩本身要可移植（2026-10-16）。

背景：`scripts/build_windows_package.py` 的 `FORBIDDEN_PATTERNS` 原来写死了**开发机上的具体路径**
（形如 `X:\某个目录`，共四条）。那些字面量换一台机器构建时**既不会命中、也帮不上忙**，还把开发机的
目录结构带进了仓库（朋友拿到源码第一眼就看见开发机的盘符）。

改成"按构建机现算"之后，风险换了个方向：**规则可能退化成空表**（自检等于没有）。所以本用例钉两头：
① 不再有"某个盘符下的具体目录"这种字面量；② 仍然真的在算本地布局（调用点必须在）。
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "build_windows_package.py"
)


def test_no_hardcoded_machine_paths_left_in_the_packaging_rules() -> None:
    """规则里不能再出现 `E:\\某个目录` 这类字面量——那是开发机的实情，不是项目的规则。"""
    text = SCRIPT.read_text(encoding="utf-8")

    literals = re.findall(r"[A-Za-z]:\\+[A-Za-z_][A-Za-z0-9_]*", text)

    assert literals == [], f"打包规则里残留了机器相关字面量：{literals}"


def test_the_local_layout_rule_is_derived_from_the_build_machine() -> None:
    """本地布局必须**现算**：同级检出目录与用户主目录，而不是一张写死的名单。"""
    text = SCRIPT.read_text(encoding="utf-8")

    assert "_local_layout_patterns()" in text, "FORBIDDEN_PATTERNS 必须用它来收本地布局"
    assert "PROJECT_ROOT.parent" in text, "同级检出目录（同机其它项目）要算进去"
    assert "Path.home()" in text, "用户主目录要算进去（且只警告不硬失败）"


def test_the_shipped_skills_folder_exists_and_is_copied_into_the_package() -> None:
    """技能**内容随包走**：仓库里有一份，打包脚本把它复制到包顶层（2026-10-16）。

    为什么内容不进设置库：技能是 `SKILL.md` + 资源文件——要能编辑、能被 git diff、能被用户自己往里
    加一条；库里只存"哪个被用户关掉了"（`agent.skills.disabled` 例外表）。这条守的是两者不跑偏。
    """
    # 技能源的**唯一**位置是仓库根的 `skills/`（2026-10-20 去重：原先 `packaging/skills`
    # 与它逐字节重复，改一处忘另一处就会漂移，那份副本已删）。
    root = SCRIPT.parents[1]
    skills = root.parent / "skills"

    assert skills.is_dir() and any(skills.iterdir()), "仓库里要有一份可发布的技能"

    text = SCRIPT.read_text(encoding="utf-8")
    assert "SKILLS_SOURCE" in text and "_copy_skills()" in text
    assert 'PROJECT_ROOT.parent / "skills"' in text


def test_the_example_config_points_at_a_relative_skills_folder() -> None:
    """示例配置用**相对路径**指向包内的 `skills/`：解压到哪个盘都能用，不用手改绝对路径。"""
    example = SCRIPT.parents[1] / "config.yaml.example"

    assert 'user_dirs: ["skills"]' in example.read_text(encoding="utf-8")


def test_the_example_config_does_not_ship_a_stranger_drive_letter() -> None:
    """示例配置是**给别人抄的模板**：里面不能出现开发机的盘符路径。

    原来 `embedding.cache_dir` 写的是开发机的 `E:/hf-cache/...`——朋友照抄到自己的机器，
    只会得到"模型下不动"的困惑。
    """
    example = SCRIPT.parents[1] / "config.yaml.example"
    text = example.read_text(encoding="utf-8")

    # 允许注释里出现"例如 C:/Users/你/…"这种**格式说明**，但不允许出现具体盘符下的目录名。
    assert "E:/hf-cache" not in text
    assert "E:\\hf-cache" not in text


def test_mcp_credentials_are_rejected_by_the_actual_package_scan(tmp_path, monkeypatch) -> None:
    import runpy
    module = runpy.run_path(str(SCRIPT))
    scan = module["_scan_package"]
    monkeypatch.setitem(scan.__globals__, "PACKAGE_DIR", tmp_path)
    (tmp_path / "MCP.JSON").write_text('{"mcpServers": {}}')
    (tmp_path / "mcp.json.1234.tmp").write_text("credential")
    forbidden, _ = scan()
    assert any("MCP.JSON" in item for item in forbidden)
    assert any("mcp.json.1234.tmp" in item for item in forbidden)
