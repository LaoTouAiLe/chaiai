"""会话轮次的全文检索索引（FTS5 + CJK 二元组）。

为什么要这一层（2026-11-02）：

``SessionStore.search_sessions`` 原来只对 ``title`` 与 ``turns.question`` 做 ``LIKE``，注释里
写明了不碰回答的两个理由：回答是长文本、``LIKE`` 扫全表会随历史线性变慢。这条理由成立，
代价是"我记得它答过什么"搜不到；而真正要回答"上次那个方案怎么定的"，答案往往在回答正文里。

三个设计约束（都是踩过的坑，不是自由发挥）：

1. **CJK 必须预分词**。Python 的 ``sqlite3`` 不暴露 ``sqlite3_create_tokenizer``，FTS5 的
   ``unicode61`` 会把一整段中文当成**一个词**——查询"瑞士轮"与文档里的"瑞士轮"才匹配得上，
   而"上次那个方案"里的"方案"永远切不出来。所以写入前把正文转成"二元组词串"
   （"瑞士轮" → ``瑞士 士轮``），查询走同一套切分，两边口径一致。
2. **只索引前若干字符**。回答单条可达数十 KB，尾部对召回没有增量收益，却会把库撑大；
   超出部分不索引（见 :data:`MAX_INDEX_CHARS`），不是"截断正文"，正文本身仍完整存在
   ``conversation_turns`` 里。
3. **失败开放**。本机 SQLite 没编 FTS5 时**不建表、不报错**，检索退回 LIKE。
   理由见 ``migrations._apply_turn_index_v17``：把"索引不可用"升级成"应用起不来"是
   2026-10-23 那次 ``no such table: terminal_audit`` 的同类错误。
4. **索引行主键跟着 ``conversation_turns.rowid``**。FTS5 的存储列没有索引，按
   ``session_id`` 查或删都要扫整张表，而每行都是几 KB 的正文——每次提交都把整个分支的
   轮次交过来，扫描等于每条消息读一遍全库正文。走 rowid 才是 O(log n)。

本模块只负责"索引与检索"，不决定谁能调、也不排序 UI 结果。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

#: 索引表名。虚拟表，没法和业务表共用外键，所以 ``session_id`` / ``turn_id`` 只做存储列。
TABLE = "session_turn_fts"

#: 单条轮次索引的正文字符上限；超出部分不进索引（正文仍在 conversation_turns 里）。
MAX_INDEX_CHARS = 8_000

#: 一次查询最多用多少个词参与 MATCH。超长查询（粘贴一段话）会把表达式撑成几千字节，
#: 而且召回质量不会更好——前 32 个词已经覆盖了用户真正想找的东西。
MAX_QUERY_TOKENS = 32

#: 单次检索最多取多少条命中轮次（去重成会话后远小于这个数）。
MAX_HITS = 400

_CREATE_SQL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5("
    "body, content_hash UNINDEXED, session_id UNINDEXED, turn_id UNINDEXED, "
    "tokenize='unicode61 remove_diacritics 0')"
)


@dataclass(frozen=True)
class TurnHit:
    """一次命中：哪一会的哪一轮。正文不回读，调用方需要时按 ``turn_id`` 自己取。"""

    session_id: str
    turn_id: str
    score: float


def is_cjk(char: str) -> bool:
    """是否按"逐字成词"处理（中日常用区 + 扩展 A/B + 兼容区）。"""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
    )


def tokenize(text: str) -> str:
    """正文 → 空格分隔的检索词串（CJK 二元组 + 拉丁/数字整词小写）。

    单字成段的 CJK（"的"）保留为单词：二元组切不出它，但它确实可能是用户输入的检索词。
    """
    tokens: list[str] = []
    run: list[str] = []
    word: list[str] = []

    def flush_run() -> None:
        if len(run) == 1:
            tokens.append(run[0])
        elif run:
            tokens.extend(run[index] + run[index + 1] for index in range(len(run) - 1))
        run.clear()

    def flush_word() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    for char in text[:MAX_INDEX_CHARS]:
        if is_cjk(char):
            flush_word()
            run.append(char)
        elif char.isalnum():
            flush_run()
            word.append(char.lower())
        else:
            flush_run()
            flush_word()
    flush_run()
    flush_word()
    return " ".join(tokens)


def match_expression(text: str) -> str | None:
    """用户输入 → FTS5 ``MATCH`` 表达式；没有可用词时返回 ``None``。

    用 ``OR`` 而不是 ``AND``：中文二元组切分后，长问句里必然夹着"怎么""这个"这类高频词，
    要求全部命中会把想找的东西挡在门外。相关度交给 bm25 排。
    """
    tokens = list(dict.fromkeys(tokenize(text).split()))[:MAX_QUERY_TOKENS]
    if not tokens:
        return None
    # 词只含 CJK 与字母数字，仍逐词加引号——FTS5 的裸词里 ``AND`` / ``OR`` / ``NEAR``
    # 是运算符，用户输入 "and" 时裸着会变成语法错误。
    return " OR ".join(f'"{token}"' for token in tokens)


def fts5_supported(connection: sqlite3.Connection) -> bool:
    """本机 SQLite 是否编进了 FTS5。判断只看编译选项，不建表、不改库。"""
    try:
        row = connection.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
    except sqlite3.Error:  # 老版本没有这个函数：当作不支持
        return False
    return bool(row and int(row[0]) == 1)


def table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def create(connection: sqlite3.Connection) -> None:
    """建索引表（幂等）。**只在确认支持 FTS5 之后调用**。"""
    connection.execute(_CREATE_SQL)


def _body_of(question: str, answer_text: str) -> str:
    # 问题与回答分开切再拼：避免"问题的最后一个字 + 回答的第一个字"凑出一个假二元组。
    return " ".join(part for part in (tokenize(question), tokenize(answer_text)) if part)


def _digest(question: str, answer_text: str) -> str:
    """**原文**的指纹，不是分词结果的指纹。

    每次提交都会把整个分支的轮次交过来，比对必须便宜：先算原文哈希（一遍字节拷贝）决定
    要不要重切，"先切词再比哈希"会把 O(轮数 × 正文长度) 的分词开销摊到每一次提交上。
    """
    digest = hashlib.sha1()
    digest.update(question.encode("utf-8"))
    digest.update(b"\x00")  # 分隔符：避免 "ab"+"c" 与 "a"+"bc" 撞成同一份指纹
    digest.update(answer_text.encode("utf-8"))
    return digest.hexdigest()


def sync_session(
    connection: sqlite3.Connection,
    session_id: str,
    turns: list[tuple[str, str, str]],
) -> None:
    """把这一会的轮次与索引对齐（``(turn_id, question, answer_text)``）。

    **用 ``conversation_turns.rowid`` 当索引行主键**，不是 ``(session_id, turn_id)`` 元组：
    FTS5 的存储列上没索引，按 ``session_id`` 查/删都要扫整张表——而这张表的每一行都是几 KB
    的正文。``ConversationStore.sync`` 每次提交都会把整个分支的轮次交过来，走扫描等于每条
    消息都读一遍全库正文。走 rowid 就是一次 O(log n) 的点查。

    **只写变化的行**：先比原文指纹（见 :func:`_digest`），变了才重新切词。
    """
    if not table_exists(connection):
        return
    rowids = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT turn_id, rowid FROM conversation_turns WHERE session_id = ?", (session_id,)
        )
    }
    for turn_id, question, answer_text in turns:
        rowid = rowids.get(turn_id)
        if rowid is None:
            # 轮次还没落库（调用顺序变化时的自保）：下一轮同步会补上，不在这里猜。
            continue
        digest = _digest(question, answer_text)
        current = connection.execute(
            f"SELECT content_hash FROM {TABLE} WHERE rowid = ?", (rowid,)
        ).fetchone()
        if current is not None and str(current[0]) == digest:
            continue
        connection.execute(f"DELETE FROM {TABLE} WHERE rowid = ?", (rowid,))
        body = _body_of(question, answer_text)
        if not body:
            # 问题与回答都为空：没有可检索内容，删掉旧行即可。
            continue
        connection.execute(
            f"INSERT INTO {TABLE}(rowid, body, content_hash, session_id, turn_id)"
            " VALUES (?,?,?,?,?)",
            (rowid, body, digest, session_id, turn_id),
        )


def delete_session(connection: sqlite3.Connection, session_id: str) -> None:
    """会话被彻底删除时清掉它的索引行（``SessionStore._delete_session_rows`` 调用）。

    **必须在删 ``conversation_turns`` 之前调用**：这里靠那些行的 rowid 定位索引行，走的是
    主键查找而不是扫整张索引表。调用顺序由 ``_delete_session_rows`` 保证。
    """
    if not table_exists(connection):
        return
    connection.execute(
        f"DELETE FROM {TABLE} WHERE rowid IN "
        "(SELECT rowid FROM conversation_turns WHERE session_id = ?)",
        (session_id,),
    )


def search(connection: sqlite3.Connection, query: str, *, limit: int = MAX_HITS) -> list[TurnHit]:
    """按相关度取命中轮次。索引不可用或词为空时返回空表，调用方自己退回 LIKE。"""
    expression = match_expression(query)
    if expression is None or not table_exists(connection):
        return []
    rows = connection.execute(
        f"SELECT session_id, turn_id, bm25({TABLE}) AS score FROM {TABLE} "
        f"WHERE {TABLE} MATCH ? ORDER BY score LIMIT ?",
        (expression, max(1, limit)),
    ).fetchall()
    return [TurnHit(str(row[0]), str(row[1]), float(row[2])) for row in rows]


def matched_session_ids(connection: sqlite3.Connection, query: str, *, limit: int = MAX_HITS) -> list[str]:
    """命中的会话 id（按首次命中的相关度去重）。会话级排序仍交给调用方。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for hit in search(connection, query, limit=limit):
        if hit.session_id in seen:
            continue
        seen.add(hit.session_id)
        ordered.append(hit.session_id)
    return ordered


@dataclass(frozen=True)
class TurnDetail:
    """一条带上下文的命中：会话标题、这一轮的问答原文，以及归档标记。

    正文回表取（索引里只存切好的词串），所以"搜得到就一定取得到"——除非这一轮刚被删掉，
    那时连 `conversation_turns` 都没有，JOIN 自然不会返回它。
    """

    session_id: str
    turn_id: str
    title: str
    question: str
    answer_text: str
    submitted_at: str | None
    archived: bool


def search_details(
    connection: sqlite3.Connection, query: str, *, limit: int = 5
) -> list[TurnDetail]:
    """按相关度取命中的**轮次明细**（`session_search` 工具用）。"""
    expression = match_expression(query)
    if expression is None or not table_exists(connection):
        return []
    # 虚拟表**不加别名**：FTS5 的 MATCH 左操作数要求是表名本身（加别名后表名不再可解析）。
    rows = connection.execute(
        f"SELECT {TABLE}.session_id, {TABLE}.turn_id, s.title, t.question, t.answer_text,"
        f" t.submitted_at, s.archived FROM {TABLE}"
        f" JOIN conversation_turns t ON t.rowid = {TABLE}.rowid"
        f" JOIN sessions s ON s.session_id = {TABLE}.session_id"
        f" WHERE {TABLE} MATCH ? ORDER BY bm25({TABLE}) LIMIT ?",
        (expression, max(1, limit)),
    ).fetchall()
    return [
        TurnDetail(
            session_id=str(row[0]),
            turn_id=str(row[1]),
            title=str(row[2] or ""),
            question=str(row[3] or ""),
            answer_text=str(row[4] or ""),
            submitted_at=None if row[5] is None else str(row[5]),
            archived=bool(row[6]),
        )
        for row in rows
    ]


def backfill(connection: sqlite3.Connection) -> int:
    """按 ``conversation_turns`` 重建整份索引，返回写入行数。

    **先清空再建**：迁移"建表成功但登记版本号前退出"会留下半份索引，如果是增量补写，
    那种库会永远缺前面的行，而且看不出来。一次性重建的代价可以接受（只在升级时跑一次）。
    """
    if not table_exists(connection):
        return 0
    connection.execute(f"DELETE FROM {TABLE}")
    written = 0
    cursor = connection.execute(
        "SELECT rowid, session_id, turn_id, question, answer_text FROM conversation_turns"
    )
    for rowid, session_id, turn_id, question, answer_text in cursor:
        raw_question, raw_answer = str(question or ""), str(answer_text or "")
        body = _body_of(raw_question, raw_answer)
        if not body:
            continue
        connection.execute(
            f"INSERT INTO {TABLE}(rowid, body, content_hash, session_id, turn_id)"
            " VALUES (?,?,?,?,?)",
            (int(rowid), body, _digest(raw_question, raw_answer), str(session_id), str(turn_id)),
        )
        written += 1
    return written
