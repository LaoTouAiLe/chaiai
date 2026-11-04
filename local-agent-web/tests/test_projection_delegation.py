"""委派参数在投影层的暴露口径（2026-11-04）。

页面要在"还没跑完"时就摊出 N 条子任务卡，所以入参里的 `tasks` 必须能被前端拿到；
但**只能给目录**，不能把子任务的提问正文一起塞进参数区。
"""

from __future__ import annotations

import json

from local_agent.application.projection import _display_arguments


def test_delegation_tasks_expose_scopes_only() -> None:
    """只留 `scope`：提问正文属于对话内容，不该在参数展示里再出现一次。"""
    rendered = _display_arguments(
        {
            "tasks": [
                {"scope": "src/local_agent/tools", "question": "这里面有哪些会写文件的工具？"},
                {"scope": "src/local_agent/graph", "question": "节点怎么串的？"},
            ],
            "concurrency": 2,
        }
    )

    assert json.loads(rendered["tasks"]) == ["src/local_agent/tools", "src/local_agent/graph"]
    assert "会写文件的工具" not in rendered["tasks"]


def test_delegation_tasks_are_parsed_back_from_the_redacted_string() -> None:
    """真实数据是**字符串**，不是列表（2026-11-04 实测）。

    工具层 `_redacted_arguments` 把每个参数值都 `str()` 了，所以到这里 `tasks` 是
    ``[{'question': '...', 'scope': 'src/x'}]`` 这种 Python repr。不解析回来，
    前端 `JSON.parse` 必然失败，卡片就不知道派了几路——只能等全部跑完。
    """
    rendered = _display_arguments(
        {"tasks": "[{'question': '看看这里', 'scope': 'src/local_agent'}, "
                  "{'question': '再看看', 'scope': 'docs'}]"}
    )
    assert json.loads(rendered["tasks"]) == ["src/local_agent", "docs"]


def test_delegation_tasks_tolerate_odd_shapes() -> None:
    """入参形状不对时不要抛：投影层崩掉会让整条工具行消失。"""
    assert json.loads(_display_arguments({"tasks": []})["tasks"]) == []
    # 没有 scope 的条目直接跳过，而不是塞进一个空字符串占位。
    assert json.loads(_display_arguments({"tasks": [{"question": "没有 scope"}]})["tasks"]) == []
    # 不是列表时按普通参数处理（原样转字符串），不会误当成子任务去解析。
    assert _display_arguments({"tasks": "not-a-list"})["tasks"] == "not-a-list"
