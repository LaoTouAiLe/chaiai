"""T18 的人工入口：记忆管理命令行与页面展示。

命令行只做三件事——列出、人工删除、执行遗忘任务——底层全部复用库 API，所以这里验证的是
"用户是否真的能做到"（可达性与确认语义），记忆本身的口径仍在 ``test_memory.py`` 验证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from local_agent import cli, web
from local_agent.config import AppConfig, load_config
from local_agent.exceptions import EmbeddingLoadError
from local_agent.memory import long_term, medium_term
from local_agent.scope import SCOPE_PROJECT, SCOPE_USER, project_id_for, visible_scope_keys

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config(long_path: Path, *, enabled: bool = True, forget_after_days: int = 30) -> AppConfig:
    """示例配置的副本：长期库指向临时目录，其余（含中期关闭）保持示例值。"""
    base = load_config(PROJECT_ROOT / "config.yaml.example")
    return base.model_copy(
        update={
            "memory": base.memory.model_copy(
                update={
                    "long_term": base.memory.long_term.model_copy(
                        update={
                            "enabled": enabled,
                            "path": str(long_path),
                            "forget_after_days": forget_after_days,
                        }
                    )
                }
            )
        }
    )


def _seed(
    long_path: Path,
    *,
    project: str,
    key: str = "test_runner",
    expires_at: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """写入一个实体与一条事实，返回 ``(entity_id, fact_id)``。"""
    store = long_term.open_store(long_path)
    try:
        entity = store.record_entity(
            entity_type="project",
            name="chaiai",
            scope=SCOPE_PROJECT,
            project_id=project,
            now=now,
        )
        result = store.upsert_fact(
            entity_id=entity.id,
            key=key,
            value="pytest",
            confidence=0.9,
            importance=0.8,
            source="test",
            expires_at=expires_at,
            now=now,
            origin="owner",
        )
    finally:
        store.close()
    return entity.id, result.record.id


class _FakeCollection:
    """只实现 ``list_records`` / ``delete`` 用到的两个方法，用来验证过滤与计数口径。"""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.get_calls: list[dict[str, Any]] = []
        self.deleted: list[list[str]] = []

    def get(
        self,
        *,
        ids: list[str] | None = None,
        where: Any = None,
        limit: int | None = None,
        include: Any = None,
    ) -> dict[str, Any]:
        self.get_calls.append({"ids": ids, "where": where, "limit": limit, "include": include})
        rows = self.records
        if ids is not None:
            wanted = set(ids)
            rows = [row for row in rows if row["id"] in wanted]
        return {
            "ids": [row["id"] for row in rows],
            "documents": [row["content"] for row in rows],
            "metadatas": [row["metadata"] for row in rows],
        }

    def delete(self, *, ids: list[str]) -> None:
        self.deleted.append(list(ids))


def _medium_store(collection: _FakeCollection) -> medium_term.MediumTermStore:
    return medium_term.MediumTermStore(
        client=None,
        collection=collection,
        path=Path("unused"),
        collection_name="test",
        embedding=None,  # type: ignore[arg-type]  # 列出与删除都不碰 embedding
    )


def _enable_embedding(config: AppConfig) -> AppConfig:
    """打开 embedding（示例配置里默认关闭，这是 T14A 的验收要求）。"""
    return config.model_copy(
        update={"embedding": config.embedding.model_copy(update={"enabled": True})}
    )


def _use(monkeypatch: pytest.MonkeyPatch, config: AppConfig) -> None:
    """让命令行拿到测试配置（真实路径由 ``--config`` 解析，这里直接替换加载函数）。"""
    monkeypatch.setattr(cli, "_load_config", lambda path: config)


def test_list_shows_recallable_facts_with_delete_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``memory list`` 列出的正是会召回的范围，并给出可用于删除的 id。"""
    database = tmp_path / "data" / "memories.sqlite"
    _, fact_id = _seed(database, project=project_id_for(tmp_path))
    _use(monkeypatch, _config(database))

    code = cli.main(["memory", "list", "--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert fact_id in output
    assert "pytest" in output
    assert "[长期事实] 1 条" in output


def test_list_is_scoped_to_the_requested_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """换一个工作区就看不到别的项目的事实：人工入口上也保持作用域隔离。"""
    database = tmp_path / "data" / "memories.sqlite"
    _seed(database, project=project_id_for(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    _use(monkeypatch, _config(database))

    cli.main(["memory", "list", "--workspace", str(other)])

    assert "test_runner" not in capsys.readouterr().out


def test_delete_requires_explicit_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺少 ``--yes`` 时只打印提示，绝不改动记忆库。"""
    database = tmp_path / "data" / "memories.sqlite"
    _, fact_id = _seed(database, project=project_id_for(tmp_path))
    _use(monkeypatch, _config(database))

    code = cli.main(["memory", "delete", fact_id])
    output = capsys.readouterr().out

    assert code == cli.EXIT_USAGE
    assert "--yes" in output
    store = long_term.open_store(database)
    try:
        record = store.get_fact(fact_id)
        assert record is not None and record.status == "active"  # 未改动
    finally:
        store.close()


def test_delete_soft_deletes_and_reports_missing_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--yes`` 后软删除：召回口径立即看不到它，重复删除给出明确失败码。"""
    database = tmp_path / "data" / "memories.sqlite"
    _, fact_id = _seed(database, project=project_id_for(tmp_path))
    _use(monkeypatch, _config(database))

    assert (
        cli.main(["memory", "delete", fact_id, "--yes", "--workspace", str(tmp_path)])
        == cli.EXIT_OK
    )
    capsys.readouterr()
    store = long_term.open_store(database)
    try:
        record = store.get_fact(fact_id)
        assert record is not None and record.status == "deleted"  # 软删除，历史行保留
    finally:
        store.close()

    assert (
        cli.main(["memory", "delete", fact_id, "--yes", "--workspace", str(tmp_path)])
        == cli.EXIT_FAILURE
    )
    assert "没有找到" in capsys.readouterr().out


def test_forget_dry_run_counts_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` 只统计：报告要处理几条，但库里状态不变。"""
    database = tmp_path / "data" / "memories.sqlite"
    old = datetime.now(timezone.utc) - timedelta(days=5)
    _, fact_id = _seed(database, project=project_id_for(tmp_path), now=old)
    _use(monkeypatch, _config(database, forget_after_days=1))

    code = cli.main(["memory", "forget", "--dry-run"])
    output = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert "自动老化 1 条" in output
    store = long_term.open_store(database)
    try:
        record = store.get_fact(fact_id)
        assert record is not None and record.status == "active"  # dry-run 不写库
    finally:
        store.close()


def test_forget_ages_out_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """真正执行遗忘任务：超龄事实落库为 ``expired``，之后人工 list 也看不到。"""
    database = tmp_path / "data" / "memories.sqlite"
    old = datetime.now(timezone.utc) - timedelta(days=5)
    _, fact_id = _seed(database, project=project_id_for(tmp_path), now=old)
    _use(monkeypatch, _config(database, forget_after_days=1))

    assert cli.main(["memory", "forget"]) == cli.EXIT_OK
    assert "自动老化 1 条" in capsys.readouterr().out

    store = long_term.open_store(database)
    try:
        record = store.get_fact(fact_id)
        assert record is not None and record.status == "expired"
    finally:
        store.close()


def test_forget_reports_when_no_layer_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """两层都关闭时说清楚"无事可做"，不报错、也不假装做了清理。"""
    _use(monkeypatch, _config(tmp_path / "data" / "memories.sqlite", enabled=False))

    assert cli.main(["memory", "forget"]) == cli.EXIT_OK
    assert "都未启用" in capsys.readouterr().out


def test_list_hides_expired_and_aged_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """过期与超龄事实不出现在 list：存储层的 ``active`` 行不等于"可召回"（复审实证）。"""
    database = tmp_path / "data" / "memories.sqlite"
    project = project_id_for(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(days=5)
    _, visible_id = _seed(database, project=project)
    _, expired_id = _seed(
        database, project=project, key="old_key", expires_at="2000-01-01T00:00:00+00:00"
    )
    _, aged_id = _seed(database, project=project, key="aged_key", now=old)
    _use(monkeypatch, _config(database, forget_after_days=1))

    cli.main(["memory", "list", "--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert "[长期事实] 1 条" in output
    assert visible_id in output
    assert expired_id not in output  # 召回不可见 → 人工也看不到
    assert aged_id not in output


def test_forget_dry_run_covers_the_whole_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """dry-run 与实际执行**同范围（整库）**：别的项目的超龄事实也要计入预览。"""
    database = tmp_path / "data" / "memories.sqlite"
    other = tmp_path / "other"
    other.mkdir()
    old = datetime.now(timezone.utc) - timedelta(days=5)
    _seed(database, project=project_id_for(tmp_path), now=old)
    _seed(database, project=project_id_for(other), key="other_key", now=old)
    _use(monkeypatch, _config(database, forget_after_days=1))

    code = cli.main(["memory", "forget", "--dry-run"])
    output = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert "自动老化 2 条" in output  # 不再只预览当前工作区（复审实证：预览 0 条、实际 1 条）


def test_visible_scope_keys_include_the_workspace_scope(tmp_path: Path) -> None:
    """可见作用域必须含 ``workspace``：漏掉它会出现"模型召回得到、人工看不到"。"""
    keys = visible_scope_keys(
        user_id=None, project_id=project_id_for(tmp_path), workspace_root=tmp_path
    )

    assert [scope for scope, _ in keys] == ["global", "user", "project", "workspace"]


def test_medium_list_applies_scope_and_expiry_filters() -> None:
    """中期列出与召回同口径：作用域条件下推给向量库，过期项在本地排除。"""
    collection = _FakeCollection(
        [
            {"id": "keep", "content": "可见", "metadata": {"scope": "project", "scope_key": "k"}},
            {
                "id": "stale",
                "content": "已过期",
                "metadata": {
                    "scope": "project",
                    "scope_key": "k",
                    "expires_at": "2000-01-01T00:00:00+00:00",
                },
            },
        ]
    )

    hits = _medium_store(collection).list_records(
        scope_keys=[(SCOPE_PROJECT, "k")], limit=5
    )

    assert [hit.memory_id for hit in hits] == ["keep"]
    [call] = collection.get_calls
    assert call["where"] is not None  # 作用域过滤不会被漏掉
    assert "k" in str(call["where"])


def test_medium_delete_reports_actual_removed_count() -> None:
    """删除返回**实际**条数：不存在的 id 不再被报成"删除成功"（复审实证）。"""
    collection = _FakeCollection(
        [{"id": "a", "content": "x", "metadata": {"scope": "project", "scope_key": "k"}}]
    )
    store = _medium_store(collection)

    assert store.delete(["missing"]) == 0
    assert collection.deleted == []  # 不存在就不发删除请求
    assert store.delete(["a"]) == 1
    assert collection.deleted == [["a"]]
    assert store.delete(["a", "a"]) == 1  # 同一 id 重复请求只算一次


class _FakeEmbedding:
    """假 Embedding 适配器：只实现探针用到的 ``embed_query``。"""

    def __init__(self, *, dimension: int = 512, error: Exception | None = None) -> None:
        self.dimension = dimension
        self.error = error
        self.calls = 0

    def embed_query(self, text: str) -> list[list[float]]:
        del text
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [[0.1] * self.dimension]


def test_probe_reports_dimension_and_reuse_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``memory probe`` 是**显式**探针：真正加载并编码，打印耗时、维度与自检结论。

    复审实证：``memory list --kind medium`` 只读向量库（适配器惰性），空缓存 + 离线时照样成功，
    用它判断"权重可用"会误判——所以需要这条会真正触发加载的命令。
    """
    adapter = _FakeEmbedding()
    monkeypatch.setattr(
        cli.embedding_module, "build_embedding_function", lambda config, *, api_key=None: adapter
    )
    _use(monkeypatch, _enable_embedding(_config(tmp_path / "data" / "memories.sqlite")))

    code = cli.main(["memory", "probe", "--text", "我习惯用 pytest"])
    output = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert adapter.calls == 2  # 默认跑两次：首次与同进程复用
    assert "维度 512" in output
    assert "全部为有限值（无 NaN/Inf）：True" in output
    assert "结论：Embedding 可用" in output


def test_probe_reports_load_failure_with_readable_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """加载失败要给出可读原因并返回非零码（而不是被当成"成功"）。"""
    adapter = _FakeEmbedding(error=EmbeddingLoadError("模型下载失败：连接超时（cache_dir=D:\\cache）"))
    monkeypatch.setattr(
        cli.embedding_module, "build_embedding_function", lambda config, *, api_key=None: adapter
    )
    _use(monkeypatch, _enable_embedding(_config(tmp_path / "data" / "memories.sqlite")))

    code = cli.main(["memory", "probe"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_FAILURE
    assert "模型下载失败" in captured.err


def test_probe_requires_enabled_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Embedding 关闭时说清楚无需探测，不假装做过检查。"""
    config = _config(tmp_path / "data" / "memories.sqlite")
    disabled = config.model_copy(
        update={"embedding": config.embedding.model_copy(update={"enabled": False})}
    )
    _use(monkeypatch, disabled)

    code = cli.main(["memory", "probe"])

    assert code == cli.EXIT_USAGE
    assert "未启用" in capsys.readouterr().out


def test_medium_list_maps_flat_get_results() -> None:
    """中期列出用的是 Chroma ``get`` 的**扁平**结果形状（与查询结果的嵌套形状不同）。"""
    result: dict[str, Any] = {
        "ids": ["mt-1"],
        "documents": ["用户习惯用 pytest"],
        "metadatas": [{"scope": "user"}],
    }

    hits = medium_term._hits_from_get(result)

    assert [hit.memory_id for hit in hits] == ["mt-1"]
    assert hits[0].content == "用户习惯用 pytest"
    assert hits[0].metadata == {"scope": "user"}


def test_delete_refuses_facts_outside_the_workspace_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """默认只能删**当前工作区可见作用域**内的记录：别的项目的事实拒绝删除且不写库。

    复审要点：作用域检查必须在删除**之前**——只在删除后打印作用域，输错 ID 时已经误删了。
    """
    database = tmp_path / "data" / "memories.sqlite"
    other = tmp_path / "other"
    other.mkdir()
    _, other_fact = _seed(database, project=project_id_for(other))
    _use(monkeypatch, _config(database))

    code = cli.main(["memory", "delete", other_fact, "--yes", "--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert code == cli.EXIT_FAILURE
    assert "拒绝删除" in output and "--workspace" in output
    store = long_term.open_store(database)
    try:
        record = store.get_fact(other_fact)
        assert record is not None and record.status == "active"  # 一个字没改
    finally:
        store.close()

    # 显式指定那个项目的工作区后可以删（清理旧项目的正当路径）
    assert (
        cli.main(["memory", "delete", other_fact, "--yes", "--workspace", str(other)])
        == cli.EXIT_OK
    )
    assert "已软删除事实" in capsys.readouterr().out


def test_delete_entity_respects_the_workspace_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """实体删除同样受作用域限制（实体下挂着一串事实，越界删除影响更大）。"""
    database = tmp_path / "data" / "memories.sqlite"
    other = tmp_path / "other"
    other.mkdir()
    other_entity, _ = _seed(database, project=project_id_for(other))
    _use(monkeypatch, _config(database))

    code = cli.main(
        ["memory", "delete", other_entity, "--kind", "entity", "--yes", "--workspace", str(tmp_path)]
    )

    assert code == cli.EXIT_FAILURE
    assert "拒绝删除" in capsys.readouterr().out
    store = long_term.open_store(database)
    try:
        assert store.get_entity(other_entity) is not None
    finally:
        store.close()


def test_delete_allows_user_scope_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``user`` / ``global`` 仍在可见范围内（按既定可见性规则），照常可清理。"""
    database = tmp_path / "data" / "memories.sqlite"
    store = long_term.open_store(database)
    try:
        entity = store.record_entity(
            entity_type="user", name="local", scope=SCOPE_USER, user_id=None
        )
        fact = store.upsert_fact(
            entity_id=entity.id,
            key="habit",
            value="pytest",
            confidence=0.9,
            importance=0.8,
            source="test",
            origin="owner",
        )
    finally:
        store.close()
    _use(monkeypatch, _config(database))

    code = cli.main(
        ["memory", "delete", fact.record.id, "--yes", "--workspace", str(tmp_path)]
    )

    assert code == cli.EXIT_OK
    assert "已软删除事实" in capsys.readouterr().out


def test_medium_delete_refuses_records_outside_the_workspace_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """中期删除受同一限制，并且拒绝时**不发删除请求**。"""
    collection = _FakeCollection(
        [
            {
                "id": "m1",
                "content": "别的项目的记忆",
                "metadata": {"scope": "project", "scope_key": "other-project"},
            }
        ]
    )
    monkeypatch.setattr(cli, "_require_medium", lambda config: _medium_store(collection))
    _use(monkeypatch, _config(tmp_path / "data" / "memories.sqlite"))

    code = cli.main(
        ["memory", "delete", "m1", "--kind", "medium", "--yes", "--workspace", str(tmp_path)]
    )

    assert code == cli.EXIT_FAILURE
    assert "拒绝删除" in capsys.readouterr().out
    assert collection.deleted == []  # 没有发出删除请求


def test_medium_find_record_returns_scope_metadata() -> None:
    """按 id 取记录要能拿到作用域（删除前校验的依据），找不到时返回 ``None``。"""
    collection = _FakeCollection(
        [{"id": "m1", "content": "x", "metadata": {"scope": "user", "scope_key": "local"}}]
    )
    store = _medium_store(collection)

    hit = store.find_record("m1")

    assert hit is not None
    assert hit.metadata == {"scope": "user", "scope_key": "local"}
    assert store.find_record("missing") is None


def test_sidebar_management_lines_count_long_term_memory(tmp_path: Path) -> None:
    """侧栏展示长期记忆条数并指到命令行；库打不开时给可处理警告而不是抛异常。"""
    database = tmp_path / "data" / "memories.sqlite"
    _seed(database, project=project_id_for(tmp_path))
    app = SimpleNamespace(app_config=_config(database), workspace_root=tmp_path)

    lines, warn = web.memory_management_lines(app.app_config, app.workspace_root)

    assert warn is False
    assert any("事实 1 条" in line for line in lines)
    assert any("python -m local_agent.cli memory list" in line for line in lines)

    # 库路径指向一个目录（打不开）：只给警告行，不抛异常打断整个侧栏
    broken_path = tmp_path / "not-a-database"
    broken_path.mkdir()
    broken = SimpleNamespace(app_config=_config(broken_path), workspace_root=tmp_path)
    lines, warn = web.memory_management_lines(broken.app_config, broken.workspace_root)
    assert warn is True
    assert lines and "打不开" in lines[0]
