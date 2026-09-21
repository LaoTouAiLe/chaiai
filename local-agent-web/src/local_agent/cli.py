"""记忆管理命令行：查看、人工删除与遗忘任务（T18 的最小人工入口）。

为什么需要它：``delete_fact`` / ``delete_entity`` / ``medium_term.delete`` / ``run_forgetting``
原先都只是库 API，用户没有任何可触达的入口——"人工删除"在实际使用中无法执行（复审存疑项）。
这里提供**最小操作面**，底层一律复用库 API：作用域推导用 :mod:`local_agent.scope`，老化口径用
:mod:`local_agent.memory.forgetting`，命令行里不重写任何判定逻辑。

用法::

    python -m local_agent.cli memory list [--kind fact|conclusion|medium|all] [--limit N]
    python -m local_agent.cli memory delete <记忆 id> [--kind fact|entity|medium] --yes
    python -m local_agent.cli memory forget [--dry-run]

通用参数：``--config``（默认 ``config.yaml``）、``--workspace``（默认当前目录）。

口径提醒：

- 长期事实与实体是**软删除**（置为 ``deleted``，历史行保留用于审计），人工删除不会恢复旧的
  ``superseded`` 版本；
- 中期记忆的删除**不可逆**（向量库没有版本历史）；
- ``list`` 列出的长期事实与召回口径一致（本项目 / 本用户 / 全局，且已排除过期与超龄项）。
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent import config as config_module
from local_agent.config import AppConfig, ConfigError
from local_agent.exceptions import EmbeddingError, MemoryStoreError
from local_agent.llm import embedding as embedding_module
from local_agent.memory import forgetting, long_term, medium_term
from local_agent.scope import MemoryScope, project_id_for, visible_scope_keys

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

#: 单个字段在输出里最多显示多少字符。
_SHORT_CHARS = 72


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；返回进程退出码（0 成功 / 1 执行失败 / 2 用法或确认缺失）。"""
    args = _parse_args(sys.argv[1:] if argv is None else list(argv))
    try:
        if args.group == "memory":
            return _MEMORY_ACTIONS[args.action](args)
    except (ConfigError, MemoryStoreError, EmbeddingError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_USAGE  # pragma: no cover —— argparse 已保证 group/action 合法


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m local_agent.cli",
        description="本机 Agent 的记忆管理（查看 / 人工删除 / 遗忘任务）",
    )
    groups = parser.add_subparsers(dest="group", required=True)
    memory = groups.add_parser("memory", help="记忆管理")
    actions = memory.add_subparsers(dest="action", required=True)

    list_parser = actions.add_parser(
        "list", help="列出当前作用域内可管理的长期事实、项目结论与中期记忆"
    )
    _add_config(list_parser)
    _add_workspace(list_parser)
    list_parser.add_argument(
        "--kind",
        choices=("all", "fact", "conclusion", "medium"),
        default="all",
        help="只看某一类（默认 all）",
    )
    list_parser.add_argument("--limit", type=int, default=20, help="每类最多显示多少条")

    delete_parser = actions.add_parser("delete", help="人工删除一条记忆（需要 --yes 确认）")
    _add_config(delete_parser)
    _add_workspace(delete_parser)
    delete_parser.add_argument("memory_id", help="list 输出里的记忆 id")
    delete_parser.add_argument(
        "--kind",
        choices=("fact", "entity", "medium"),
        default="fact",
        help="删除对象类型（默认 fact；entity 会一并删除其下所有事实）",
    )
    delete_parser.add_argument(
        "--yes", action="store_true", help="确认执行；缺少时只打印提示、不写库"
    )

    # forget 是**整库**维护任务（存储层的过期/老化都不按作用域过滤），因此不提供 --workspace：
    # 给了也只是摆设，反而会让人以为"只清理当前工作区"（复审实证：预览与执行范围不一致）。
    forget_parser = actions.add_parser(
        "forget", help="执行整库遗忘任务：显式过期 + 自动老化 + 中期清理"
    )
    _add_config(forget_parser)
    forget_parser.add_argument("--dry-run", action="store_true", help="只统计将要处理的条数")

    probe_parser = actions.add_parser(
        "probe", help="显式探测 Embedding：真正加载模型并编码一段文本"
    )
    _add_config(probe_parser)
    probe_parser.add_argument("--text", default=None, help="探针文本（默认一段中文）")
    probe_parser.add_argument(
        "--repeat", type=int, default=2, help="编码次数（第 2 次用于观察同进程复用耗时）"
    )

    return parser.parse_args(argv)


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="配置文件路径（默认 ./config.yaml）")


def _add_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=None, help="工作区目录（默认当前目录）")


def _visible_keys(workspace: Path) -> list[tuple[MemoryScope, str]]:
    """当前环境可见的全部作用域（与召回层同一真源，见 :func:`local_agent.scope.visible_scope_keys`）。"""
    return visible_scope_keys(
        user_id=None, project_id=project_id_for(workspace), workspace_root=workspace
    )


def _cmd_list(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    workspace = Path(args.workspace or ".").resolve()
    now = datetime.now(timezone.utc)
    print(f"工作区：{workspace.as_posix()}")
    project = project_id_for(workspace)
    print(f"项目作用域：{project or '（无法推导）'}")
    if args.kind in ("all", "fact"):
        _print_facts(config, workspace=workspace, now=now, limit=args.limit)
    if args.kind in ("all", "conclusion"):
        _print_conclusions(config, workspace=workspace, limit=args.limit)
    if args.kind in ("all", "medium"):
        _print_medium(config, workspace=workspace, limit=args.limit)
    return EXIT_OK


def _cmd_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "删除会改变记忆库内容：确认无误后加 --yes 重跑，例如\n"
            f"  python -m local_agent.cli memory delete {args.memory_id} --kind {args.kind} --yes"
        )
        return EXIT_USAGE
    config = _load_config(args.config)
    workspace = Path(args.workspace or ".").resolve()
    visible = _visible_keys(workspace)
    if args.kind == "medium":
        store = _require_medium(config)
        hit = store.find_record(args.memory_id)
        if hit is None:
            print(f"没有找到这条中期记忆：{args.memory_id}（可能已被删除或 id 有误）")
            return EXIT_FAILURE
        if not _is_visible(hit.metadata.get("scope"), hit.metadata.get("scope_key"), visible=visible):
            print(_out_of_scope_text(args.memory_id, hit.metadata, workspace=workspace))
            return EXIT_FAILURE
        removed = store.delete([args.memory_id])
        if not removed:
            print(f"没有找到这条中期记忆：{args.memory_id}（可能已被删除或 id 有误）")
            return EXIT_FAILURE
        print(f"已从中期记忆删除 {removed} 条：{args.memory_id}（不可逆）")
        return EXIT_OK
    long_store = _require_long(config)
    if args.kind == "entity":
        entity = long_store.get_entity(args.memory_id)
        if entity is None:
            print(f"没有找到实体：{args.memory_id}")
            return EXIT_FAILURE
        if not _is_visible(entity.scope, entity.scope_key, visible=visible):
            print(_out_of_scope_text(args.memory_id, {"scope": entity.scope, "scope_key": entity.scope_key}, workspace=workspace))
            return EXIT_FAILURE
        removed = long_store.delete_entity(args.memory_id)
        print(f"已删除实体（作用域 {entity.scope}）及其事实共 {removed} 处改动：{args.memory_id}")
        return EXIT_OK if removed else EXIT_FAILURE
    record = long_store.get_fact(args.memory_id)
    if record is None:
        print(f"没有找到这条事实：{args.memory_id}（可能 id 有误）")
        return EXIT_FAILURE
    owner = long_store.get_entity(record.entity_id)
    if owner is None:
        # 拿不到作用域就不删：分不清它属于谁，宁可不删（与召回/写入的 fail-closed 方向一致）。
        print(f"拒绝删除：{args.memory_id} 的所属实体已不存在，无法确认作用域")
        return EXIT_FAILURE
    if not _is_visible(owner.scope, owner.scope_key, visible=visible):
        print(_out_of_scope_text(args.memory_id, {"scope": owner.scope, "scope_key": owner.scope_key}, workspace=workspace))
        return EXIT_FAILURE
    if not long_store.delete_fact(args.memory_id):
        print(f"没有找到可删除的 active 事实：{args.memory_id}（当前状态 {record.status}）")
        return EXIT_FAILURE
    print(f"已软删除事实：{args.memory_id}（作用域 {owner.scope}；历史行保留，可用于审计）")
    return EXIT_OK


def _is_visible(
    scope: Any, scope_key: Any, *, visible: Sequence[tuple[MemoryScope, str]]
) -> bool:
    """记录的作用域是否落在当前工作区的**可见范围**内（与召回用同一套作用域组合）。"""
    return (str(scope or ""), str(scope_key or "")) in set(visible)


def _out_of_scope_text(
    memory_id: str, metadata: Mapping[str, Any], *, workspace: Path
) -> str:
    """作用域不匹配的提示：**没有写库**，并说清怎么才能删到它。"""
    scope = str(metadata.get("scope") or "?")
    scope_key = str(metadata.get("scope_key") or "?")
    return (
        f"拒绝删除：{memory_id} 属于作用域 {scope}（scope_key {scope_key}），"
        f"不在当前工作区 {workspace.as_posix()} 的可见范围内，未改动任何记录。\n"
        "删除默认只允许清理当前工作区可见作用域内的记忆；"
        "要清理别的项目，请用 --workspace 指定它的工作目录"
        "（跨项目整库清理需要单独的管理开关，目前没有提供）。"
    )


def _cmd_forget(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    now = datetime.now(timezone.utc)
    long_store = _open_long(config)
    medium_store = _open_medium(config)
    if long_store is None and medium_store is None:
        print("中期与长期记忆都未启用，无需遗忘处理")
        return EXIT_OK
    if args.dry_run:
        report = _preview_forgetting(
            config, long_store=long_store, medium_store=medium_store, now=now
        )
        print(
            "[dry-run] 整库预计："
            f"显式过期 {report.expired_facts} 条、自动老化 {report.aged_facts} 条、"
            f"中期过期清理 {report.deleted_medium} 条（共 {report.total} 条）"
        )
        print("（dry-run 不写库；范围是整库，与实际执行范围一致）")
        return EXIT_OK
    report = forgetting.run_forgetting(
        config=config, long_store=long_store, medium_store=medium_store, now=now
    )
    print(
        "遗忘完成（整库）："
        f"显式过期 {report.expired_facts} 条、自动老化 {report.aged_facts} 条、"
        f"中期删除 {report.deleted_medium} 条（共 {report.total} 条）"
    )
    return EXIT_OK


def _cmd_probe(args: argparse.Namespace) -> int:
    """显式探测 Embedding：**真正加载模型并编码**一段文本，打印耗时与向量自检结果。

    为什么需要独立的探针命令：中期库的 embedding 适配器是**惰性**的，`memory list --kind medium`
    只读向量库、不做编码，所以"空缓存 + 离线"时它照样成功——用它判断"权重可用"会误判（复审实证：
    实际编码才抛 ``EmbeddingLoadError``）。这里的探针会触发加载，把 T14A 手工验收要看的东西
    （首次/复用耗时、维度、是否有限值）一次打出来。
    """
    config = _load_config(args.config)
    embedding_config = config.embedding
    if not embedding_config.enabled:
        print("Embedding 未启用（embedding.enabled=false），没有可探测的模型")
        return EXIT_USAGE
    api_key = (
        config_module.resolve_api_key(embedding_config)
        if embedding_config.provider == "openai_compatible"
        else None
    )
    adapter = embedding_module.build_embedding_function(embedding_config, api_key=api_key)
    text = args.text or "探针文本：验证 Embedding 能否加载并编码。"
    print(
        f"Embedding 探针：provider={embedding_config.provider} model={embedding_config.model}\n"
        f"缓存目录：{embedding_config.cache_dir}\n"
        f"离线模式（local_files_only）：{embedding_config.local_files_only}"
    )
    vector: list[float] = []
    for index in range(max(1, args.repeat)):
        started = time.perf_counter()
        vectors = adapter.embed_query(text)  # 真正触发加载与编码
        elapsed = time.perf_counter() - started
        vector = [float(item) for item in (vectors[0] if vectors else [])]
        label = "首次（含加载）" if index == 0 else f"第 {index + 1} 次（同进程复用）"
        print(f"  {label}：{elapsed:.3f}s，维度 {len(vector)}")
    finite = bool(vector) and all(math.isfinite(item) for item in vector)
    print(f"  向量自检：维度 {len(vector)}，全部为有限值（无 NaN/Inf）：{finite}")
    if not finite:
        print("模型能加载，但编码结果异常（空向量或含 NaN/Inf）；请检查模型与缓存是否完整")
        return EXIT_FAILURE
    print("结论：Embedding 可用（本次编码成功）")
    return EXIT_OK


# --------------------------------------------------------------------------- 读取与打印


def _load_config(path: str | None) -> AppConfig:
    """加载配置；文件缺失时给出能照着做的提示（而不是一段 traceback）。"""
    target = Path(path) if path else Path("config.yaml")
    if not target.is_file():
        raise ConfigError(
            f"找不到配置文件：{target.as_posix()}；"
            "用 --config 指定路径，或先复制 config.yaml.example 为 config.yaml"
        )
    return config_module.load_config(target)


def _open_long(config: AppConfig) -> long_term.LongTermStore | None:
    """打开长期库；未启用时返回 ``None``（调用方按"这一层跳过"处理）。"""
    if not config.memory.long_term.enabled:
        return None
    return long_term.open_store(Path(config.memory.long_term.path))


def _open_medium(config: AppConfig) -> medium_term.MediumTermStore | None:
    """打开中期库；未启用或 embedding 关闭时返回 ``None``。

    这一层会加载 embedding 模型（首次可能下载），所以只有真正需要时才调用它。
    """
    memory = config.memory.medium_term
    embedding_config = config.embedding
    if not (memory.enabled and embedding_config.enabled):
        return None
    api_key = (
        config_module.resolve_api_key(embedding_config)
        if embedding_config.provider == "openai_compatible"
        else None
    )
    return medium_term.open_store(
        memory,
        embedding_function=embedding_module.build_embedding_function(
            embedding_config, api_key=api_key
        ),
    )


def _require_long(config: AppConfig) -> long_term.LongTermStore:
    store = _open_long(config)
    if store is None:
        raise MemoryStoreError("长期记忆未启用（memory.long_term.enabled=false）")
    return store


def _require_medium(config: AppConfig) -> medium_term.MediumTermStore:
    store = _open_medium(config)
    if store is None:
        raise MemoryStoreError("中期记忆未启用（memory.medium_term.enabled 或 embedding.enabled 为 false）")
    return store


def _print_facts(config: AppConfig, *, workspace: Path, now: datetime, limit: int) -> None:
    store = _open_long(config)
    if store is None:
        print("\n[长期事实] 未启用（memory.long_term.enabled=false）")
        return
    rows = [
        (entity, fact)
        for entity, fact in store.active_facts_with_entities(_visible_keys(workspace), now=now)
        # 与召回同一口径的**可见性过滤**：已过期、已超龄的事实不再列出——存储层的 active 行
        # 包含"状态还是 active 但时间上已不可召回"的记录，直接列会与召回结果不一致（复审实证）。
        if forgetting.is_recallable_fact(fact, config=config.memory.long_term, now=now)
    ]
    print(
        f"\n[长期事实] {len(rows)} 条（当前作用域内可管理的记忆：本项目 / 本用户 / 全局 / 本工作区；"
        "已排除过期与超龄）"
    )
    if rows:
        print("  （是否召回还取决于查询相似度与阈值；这里只保证可见性口径一致）")
    for entity, fact in rows[: max(0, limit)]:
        print(
            f"  {fact.id}  {entity.name} 的 {fact.key} = {_short(fact.value_json)}"
            f"  [{entity.scope}] importance={fact.importance:g} confidence={fact.confidence:g}"
            f" 更新于 {fact.updated_at}"
        )
    if len(rows) > limit:
        print(f"  …还有 {len(rows) - limit} 条（用 --limit 调整）")


def _print_conclusions(config: AppConfig, *, workspace: Path, limit: int) -> None:
    store = _open_long(config)
    project = project_id_for(workspace)
    if store is None:
        print("\n[长期任务结论] 未启用（memory.long_term.enabled=false）")
        return
    if not project:
        print("\n[长期任务结论] 无法推导项目作用域，跳过")
        return
    records = store.list_task_conclusions(project_scope=project, limit=None)
    print(f"\n[长期任务结论] {len(records)} 条（仅本项目）")
    for record in records[: max(0, limit)]:
        print(
            f"  {record.id}  {record.task_title}：{_short(record.conclusion)}"
            f"  confidence={record.confidence:g} 创建于 {record.created_at}"
        )
    if len(records) > limit:
        print(f"  …还有 {len(records) - limit} 条（用 --limit 调整）")


def _print_medium(config: AppConfig, *, workspace: Path, limit: int) -> None:
    store = _open_medium(config)
    if store is None:
        print("\n[中期记忆] 未启用（或 embedding 关闭）")
        return
    # 作用域与有效期过滤都在存储层完成（与召回同一套条件），因此这里不会列出别的项目的记录。
    hits = store.list_records(scope_keys=_visible_keys(workspace), limit=max(1, limit))
    print(f"\n[中期记忆] 当前作用域内 {len(hits)} 条（向量库无顺序语义，按库内顺序列出）")
    for hit in hits:
        scope = str(hit.metadata.get("scope") or "?")
        print(f"  {hit.memory_id}  {_short(hit.content)}  [{scope}]")


def _preview_forgetting(
    config: AppConfig,
    *,
    long_store: long_term.LongTermStore | None,
    medium_store: medium_term.MediumTermStore | None,
    now: datetime,
) -> forgetting.ForgetReport:
    """``--dry-run`` 的预演：与 :func:`forgetting.run_forgetting` **同范围（整库）**、同判定。

    范围不一致会让预览失去意义（复审实证：预览 0 条、实际过期 1 条其它项目的事实）。这里的判定顺序
    与实际执行严格对齐——**显式有效期优先**（``expire_overdue`` 只看带 ``expires_at`` 的行），
    纯年龄淘汰只处理**没有**有效期的行（``expire_aged`` 的 `expires_at IS NULL`），不重不漏。
    """
    long_config = config.memory.long_term
    expired_facts = 0
    aged_facts = 0
    if long_store is not None:
        for record in long_store.list_active_facts():
            if forgetting.is_expired(record.expires_at, now=now):
                expired_facts += 1
            elif forgetting.is_aged_out(record, config=long_config, now=now):
                aged_facts += 1
    deleted_medium = len(medium_store.expired_ids(now=now)) if medium_store is not None else 0
    return forgetting.ForgetReport(
        expired_facts=expired_facts, aged_facts=aged_facts, deleted_medium=deleted_medium
    )


def _short(text: str, *, limit: int = _SHORT_CHARS) -> str:
    """把长文本压成单行预览（列表要能一行读完）。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


#: 子命令 → 处理函数。
_MEMORY_ACTIONS = {
    "list": _cmd_list,
    "delete": _cmd_delete,
    "forget": _cmd_forget,
    "probe": _cmd_probe,
}


if __name__ == "__main__":  # pragma: no cover —— 进程入口
    raise SystemExit(main())
