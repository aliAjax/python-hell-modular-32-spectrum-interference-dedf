# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位和状态机，`src/repository.py` 管理 SQLite、版本、审计链与**协调席位占用账**，`src/service.py` 编排权限与两阶段占位，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。基础接口：`GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`（`assess`/`locate`/`coordinate`/`resolve`/`cancel`）和 `GET /api/items/<id>/audit`。

## 协调席位占用账

干扰事件、协调席位、停用授权共用同一本占用账（`occupancy` 表，按区域设席位池，默认容量 3，监管员可用 `POST /api/seats/capacity` 调整）。事件完成定位（`located`）后由协调员/监管员操作：

| 接口 | 说明 |
| --- | --- |
| `POST /api/items/<id>/occupancy` `{"occupancy_action":"apply","authorization_code":"REG-…","seat":"SEAT-1"?,"expected_release_at"?}` | 申请席位并登记停用授权。有空位→`held`；满员→`waiting`，返回 `earliest_release_at` 与候补位次。紧急等级取评估结果（critical>high>medium>low），同级按提交先后（`applied_at,id`）排序。 |
| `POST /api/items/<id>/occupancy` `{"occupancy_action":"confirm","seat"?}` | **确认后授权才生效**：账上转 `confirmed`、事件转 `suspended`。两人同时确认同一席位只有一人成功，后到者收到 409 `seat_taken` 并看到最新占用者（`details`）。 |
| `POST /api/items/<id>/occupancy` `{"occupancy_action":"release","reason"}` | 释放：只把该事件写入的席位标记 `released`、撤回其授权（事件由 `suspended` 退回 `located`），其余事件继续沿用；同时按优先级自动提补候补。 |
| `POST /api/items/<id>/occupancy` `{"occupancy_action":"revoke","reason"}` | 监管员事后发现越权：只撤销该事件的席位与授权（`revoked`），不影响其他事件。 |
| `GET /api/items/<id>/occupancy` | 该事件当前占用；**旧事件没有占用记录按 `unoccupied` 读取**。 |
| `GET /api/seats?region=<r>` | 页面数据：席位余量 `remaining`、占用列表、候补列表、`earliest_release_at`、每人 `blocked_reason`。 |
| `GET /api/occupancy/operations` / `GET /api/occupancy/operations/<opId>` | 未完成操作与重试。 |

保证：

- **只撤回自己写入的席位和授权**——释放、取消、结案或越权撤回都按事件维度作用于占用账，其他事件状态/席位不变。
- **结案/取消联动释放**——`resolve`、`cancel` 自动释放本事件席位并提补候补；`coordinate` 要求先确认。
- **写入失败可续跑**——每个操作（`occupancy_operations`）把步骤（`occupancy_steps`）与重试次数落盘；失败返回未完成操作 id，重试已完成步骤，**不重复占位、不重复审计**（审计事件带幂等键，账本行记录所属操作）。
- 根页面 `/` 实时显示各区域席位余量、候补队列与阻塞原因。

协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
