"""可选按需代码检索（架构 §2、§5.3；开发文档 T21）。

子模块职责：

- :mod:`~local_agent.rag.search`：请求词项提取与候选发现（``rg`` 优先，缺失退化有界扫描）；
- :mod:`~local_agent.rag.snippets`：命中处附近的有界片段读取；
- :mod:`~local_agent.rag.retriever`：编排成 ``rag_refs``（架构 §4.3 的 ``RAGReference``）。

**不建持久索引**：每次查询直接读磁盘现状；``rag.enabled=false`` 时整包不会被调用
（节点直接透传，不访问文件系统、不探测 ``rg``、不加载任何解析器）。
"""

from __future__ import annotations
