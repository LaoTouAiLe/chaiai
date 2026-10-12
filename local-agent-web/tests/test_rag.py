"""T21 验收：按需代码检索（架构 §4.3、§5.3、§7.2；开发文档 T21）。

全部使用临时工作区与替身：不联网、**不依赖本机是否安装 ``rg``**（``rg`` 路径用替身进程注入，
缺失路径用 ``resolve -> None`` 模拟），也不依赖 ``shutil.which`` 的真实结果。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from local_agent.config import AppConfig
from local_agent.context import prompt_context
from local_agent.graph.state import RAGReference
from local_agent.rag import retriever, search, snippets
from local_agent.security.policy import SecurityPolicy


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _rag_config(app_config: AppConfig, **overrides: object) -> AppConfig:
    data = app_config.model_dump()
    data["rag"].update({"enabled": True, **overrides})
    return AppConfig.model_validate(data)


def _policy(app_config: AppConfig, workspace: Path) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})


def _no_rg(*args: object, **kwargs: object) -> str | None:
    """解析不到 ``rg``（本机没装时的真实情形）。"""
    return None


def _find(
    query: str,
    *,
    workspace: Path,
    app_config: AppConfig,
    resolve: Any = _no_rg,
    **overrides: object,
) -> retriever.RetrievedCode:
    app = _rag_config(app_config, **overrides)
    return retriever.retrieve_code(
        query,
        config=app.rag,
        workspace_root=workspace,
        policy=_policy(app, workspace),
        resolve=resolve,
    )


# --------------------------------------------------------------------------- 词项提取


def test_extract_terms_prefers_identifiers_over_chinese_filler() -> None:
    """有文件名/标识符时**只用**它们：中文虚词当词项会按字面命中中文散文，把结果位吃光。"""
    terms = search.extract_terms("routers.py 里的 decide_mode 是怎么判断模式的？", limit=4)

    assert terms == ["routers.py", "decide_mode"]


def test_extract_terms_keeps_chinese_terms_for_pure_chinese_queries() -> None:
    """纯中文请求没有任何 ASCII 词项，这时才退回中文片段——否则等于什么都没搜。"""
    terms = search.extract_terms("检索怎么做的？索引又在哪里？", limit=4)

    assert terms == ["检索怎么做的", "索引又在哪里"]


def test_extract_terms_drops_stopwords_short_tokens_and_duplicates() -> None:
    terms = search.extract_terms("the code use use ab xx_yy xx_yy", limit=5)

    assert terms == ["ab", "xx_yy"]


# --------------------------------------------------------------------------- 候选发现（无 rg）


def test_filename_match_returns_a_bounded_snippet(
    workspace: Path, app_config: AppConfig
) -> None:
    """按文件名找到候选：片段是文件**开头**的有界窗口，行号是 1 基闭区间。"""
    (workspace / "routers.py").write_text(
        "\n".join(f"line {index}" for index in range(1, 41)) + "\n", encoding="utf-8"
    )

    result = _find("routers.py 里怎么判断", workspace=workspace, app_config=app_config, context_lines=2)

    assert [reference["path"] for reference in result.references] == ["routers.py"]
    reference = result.references[0]
    assert reference["match_type"] == "filename"
    assert reference["source"] == "python_scan"
    assert reference["rank"] == 1
    assert (reference["line_start"], reference["line_end"]) == (1, 5)
    assert reference["snippet"].startswith("1: line 1")
    assert "6: line 6" not in reference["snippet"]


def test_text_match_reports_one_based_line_range(workspace: Path, app_config: AppConfig) -> None:
    """正文命中给出命中行前后各 ``context_lines`` 行，且行号必须与文件一致。"""
    (workspace / "module.py").write_text(
        "a = 1\nb = 2\ntarget_symbol = 3\nc = 4\nd = 5\n", encoding="utf-8"
    )

    result = _find("target_symbol", workspace=workspace, app_config=app_config, context_lines=1)

    reference = result.references[0]
    assert reference["match_type"] == "text"
    assert reference["path"] == "module.py"
    assert (reference["line_start"], reference["line_end"]) == (2, 4)
    assert "3: target_symbol = 3" in reference["snippet"]


def test_path_match_finds_candidates_by_directory_name(
    workspace: Path, app_config: AppConfig
) -> None:
    """按路径命中：目录名出现在请求里也能定位到文件（架构 §4.3 的 ``path`` 命中）。"""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")

    result = _find("pkg", workspace=workspace, app_config=app_config)

    assert [reference["match_type"] for reference in result.references] == ["path"]
    assert result.references[0]["path"] == "pkg/mod.py"


def test_identifiers_are_not_crowded_out_by_chinese_prose(
    workspace: Path, app_config: AppConfig
) -> None:
    """实测回归（真实模型手测发现）：句中的"里的"曾命中 README 散文，把真命中挤出结果位。

    修法是"有 ASCII 词项时不再加入 CJK 片段"，所以这条用例固定住：中文散文不得进入结果，
    而 ``decide_mode`` 的正文命中必须在结果里。
    """
    (workspace / "README.md").write_text(
        "\n".join(f"第 {index} 行里的说明文字" for index in range(1, 40)) + "\n",
        encoding="utf-8",
    )
    # 函数放在文件中部（第 60 行）：文件名命中的是文件开头窗口，正文命中的是函数附近窗口，
    # 两者不会被"路径 + 行区间"去重掉——这才看得出正文命中是否真的进了结果。
    (workspace / "routers.py").write_text(
        "\n".join(["a = 1"] * 59 + ["def decide_mode():", "    return 'tool'"] + ["b = 2"] * 20),
        encoding="utf-8",
    )

    result = _find(
        "routers.py 里的 decide_mode 怎么判断模式？", workspace=workspace, app_config=app_config
    )

    assert {reference["path"] for reference in result.references} == {"routers.py"}
    text_refs = [ref for ref in result.references if ref["match_type"] == "text"]
    assert text_refs, "decide_mode 的正文命中必须出现在结果里"
    assert text_refs[0]["line_start"] <= 60 <= text_refs[0]["line_end"]


def test_results_reflect_the_disk_immediately(workspace: Path, app_config: AppConfig) -> None:
    """没有索引，也就没有陈旧结果：改动与删除在下一次查询里立刻反映出来。"""
    target = workspace / "note.py"
    target.write_text("needle_one = 1\n", encoding="utf-8")
    assert [ref["path"] for ref in _find("needle_one", workspace=workspace, app_config=app_config).references] == [
        "note.py"
    ]

    target.write_text("nothing_here = 2\n", encoding="utf-8")
    after_edit = _find("needle_one", workspace=workspace, app_config=app_config)
    assert after_edit.references == ()

    target.unlink()
    after_delete = _find("needle_one", workspace=workspace, app_config=app_config)
    assert after_delete.references == ()
    assert any("没有可用片段" in note for note in after_delete.notes)


def test_results_are_ranked_deduped_and_capped(workspace: Path, app_config: AppConfig) -> None:
    """条数上限、连续编号与 ``path + 行区间`` 去重（架构 §4.3）。"""
    for index in range(5):
        (workspace / f"mod{index}.py").write_text("shared_symbol = 1\n", encoding="utf-8")

    result = _find("shared_symbol", workspace=workspace, app_config=app_config, max_results=2)

    assert [reference["rank"] for reference in result.references] == [1, 2]
    keys = {
        (reference["path"], reference["line_start"], reference["line_end"])
        for reference in result.references
    }
    assert len(keys) == 2
    assert any("只保留前 2 条" in note for note in result.notes)


def test_hidden_sensitive_binary_and_oversized_files_are_skipped(
    workspace: Path, app_config: AppConfig
) -> None:
    """隐藏项、敏感文件、二进制与超大文件都不进入结果（架构 §6.2 的同一套边界）。"""
    (workspace / ".env").write_text("needle_x = 1\n", encoding="utf-8")
    (workspace / "server.pem").write_text("needle_x = 1\n", encoding="utf-8")  # 非隐藏但敏感
    (workspace / "blob.bin").write_bytes(b"\x00needle_x")
    (workspace / "big.py").write_text("needle_x\n" * 50, encoding="utf-8")
    (workspace / "ok.py").write_text("needle_x = 1\n", encoding="utf-8")

    result = _find("needle_x", workspace=workspace, app_config=app_config, max_file_bytes=100)

    assert [reference["path"] for reference in result.references] == ["ok.py"]
    assert any("超过 100 字节" in note for note in result.notes)
    assert any("二进制" in note for note in result.notes)


def test_no_terms_means_no_filesystem_access(
    workspace: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有可用词项时连遍历都不做：不碰文件系统，也不假装"搜过了"。"""
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("没有词项时不应遍历文件系统")

    # 遍历的入口是 `rag.walk.iter_candidate_files`（v0.6 T63 第三片：`grep` / `glob` / 本模块
    # 共用同一份实现），钉住它就等于钉住"没有词项时不去枚举文件系统"。
    monkeypatch.setattr(search, "iter_candidate_files", boom)

    result = _find("！？", workspace=workspace, app_config=app_config)

    assert result.references == ()
    assert result.failed is False
    assert "没有可检索的词项" in result.notes[0]


def test_walk_failure_is_reported_as_failed(
    workspace: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """遍历失败要说成"没检索成"，不是"没有相关代码"。"""
    def boom(*args: object, **kwargs: object) -> object:
        raise OSError("模拟遍历失败")

    monkeypatch.setattr(search, "iter_candidate_files", boom)

    result = _find("symbol_one", workspace=workspace, app_config=app_config)

    assert result.failed is True
    assert result.references == ()
    assert any("遍历失败" in note for note in result.notes)


# --------------------------------------------------------------------------- rg 加速路径


def _fake_rg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    returncode: int = 0,
    raises: Exception | None = None,
) -> list[list[str]]:
    """把 ``subprocess.run`` 换成替身，记录被调用的 argv，并返回构造好的 rg 输出。"""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout.encode("utf-8"), b"")

    monkeypatch.setattr(search.subprocess, "run", fake_run)
    return calls


def _rg_payload(relative_path: str, line_number: int, text: str) -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": relative_path},
                "line_number": line_number,
                "lines": {"text": f"{text}\n"},
            },
        }
    )


def test_rg_results_are_parsed_attributed_and_bounded(
    workspace: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rg --json`` 的输出被解析成候选，并归属到具体词项；argv 用数组直执行、不拼 Shell。"""
    (workspace / "mod.py").write_text("def decide_mode():\n    return 1\n", encoding="utf-8")
    calls = _fake_rg(monkeypatch, stdout=_rg_payload("mod.py", 1, "def decide_mode():") + "\n")

    result = _find(
        "decide_mode", workspace=workspace, app_config=app_config, resolve=lambda *a, **k: "rg"
    )

    reference = result.references[0]
    assert reference["source"] == "rg"
    assert reference["match_type"] == "text"
    assert (reference["line_start"], reference["line_end"]) == (1, 2)
    argv = calls[0]
    assert argv[0] == "rg"
    assert "--fixed-strings" in argv and "--json" in argv
    assert "-e" in argv and argv[argv.index("-e") + 1] == "decide_mode"
    assert argv[-1] == "."  # 搜索路径显式给出，不依赖 rg 的"哪个参数是路径"启发式


def test_rg_results_outside_the_workspace_are_dropped(
    workspace: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """不信任外部工具给出的路径：工作区外的结果一律丢弃（架构 §5.3）。"""
    outside = tmp_path / "outside.py"
    outside.write_text("decide_mode = 1\n", encoding="utf-8")
    _fake_rg(monkeypatch, stdout=_rg_payload("../outside.py", 1, "decide_mode = 1") + "\n")

    result = _find(
        "decide_mode", workspace=workspace, app_config=app_config, resolve=lambda *a, **k: "rg"
    )

    assert result.references == ()


def test_rg_error_falls_back_to_the_bounded_scan(
    workspace: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rg`` 出错（退出码 2）时如实记一条说明，并退化到 Python 扫描拿到结果。"""
    (workspace / "mod.py").write_text("decide_mode = 1\n", encoding="utf-8")
    _fake_rg(monkeypatch, stdout="", returncode=2)

    result = _find(
        "decide_mode", workspace=workspace, app_config=app_config, resolve=lambda *a, **k: "rg"
    )

    assert [reference["source"] for reference in result.references] == ["python_scan"]
    assert any("rg 检索未完成" in note for note in result.notes)


def test_rg_timeout_falls_back_to_the_bounded_scan(
    workspace: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时同样退化，且说明里写明"超过 N 秒"。"""
    (workspace / "mod.py").write_text("decide_mode = 1\n", encoding="utf-8")
    _fake_rg(monkeypatch, raises=subprocess.TimeoutExpired(cmd="rg", timeout=1))

    result = _find(
        "decide_mode",
        workspace=workspace,
        app_config=app_config,
        resolve=lambda *a, **k: "rg",
        timeout_seconds=3,
    )

    assert result.references and result.references[0]["source"] == "python_scan"
    assert any("超过 3 秒" in note for note in result.notes)


def test_missing_rg_is_reported(workspace: Path, app_config: AppConfig) -> None:
    """本机没有 ``rg``：说明里写清"可选加速器缺失"，但结果照常给出。"""
    (workspace / "mod.py").write_text("decide_mode = 1\n", encoding="utf-8")

    result = _find("decide_mode", workspace=workspace, app_config=app_config)

    assert result.references
    assert any("未找到可执行文件 rg" in note for note in result.notes)


# --------------------------------------------------------------------------- 片段读取


def test_snippet_refuses_paths_outside_the_workspace(
    workspace: Path, app_config: AppConfig, tmp_path: Path
) -> None:
    """片段读取也走安全策略：工作区外的文件取不到片段（与文件工具同一套边界）。"""
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")

    snippet = snippets.read_snippet(
        outside,
        relative_path="outside.py",
        line_number=1,
        config=_rag_config(app_config).rag,
        policy=_policy(app_config, workspace),
    )

    assert snippet is None


def test_snippet_reports_a_stale_hit_as_none(workspace: Path, app_config: AppConfig) -> None:
    """命中行已经不存在（文件被改短）时丢弃候选，不返回错位的片段。"""
    target = workspace / "mod.py"
    target.write_text("a = 1\n", encoding="utf-8")

    snippet = snippets.read_snippet(
        target,
        relative_path="mod.py",
        line_number=99,
        config=_rag_config(app_config).rag,
        policy=_policy(app_config, workspace),
    )

    assert snippet is None


# --------------------------------------------------------------------------- Prompt 段落


def test_prompt_renders_snippets_as_untrusted_data() -> None:
    """片段段落带路径与行号区间；空结果与"检索失败"用不同占位文字。"""
    reference = RAGReference(
        path="pkg/mod.py",
        line_start=3,
        line_end=4,
        snippet="3: x = 1\n4: y = 2",
        match_type="text",
        source="rg",
        rank=1,
    )

    rendered = prompt_context.render_rag_section([reference], max_chars=1000)

    assert "1. pkg/mod.py:3-4 [正文命中]" in rendered
    assert "3: x = 1" in rendered
    assert prompt_context.render_rag_section([]) == "（本轮没有检索到匹配的代码片段）"
    assert "未启用" in prompt_context.render_rag_section([], enabled=False)
    assert "不可用" in prompt_context.render_rag_section([], search_failed=True)


def test_prompt_section_respects_the_character_budget() -> None:
    """段落超预算时丢掉后面的条目，而不是整段塞进 Prompt。"""
    references = [
        RAGReference(
            path=f"mod{index}.py",
            line_start=1,
            line_end=1,
            snippet="1: " + "x" * 40,
            match_type="text",
            source="python_scan",
            rank=index + 1,
        )
        for index in range(3)
    ]

    rendered = prompt_context.render_rag_section(references, max_chars=120)

    assert "1. mod0.py:1-1" in rendered
    assert "2. mod1.py:1-1" not in rendered


# --------------------------------------------------------------------------- 配置迁移


def test_rag_config_rejects_legacy_index_keys(app_config: AppConfig) -> None:
    """旧的持久索引预留键已经收窄掉：还在写它们的配置必须启动即失败，而不是静默忽略。"""
    data = app_config.model_dump()
    data["rag"].update({"backend": "tree_sitter", "index_path": "data/code_index"})

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)
