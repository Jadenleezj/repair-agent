"""统一限制 HTTP JSON 响应大小，及时关闭流与连接。"""
import json

from repair_agent.execution.reliability import CURRENT_RUN


# 按小块读取并校验总大小；超限或取消时退出上下文，释放 HTTP 连接。
def read_json_response(client, method, url, *, max_bytes, timeout, on_response=None, **kwargs):
    control = CURRENT_RUN.get()
    if control:
        control.check()
    with client.stream(method, url, timeout=timeout, **kwargs) as response:
        if on_response:
            on_response(response)
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=16384):
            if control:
                control.check()
            if len(body) + len(chunk) > max_bytes:
                raise ValueError("HTTP_RESPONSE_TOO_LARGE")
            body.extend(chunk)
        if control:
            control.check()
        return json.loads(body)
