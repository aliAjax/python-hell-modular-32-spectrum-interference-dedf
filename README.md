# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`GET /api/occupancy`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`GET|POST /api/items/<id>/seat`、`POST /api/items/<id>/seat/confirm`、`POST /api/items/<id>/seat/withdraw` 和 `GET /api/items/<id>/audit`。

协调席位采用共用占用账：干扰事件、协调席位与停用授权记入同一本账，按紧急等级优先、同级按提交先后占位；满员后候补并给出最早释放时间。占位后需 `confirm` 确认，停用授权才生效；两人确认同一位仅一人成功，后到者看到最新占用。释放（resolve）、取消（cancel）或事后发现越权（withdraw）时，只撤回该事件写入的席位与授权，其余事件继续沿用，空出席位由候补按优先级递补。写入失败会保留未完成步骤与重试次数（`operation_steps`），重试幂等，不重复占位、不重复写审计。没有占用记录的旧事件按未占用读取；`/api/state` 与页面展示席位余量、候补位次和阻塞原因。测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突以及占用账的占位/候补/递补/并发确认/越权撤回/幂等重试。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
