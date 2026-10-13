"""业务状态定义；与 Agent 工单的 QUEUED、WAITING_APPROVAL 等执行状态分开。"""
from enum import StrEnum


class OrderStatus(StrEnum):
    CREATE = "CREATE"
    DELIVERING = "DELIVERING"
    ON_HOLD = "ON_HOLD"
    DELIVERED = "DELIVERED"
    RETURNED = "RETURNED"


class DeliveryTaskStatus(StrEnum):
    CREATE = "CREATE"
    PENDING_ASSIGN = "PENDING_ASSIGN"
    ASSIGNED = "ASSIGNED"
    COMPLETE = "COMPLETE"


ORDER_DESCRIPTIONS = {
    OrderStatus.CREATE: ("Create", "订单刚刚创建"),
    OrderStatus.DELIVERING: ("Delivering", "骑手已接单"),
    OrderStatus.ON_HOLD: ("On Hold", "骑手因某些原因暂时搁置派送"),
    OrderStatus.DELIVERED: ("Delivered", "订单派送完成"),
    OrderStatus.RETURNED: ("Returned", "异常情况导致无法派送"),
}
TASK_DESCRIPTIONS = {
    DeliveryTaskStatus.CREATE: ("Create", "派送任务刚刚创建"),
    DeliveryTaskStatus.PENDING_ASSIGN: ("Pending Assign", "已有订单加入，等待骑手接任务"),
    DeliveryTaskStatus.ASSIGNED: ("Assigned", "骑手已接派送任务"),
    DeliveryTaskStatus.COMPLETE: ("Complete", "任务内所有订单均已派送完成"),
}


# 向 API 与模型展示统一的业务状态名称和含义。
def status_catalog():
    return {
        "order": [{"code": code.value, "name": label, "description": meaning}
                    for code, (label, meaning) in ORDER_DESCRIPTIONS.items()],
        "delivery_task": [{"code": code.value, "name": label, "description": meaning}
                          for code, (label, meaning) in TASK_DESCRIPTIONS.items()],
    }


# 派送进行中对应 Assigned，全部成员送达对应 Complete；缺少聚合事实时无法确认一致性。
def delivery_task_consistent(task):
    return bool(task and (
        (task["task_status"] == DeliveryTaskStatus.ASSIGNED and task.get("all_orders_delivered") is False) or (
            task["task_status"] == DeliveryTaskStatus.COMPLETE and task.get("all_orders_delivered") is True
        )
    ))


# 验收单张订单时允许任务仍为 Assigned；Complete 必须有全部成员送达的查询结果。
def verify_business_state(order, task, playbook_id):
    if order is None:
        return {"success": False, "checks": {"order_exists": False}}
    task_valid = delivery_task_consistent(task)
    if playbook_id == "APPLY_DELIVERY_COMPLETION_V1":
        checks = {"order_delivered": order["status"] == OrderStatus.DELIVERED,
                  "task_state_consistent": task_valid}
    elif playbook_id == "APPLY_PICKUP_SCAN_V1":
        checks = {"order_delivering": order["status"] == OrderStatus.DELIVERING,
                  "task_assigned": bool(task and task["task_status"] == DeliveryTaskStatus.ASSIGNED)}
    elif playbook_id == "REBUILD_DISPATCH_PROJECTION_V1":
        checks = {"projection_matches_source": order["projection_status"] == order["status"]}
    else:
        checks = {"known_playbook": False}
    return {"success": all(checks.values()), "checks": checks}
