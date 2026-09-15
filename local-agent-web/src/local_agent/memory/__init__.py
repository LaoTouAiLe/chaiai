"""三层记忆（架构 §10）。

本包**刻意不在 ``__init__`` 里导入任何子模块**：中期记忆依赖可选的 ``chromadb``，
长期记忆要连 SQLite，而 ``local_agent`` 的其他部分（含 ``persistence``）只需要
``memory.schemas`` 的字段契约。保持空导入表可以让"未装可选依赖也能启动"成立。
"""

from __future__ import annotations
