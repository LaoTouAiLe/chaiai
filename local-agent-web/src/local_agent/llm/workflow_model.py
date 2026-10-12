"""受控工作流的单次请求：克隆实际 SDK 资源，不改变父连接。"""

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI


def single_attempt_model(model: BaseChatModel, *, timeout: float | None = None) -> BaseChatModel:
    if isinstance(model, ChatOpenAI):
        if model.root_client is None:
            raise ValueError("无法禁用底层 SDK 重试")
        client = model.root_client.with_options(max_retries=0) if timeout is None else (
            model.root_client.with_options(max_retries=0, timeout=timeout))
        return model.model_copy(update={"max_retries": 0, "root_client": client,
                                        "client": client.chat.completions})
    if hasattr(model, "max_retries"):
        return model.model_copy(update={"max_retries": 0})
    return model
