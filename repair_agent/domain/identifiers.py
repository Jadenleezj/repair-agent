"""业务标识解析。"""
import re

ORDER_PATTERN = re.compile(r"\bW\d{4,}\b", re.IGNORECASE)


# 提取 W 开头的订单号，统一大小写；缺失时返回 None。
def extract_order_id(text: str) -> str | None:
    match = ORDER_PATTERN.search(text)
    return match.group(0).upper() if match else None
