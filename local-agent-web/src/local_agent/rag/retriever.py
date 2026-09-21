"""按需代码检索的编排（架构 §4.3、§5.3；开发文档 T21）。

一次检索的完整流程：从请求提取词项 → 发现候选（``rg`` 优先，缺失退化有界扫描）→
为每个候选读取有界片段 → 去重、限制条数、编号 → 产出 ``rag_refs``。

没有持久索引：每次调用都读当前磁盘现状。因此**文件的新增、修改、删除在下一次查询里
立刻可见**，也不存在"陈旧索引"或缓存目录需要失效。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from local_agent.config import RagConfig
from local_agent.graph.state import RAGReference
from local_agent.rag.search import discover_matches, extract_terms
from local_agent.rag.snippets import read_snippet
from local_agent.security.policy import SecurityPolicy
from local_agent.security.shell_guard import resolve_executable


@dataclass(frozen=True)
class RetrievedCode:
    """一次检索的结果与说明。"""

    references: tuple[RAGReference, ...] = ()
    notes: tuple[str, ...] = ()
    #: 检索**没能完成**（遍历失败等）：调用方要写可见错误，且不得伪称已检索。
    failed: bool = False


def retrieve_code(
    query: str,
    *,
    config: RagConfig,
    workspace_root: Path,
    policy: SecurityPolicy,
    resolve: Callable[..., str | None] = resolve_executable,
) -> RetrievedCode:
    """按用户请求在工作区内做一次按需代码检索。

    调用方负责判断 ``rag.enabled``：**关闭时不要调用本函数**（关闭不访问文件系统是节点的
    职责，不是这里的兜底）。

    Args:
        query: 查询素材（当前轮的 ``normalized_request`` / ``user_input``）。
        config: ``rag.*`` 限额。
        workspace_root: 工作区根目录。
        policy: 安全策略（工作区边界与敏感文件判定）。
        resolve: 可执行文件解析函数；测试用它注入"有 ``rg`` / 没有 ``rg``"。

    Returns:
        去重、编号并限制条数后的 ``RAGReference`` 列表，以及降级说明。
    """
    terms = extract_terms(query, limit=config.max_terms)
    if not terms:
        return RetrievedCode(
            notes=("请求里没有可检索的词项（路径、文件名或标识符），本轮未搜索工作区",)
        )

    outcome = discover_matches(
        terms, workspace_root=workspace_root, config=config, resolve=resolve
    )
    notes: list[str] = list(outcome.notes)
    references: list[RAGReference] = []
    seen: set[tuple[str, int, int]] = set()
    truncated_snippets = 0
    unreadable = 0
    for match in outcome.matches:
        if len(references) >= config.max_results:
            notes.append(f"候选超过上限 {config.max_results} 条，只保留前 {config.max_results} 条")
            break
        snippet = read_snippet(
            match.path,
            relative_path=match.relative_path,
            line_number=match.line_number,
            config=config,
            policy=policy,
        )
        if snippet is None:
            # 命中之后文件被改动、越界、敏感、二进制或超大：丢弃这条候选，不猜内容。
            unreadable += 1
            continue
        key = (snippet.relative_path, snippet.line_start, snippet.line_end)
        if key in seen:
            continue
        seen.add(key)
        if snippet.note:
            truncated_snippets += 1
        references.append(
            RAGReference(
                path=snippet.relative_path,
                line_start=snippet.line_start,
                line_end=snippet.line_end,
                snippet=snippet.text,
                match_type=match.match_type,
                source=match.source,
                rank=len(references) + 1,
            )
        )

    if truncated_snippets:
        notes.append(f"{truncated_snippets} 条片段已按字符上限截断")
    if unreadable:
        notes.append(
            f"{unreadable} 条候选无法读取片段（越界、敏感、二进制、超大或已被改动），已跳过"
        )
    if not references and not outcome.failed:
        notes.append("本次检索没有可用片段")
    return RetrievedCode(
        references=tuple(references), notes=tuple(notes), failed=outcome.failed
    )
