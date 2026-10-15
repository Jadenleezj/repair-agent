"""DeepSeek 专用协议适配：JSON Output、普通工具调用和非思考模式。"""
from __future__ import annotations

from copy import deepcopy
import json

from repair_agent.agent.models import DiagnosisResult, TriageResult
from repair_agent.agent.reasoning import HttpJsonReasoner, ModelT


class DeepSeekReasoner(HttpJsonReasoner):
    """复用业务推理、工具校验和 HTTP 预算，仅覆盖 DeepSeek 请求格式。"""

    def _complete(self, output_model: type[ModelT], system: str, payload: dict) -> ModelT:
        examples = {
            TriageResult: {
                "order_id": None, "issue_type": "UNKNOWN", "claims": [],
                "missing_information": ["order_id"],
            },
            DiagnosisResult: {
                "root_cause": None, "evidence_ids": [], "playbook_id": None,
                "historical_match": 0.0, "evidence_consistency": 0.0,
            },
        }
        instructions = (
            system + " Return only a JSON object conforming to this JSON Schema: "
            + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
            + "\nEXAMPLE JSON OUTPUT (shape only, not facts for the current case): "
            + json.dumps(examples[output_model], ensure_ascii=False)
        )
        message = self._request_message({
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
            "response_format": {"type": "json_object"},
        }, stage=output_model.__name__)
        return output_model.model_validate_json(message["content"])

    def _request_message(self, body: dict, *, stage: str | None = None) -> dict:
        # 独立副本，不改变通用推理器生成的工具定义或调用方请求。
        request_body = deepcopy(body)
        request_body["thinking"] = {"type": "disabled"}
        request_body.setdefault("max_tokens", 4096)
        request_body.pop("parallel_tool_calls", None)
        for definition in request_body.get("tools", []):
            definition["function"].pop("strict", None)
        return super()._request_message(request_body, stage=stage)
