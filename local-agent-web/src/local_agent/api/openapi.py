"""受控 OpenAPI 的产出（T29；计划 §6.2）。

前端的传输类型由这份文档生成，所以它必须**可复现**且**不依赖运行环境**：同一份代码任何时候导出
都逐字节一致，也不需要数据库、模型或密钥。做法是用一个**占位运行时**走 `create_app` 这个唯一装配点
——路由只在请求真的进来时才用运行时，中间件与静态托管也不影响文档内容。导出结果与真实应用注册的
面是否一致，由 `tests/test_api.py` 直接对比（真实应用的 `/api/openapi.json` == 落盘文件）。
"""

from __future__ import annotations

import json
from typing import Any, Final, cast

from local_agent.api.app import create_app
from local_agent.api.container import ApiRuntime
from local_agent.config import WebConfig

#: 受控文档的落盘位置（相对 `local-agent-web/`）。
#: 2026-10-25 起文档统一放在仓库根 `docs/`，所以这里多一层 `../`。
OPENAPI_PATH: Final[str] = "../docs/api/openapi.json"


class _PlaceholderRuntime:
    """装配时需要的最小面：`web` 给中间件用，`service`/`model_name`/`static_dir` 只在请求真的进来时才用。"""

    def __init__(self) -> None:
        self.web = WebConfig()
        self.static_dir = None
        self.service = None
        self.model_name = ""
        self.database = None
        self.model = None
        self.lock = None


def openapi_document() -> dict[str, Any]:
    """产出 OpenAPI 文档（与真实应用共用同一套路由与装配点）。

    SSE 事件面由装配点补进文档（见 `api/schema_extras.py`）：这里不再额外加工，
    否则导出与服务上的 `/api/openapi.json` 会变成两份不同的契约。
    """
    return create_app(cast(ApiRuntime, _PlaceholderRuntime())).openapi()


def openapi_json() -> str:
    """文档的规范化文本：键排序、缩进 2、非 ASCII 原样、结尾一个换行。

    排序是为了让 diff 只反映**真的变化**（字典插入顺序不算变化），生成物因此可以入库。
    """
    return json.dumps(openapi_document(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
