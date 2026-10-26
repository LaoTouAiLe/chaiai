"""受控 OpenAPI 的补充面：SSE 事件联合（T30）。

`GET /sessions/{id}/events` 是 `text/event-stream`、没有 `response_model`，所以 FastAPI 不会把事件
写进文档；但事件载荷**是传输契约的一部分**，前端必须按 `event_type` 判别（§15.1：契约只有一份，
不给前端手抄一遍再各自漂移的机会）。

因此这里把事件联合与它的全部载荷模型并进 `components.schemas`，并且**在装配点调用**
（`create_app` 包住 `app.openapi`）——这样真实服务的 `/api/openapi.json` 与
`scripts/export_openapi.py` 导出的受控文档逐字节一致；只加在导出路径上的话，前端就会按一份
与真实服务不同的契约生成类型（`tests/test_api.py` 有测试守着这一点，这条差异真的被测出来过）。
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import TypeAdapter

from local_agent.application.browser_models import BrowserCommand, BrowserFrame, BrowserStateMessage
from local_agent.application.events import EventEnvelope

#: 事件联合在 `components.schemas` 里的登记名；前端按这个名字引用。
EVENT_ENVELOPE_SCHEMA: Final[str] = "EventEnvelope"


def merge_event_schemas(document: dict[str, Any]) -> None:
    """把事件联合及其载荷模型并入文档（幂等）。

    已被路由登记过的同名 schema（例如 `SessionSnapshot`）保留原样：两处同源，
    真出现不一致会在重导出时立刻暴露，而不是被这里悄悄改写掉。

    走 `TypeAdapter` 而不是 `models_json_schema`：`EventEnvelope` 是 `Annotated` 别名，
    后者只接受模型类。`$ref` 用同一个模板，写法与 FastAPI 自己生成的引用一致。
    """
    schema = TypeAdapter(EventEnvelope).json_schema(mode="serialization", ref_template="#/components/schemas/{model}")
    payloads = dict(schema.pop("$defs", {}))
    payloads[EVENT_ENVELOPE_SCHEMA] = schema
    schemas = document.setdefault("components", {}).setdefault("schemas", {})
    for name, payload in payloads.items():
        schemas.setdefault(name, payload)
    # WebSocket 消息同样只维护一份传输契约，供前端生成类型。
    for model in (BrowserCommand, BrowserFrame, BrowserStateMessage):
        schemas[model.__name__] = model.model_json_schema(ref_template="#/components/schemas/{model}")
