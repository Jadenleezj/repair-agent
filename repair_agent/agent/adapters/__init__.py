"""按配置创建模型适配器；工作流不依赖具体服务商。"""
from urllib.parse import urlsplit

from repair_agent.agent.reasoning import HttpJsonReasoner
from repair_agent.agent.adapters.deepseek import DeepSeekReasoner


def create_reasoner(base_url: str, api_key: str, model: str, *, provider: str | None = None) -> HttpJsonReasoner:
    selected = (provider or "auto").lower()
    if selected == "auto":
        selected = "deepseek" if urlsplit(base_url).hostname == "api.deepseek.com" else "openai"
    if selected == "deepseek":
        return DeepSeekReasoner(base_url, api_key, model)
    if selected == "openai":
        return HttpJsonReasoner(base_url, api_key, model)
    raise ValueError("REPAIR_LLM_PROVIDER must be auto, openai or deepseek")
