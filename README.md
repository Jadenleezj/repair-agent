# Delivery State Repair Agent

面向异常运单状态的辅助诊断与修复项目。Agent 查询当前证据、选择白名单 Playbook，通过风险门禁、人工审批、版本复核与幂等执行完成修复，并独立验收。

## 启动与验证

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
repair-agent --reset-demo --approve "运单 W1001 已签收但仍在派送中"
repair-agent-api
python -m unittest discover -s tests -v
```

HTTP 服务默认地址为 `http://127.0.0.1:8000`，接口文档位于 `/docs`。`POST /cases` 同步创建工单，待审批时使用 `POST /cases/{case_id}/approval`。模型只负责理解、规划与诊断，门禁与执行由程序控制。
