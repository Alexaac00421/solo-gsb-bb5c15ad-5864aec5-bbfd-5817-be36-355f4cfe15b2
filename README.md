# 实验室设备校准追溯服务

基于 **FastAPI + PostgreSQL** 的校准证书登记与测量记录追溯服务。

- 登记设备校准证书：编号、有效起止时刻（**必须带时区**，起点 < 终点，区间**左闭右开** `[valid_from, valid_to)`）
- 同一设备证书有效期**不得重叠**：由数据库 exclusion 约束保证，**并发提交只有一份成功**，其余返回 409
- 测量记录：仅当证书**覆盖测量时刻且未撤销**时才接受写入；记录**一经写入不可修改**（数据库触发器强制）
- 测量更正：测量时刻或证书填错时可提交**带原因的更正**，原始记录与历次更正**只追加、不删改**；每次更正须携带当前版本号，**版本不符返回 409，同一版本的并发更正最多成功一次**
- 撤销证书**不删除**历史测量；查询时**实时**返回每条记录的当前有效性及原因
- 证书登记支持**幂等键**：相同键 + 相同内容重试返回首次结果；相同键 + 不同内容返回 409
- **测量批次写入**：一次提交批次键与有序记录，逐条沿用单条写入校验、**整批原子**（任一不合格返回其序号与原因且整批不落库）；批次键支持**幂等重放**——相同键 + 相同有序内容（时刻按 UTC 比较）即使证书后来撤销也返回首次结果，同键不同内容返回 409，并发同键至多生成一批
- **合规审计快照**：按设备编号与带时区的左闭右开时间窗生成不可变快照，返回快照标识与有效/无效总数；版本链与证书撤销状态取自**同一一致视图**（并发更正/撤销只能整体位于快照前或后），生成后明细与计数**永久冻结**；凭标识按测量编号**稳定分页**读取固定字段明细

## 一键启动

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
|---|---|
| HTTP API | http://localhost:8000 |
| 交互式文档 (Swagger UI) | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/密码 `postgres`/`postgres`，库名 `calibration`） |

数据库首次启动时自动执行 `db/init.sql` 完成建表初始化；API 容器等待数据库健康检查后启动，自身也带连接重试。

停止并清空数据：`docker compose down -v`

## 配置

通过环境变量覆盖（见 `app/config.py`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/calibration` | 数据库连接串 |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | `1` / `10` | 连接池大小 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库的重试次数（每秒一次） |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/calibration
psql "$DATABASE_URL" -f db/init.sql   # 初始化建表
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

统一错误格式：`{"error": {"code": "...", "message": "..."}}`

### 证书

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/certificates` | 登记证书（**要求 `Idempotency-Key` 请求头**），成功 201；幂等重放 200（响应头 `Idempotency-Replayed: true`） |
| `GET` | `/api/v1/certificates?device_id=&status=&limit=&offset=` | 列表（`status` 取 `active`/`revoked`） |
| `GET` | `/api/v1/certificates/{id}` | 详情 |
| `POST` | `/api/v1/certificates/{id}/revoke` | 撤销，请求体 `{"reason": "..."}` |

登记请求体：

```json
{
  "device_id": "EQ-001",
  "certificate_no": "CERT-2026-0001",
  "valid_from": "2026-01-01T00:00:00+08:00",
  "valid_to": "2027-01-01T00:00:00+08:00"
}
```

典型错误：

| 状态码 | code | 场景 |
|---|---|---|
| 422 | （校验错误） | 时刻缺时区、`valid_from >= valid_to` |
| 409 | `VALIDITY_OVERLAP` | 同设备有效期重叠（含并发冲突） |
| 409 | `DUPLICATE_CERTIFICATE_NO` | 同设备证书编号重复 |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | 同一幂等键提交了不同内容 |
| 409 | `CERTIFICATE_ALREADY_REVOKED` | 重复撤销 |

### 测量记录

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/measurements` | 写入单条测量记录，成功 201 |
| `POST` | `/api/v1/measurement-batches` | 按批次写入测量记录（批次键在请求体），成功 201；幂等重放 200（响应头 `Batch-Replayed: true`） |
| `GET` | `/api/v1/measurements?device_id=&limit=&offset=` | 列表，含实时有效性 |
| `GET` | `/api/v1/measurements/{id}` | 详情（原始登记版本），含实时有效性 |
| `POST` | `/api/v1/measurements/{id}/corrections` | 提交更正（**须携带当前版本号**），成功 201 返回新版本 |
| `GET` | `/api/v1/measurements/{id}/history` | 完整历史：原始版本 + 按序更正链 + 当前版本 |
| `POST` | `/api/v1/audit-snapshots` | 生成合规审计快照（设备 + 左闭右开时间窗），成功 201 |
| `GET` | `/api/v1/audit-snapshots/{snapshot_id}` | 快照摘要：时间窗、有效/无效总数 |
| `GET` | `/api/v1/audit-snapshots/{snapshot_id}/items?limit=&offset=` | 按测量编号升序稳定分页读取快照明细 |

写入请求体：

```json
{
  "device_id": "EQ-001",
  "measured_at": "2026-06-15T10:30:00+08:00",
  "certificate_no": "CERT-2026-0001"
}
```

仅当 `valid_from <= measured_at < valid_to` 且证书未撤销时接受，否则 422
（`CERTIFICATE_NOT_FOUND` / `CERTIFICATE_REVOKED` / `MEASUREMENT_TIME_NOT_COVERED`）。

响应中的 `validity` 按**当前**证书状态实时计算——证书撤销后，历史记录立即变为无效并给出原因：

```json
{
  "id": 1,
  "device_id": "EQ-001",
  "measured_at": "2026-06-15T02:30:00+00:00",
  "certificate_no": "CERT-2026-0001",
  "certificate_id": 1,
  "created_at": "2026-10-05T12:00:00+00:00",
  "validity": {
    "is_valid": false,
    "reason": "certificate revoked at 2026-10-05T13:00:00+00:00: calibration lab found drift"
  }
}
```

### 测量批次

实验室可一次上传**一批**测量记录。请求体包含调用方指定的 `batch_key` 和按顺序排列的 `records`（每条与单条写入结构一致，测量时刻**必须带时区**；批次限 1–1000 条）：

```json
{
  "batch_key": "lab-upload-20261006-001",
  "records": [
    {"device_id": "EQ-001", "measured_at": "2026-06-15T10:30:00+08:00", "certificate_no": "CERT-2026-0001"},
    {"device_id": "EQ-002", "measured_at": "2026-06-15T02:30:00+00:00", "certificate_no": "CERT-2026-0002"}
  ]
}
```

行为语义：

- **逐条沿用单条写入校验**：每条都校验证书归属（证书编号必须属于该设备）、未撤销、左闭右开有效期 `[valid_from, valid_to)`；
  **任一条不合格即整批回滚不落库**，返回第一条不合格记录的**序号（`index`，从 0 开始）**与原因，错误码与单条写入一致：

  ```json
  {"error": {"code": "CERTIFICATE_NOT_FOUND",
             "message": "no certificate 'CERT-2026-0002' registered for device 'EQ-001'",
             "index": 1}}
  ```
- **全部成功**：201 返回 `measurement_ids`，与输入记录顺序**一一对应**；批次写入的记录与单条写入一样**不可修改/删除**（同一触发器强制），并可用既有 `GET /measurements/...` 接口查询。
- **批次键幂等**：相同 `batch_key` + 相同有序内容重试返回**首次结果**——成功重放为 200（响应头 `Batch-Replayed: true`、响应体为首次的 `measurement_ids`），失败重放为首次的状态码/序号/原因；
  内容比较时测量时刻**统一换算 UTC**，同一时刻的不同时区写法（如 `10:30+08:00` 与 `11:30+09:00`）视为相同内容；记录顺序不同视为不同内容。
- **同键不同内容** → 409 `BATCH_KEY_CONFLICT`。
- **即使引用的证书后来被撤销**，同键同内容重放仍返回首次成功结果，不会重新校验、不会写入新记录。
- **并发提交同一键至多生成一批**：由 `batch_key` 唯一约束保证。若首个请求尚未提交，并发方收到 409 `BATCH_KEY_IN_FLIGHT`，稍后重试即重放；首个提交后并发方直接以 200 重放。
- **与证书撤销并发安全**：批次在**同一事务**内先按证书 id 升序对全部引用证书加 `FOR UPDATE` 行锁再逐条校验、写入；并发撤销要么先完成（批次看到已撤销并拒绝），要么等待批次提交后才生效，**不会出现通过校验却写入已撤销证书的批次**。
- 首次失败结果也会持久化（批次行状态为 `failed`，仅记录序号与原因），批次表与测量表一样只追加、不可改删。

成功响应：

```json
{
  "batch_key": "lab-upload-20261006-001",
  "measurement_ids": [8, 9],
  "created_at": "2026-10-06T14:26:49.193255+00:00"
}
```

典型错误：

| 状态码 | code | 场景 |
|---|---|---|
| 422 | `CERTIFICATE_NOT_FOUND` / `CERTIFICATE_REVOKED` / `MEASUREMENT_TIME_NOT_COVERED` | 第 `index` 条记录未通过单条写入校验（整批不落库，失败结果作为首次结果持久化） |
| 422 | （请求校验错误） | 时刻缺时区、`records` 为空或超过 1000 条、`batch_key` 为空 |
| 409 | `BATCH_KEY_CONFLICT` | 相同批次键提交了不同的有序内容 |
| 409 | `BATCH_KEY_IN_FLIGHT` | 相同批次键的首个请求仍在进行中，重试即可 |

### 测量更正

发现测量时刻或所用证书填错时，可对指定记录提交**带原因的更正**。原始登记与历次更正**只追加保存，任何版本不得修改或删除**（数据库触发器强制）。

- 原始登记为 **version 1**，每次更正追加一个版本（+1）；更正须携带 `base_version`（当前最新版本号），**版本不符返回 409 `VERSION_CONFLICT`**；同一版本的并发更正由行锁 + 唯一约束保证**最多成功一次**
- **设备编号不可更正**，始终沿用原始记录的值；`measured_at`（须带时区）与 `certificate_no` 至少提供一个，缺省字段沿用当前版本值
- 所选证书必须**属于该设备、覆盖新时刻且提交时未撤销**；校验与写入在同一事务内并对证书行加 `FOR UPDATE` 锁，并发撤销不会造成无效更正

更正请求体：

```json
{
  "base_version": 1,
  "measured_at": "2027-03-10T09:00:00+08:00",
  "certificate_no": "CERT-2027-0001",
  "reason": "recorded against wrong certificate; re-associated per lab audit"
}
```

成功 201 返回新版本（`version`、`measured_at`、`certificate_no`、`reason`、`validity` 等）。典型错误：

| 状态码 | code | 场景 |
|---|---|---|
| 404 | `MEASUREMENT_NOT_FOUND` | 测量记录不存在 |
| 409 | `VERSION_CONFLICT` | `base_version` 与当前最新版本不符（含并发更正冲突） |
| 422 | （校验错误） | 新时刻缺时区、两个更正字段都未提供 |
| 422 | `CERTIFICATE_NOT_FOUND` | 证书不存在或不属于该设备 |
| 422 | `CERTIFICATE_REVOKED` | 目标证书已撤销 |
| 422 | `MEASUREMENT_TIME_NOT_COVERED` | 证书有效期未覆盖新时刻 |

历史查询 `GET /api/v1/measurements/{id}/history` 一次返回**原始版本、按版本号升序的更正链和当前版本**；每个版本的有效性均按证书撤销状态**实时重算**，旧版历史保留可查：

```json
{
  "measurement_id": 1,
  "device_id": "EQ-001",
  "original":    {"version": 1, "measured_at": "2026-06-15T02:30:00+00:00", "certificate_no": "CERT-2026-0001", "reason": null, "validity": {"is_valid": true, "reason": "..."}, "...": "..."},
  "corrections": [{"version": 2, "measured_at": "2027-03-10T01:00:00+00:00", "certificate_no": "CERT-2027-0001", "reason": "recorded against wrong certificate; ...", "validity": {"is_valid": true, "reason": "..."}, "...": "..."}],
  "current":     {"version": 2, "...": "..."}
}
```

### 合规审计快照

实验室复核设备测量合规性时，按**设备编号**与**带时区的起止时刻**生成审计快照。时间窗为**左闭右开** `[window_from, window_to)`，起点必须早于终点。

生成请求体：

```json
{
  "device_id": "EQ-001",
  "window_from": "2026-01-01T00:00:00+08:00",
  "window_to":   "2027-01-01T00:00:00+08:00"
}
```

成功 201 返回**快照标识**（随机不可枚举的 `snapshot_id`）与有效/无效总数（`valid_count + invalid_count = total_count`）：

```json
{
  "snapshot_id": "be648a66a1594717bd492d512f1120c8",
  "device_id": "EQ-001",
  "window_from": "2025-12-31T16:00:00Z",
  "window_to":   "2026-12-31T16:00:00Z",
  "total_count": 3,
  "valid_count": 2,
  "invalid_count": 1,
  "created_at": "2026-10-06T12:00:00Z"
}
```

行为语义：

- **纳入范围**：单条写入与批次写入的测量**均纳入**；但每条测量只取其**最新版本**（无更正时为原始 version 1，有更正时为版本号最大的一行），仅当**该最新版本的测量时刻**落在窗内才计入。旧版本即使时刻在窗内也不重复计数；最新版本时刻已被更正到窗外的测量不再出现。
- **同一一致视图**：生成在单事务内以 `REPEATABLE READ` 隔离级别执行——"最新版本"的计算（更正链）与**证书撤销状态**取自同一数据库快照，并发的更正或撤销事务**只能整体位于快照之前或之后**，绝不会出现"计数按更正后、原因按撤销前"之类的撕裂视图。
- **永久冻结**：明细与计数在生成时**物化**到只追加的快照表；此后对测量的更正、证书撤销或任何新写入**都不会改变**已生成快照的明细与总数（快照表挂不可变触发器，数据库层拒绝 UPDATE/DELETE）。
- **空窗计数为零**：窗口内无任何最新版本时刻命中（含设备从未出现）时正常返回 201，三个计数均为 0，分页为空。
- **分页稳定**：凭 `snapshot_id` 读取明细，固定按 `measurement_id` **升序**稳定排序，`limit`（1–1000，默认 100）+ `offset`（≥ 0）翻页；页与页之间发生的任何业务变更都不影响已冻结的顺序和内容。

明细字段**固定**为测量编号、版本、时刻、证书编号、有效性及原因：

```json
{
  "snapshot_id": "be648a66a1594717bd492d512f1120c8",
  "device_id": "EQ-001",
  "window_from": "2025-12-31T16:00:00Z",
  "window_to":   "2026-12-31T16:00:00Z",
  "total_count": 3,
  "valid_count": 2,
  "invalid_count": 1,
  "limit": 100,
  "offset": 0,
  "items": [
    {
      "measurement_id": 2,
      "version": 1,
      "measured_at": "2026-08-31T23:00:00Z",
      "certificate_no": "CERT-2026-0001",
      "validity": {"is_valid": true, "reason": "certificate is active and covers the measurement time"}
    },
    {
      "measurement_id": 7,
      "version": 3,
      "measured_at": "2026-05-10T02:00:00Z",
      "certificate_no": "CERT-2026-0001",
      "validity": {"is_valid": false,
                   "reason": "certificate revoked at 2026-10-05T13:00:00+00:00: drift found in audit"}
    }
  ]
}
```

`validity` 为**生成时刻**冻结的证书撤销状态：撤销发生在快照之后时，旧快照中的记录保持 `is_valid: true`；撤销发生在快照之前时，记录冻结为 `is_valid: false` 且原因含撤销时刻与理由。实时查询接口（`GET /measurements...`）与追加式更正接口不受影响，仍按当前状态实时计算。

典型错误：

| 状态码 | code | 场景 |
|---|---|---|
| 422 | （请求校验错误） | 时刻缺时区、`window_from >= window_to`；请求被拒绝且**不留下任何快照** |
| 404 | `SNAPSHOT_NOT_FOUND` | 摘要或翻页接口使用了未知快照标识 |
| 422 | （分页校验错误） | `limit` 不在 1–1000、`offset` 为负 |

最小示例：

```bash
# 生成快照（201，返回 snapshot_id 与计数；响应头 Location 指向摘要）
curl -X POST http://localhost:8000/api/v1/audit-snapshots \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"EQ-001",
       "window_from":"2026-01-01T00:00:00+08:00",
       "window_to":"2027-01-01T00:00:00+08:00"}'

# 凭标识按测量编号稳定翻页
curl 'http://localhost:8000/api/v1/audit-snapshots/be648a66a1594717bd492d512f1120c8/items?limit=50&offset=0'
curl 'http://localhost:8000/api/v1/audit-snapshots/be648a66a1594717bd492d512f1120c8/items?limit=50&offset=50'
```

## 调用示例

完整端到端示例（登记 → 幂等重试 → 冲突 → 测量 → 撤销 → 有效性查询 → 更正 → 版本冲突/并发 → 历史查询）：

```bash
./examples/demo.sh
```

测量批次端到端示例（成功批次 → 幂等重放 → 同键异内容冲突 → 逐条校验失败/重放 → 撤销后重放 → 并发同键）：

```bash
./examples/batch_demo.sh
```

合规审计快照端到端示例（单条/批次写入 → 更正移出窗口 → 非法时间窗拒绝 → 生成与稳定翻页 → 空窗 → 撤销后重拍 → 旧快照冻结不变 → 未知标识 404）：

```bash
./examples/snapshot_demo.sh
```

最小示例：

```bash
# 登记证书
curl -X POST http://localhost:8000/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: reg-eq001-0001' \
  -d '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001",
       "valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}'

# 写入测量记录
curl -X POST http://localhost:8000/api/v1/measurements \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00",
       "certificate_no":"CERT-2026-0001"}'

# 撤销证书
curl -X POST http://localhost:8000/api/v1/certificates/1/revoke \
  -H 'Content-Type: application/json' \
  -d '{"reason":"calibration lab found drift"}'

# 查询测量记录当前有效性
curl http://localhost:8000/api/v1/measurements?device_id=EQ-001

# 更正测量记录 1：换证书并调整时刻（base_version 为当前最新版本号，原始登记为 1）
curl -X POST http://localhost:8000/api/v1/measurements/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"base_version":1,"measured_at":"2027-03-10T09:00:00+08:00",
       "certificate_no":"CERT-2027-0001","reason":"recorded against wrong certificate"}'

# 查询完整历史：原始版本 + 更正链 + 当前版本
curl http://localhost:8000/api/v1/measurements/1/history
```

批次写入最小示例：

```bash
# 一次提交两条（batch_key 由调用方指定）
curl -X POST http://localhost:8000/api/v1/measurement-batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_key":"lab-upload-20261006-001","records":[
        {"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-0001"},
        {"device_id":"EQ-001","measured_at":"2026-06-16T09:00:00+08:00","certificate_no":"CERT-2026-0001"}]}'
# -> 201 {"batch_key":"...","measurement_ids":[10,11],"created_at":"..."}

# 同键同内容重试（哪怕换等价时区写法、证书事后撤销）→ 200 返回首次的 [10,11]
curl -X POST http://localhost:8000/api/v1/measurement-batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_key":"lab-upload-20261006-001","records":[
        {"device_id":"EQ-001","measured_at":"2026-06-15T11:30:00+09:00","certificate_no":"CERT-2026-0001"},
        {"device_id":"EQ-001","measured_at":"2026-06-16T09:00:00+08:00","certificate_no":"CERT-2026-0001"}]}'
```

## 设计要点

- **区间语义**：有效期为 `[valid_from, valid_to)` 左闭右开，用 PostgreSQL `tstzrange(..., '[)')` 表达；端点相接的两段区间不算重叠。
- **并发不重叠**：`EXCLUDE USING gist (device_id WITH =, tstzrange WITH &&)` 约束在数据库层串行化并发写入，重叠提交只有一份成功，其余收到 409。
- **幂等**：`idempotency_key` 唯一约束 + 请求内容规范化哈希（时刻统一换算 UTC 后计算）；同键同内容返回首次结果，同键异内容 409。
- **测量写入一致性**：校验与写入在同一事务内，并对证书行加 `FOR UPDATE` 锁，防止校验后、写入前证书被并发撤销。
- **批次原子性**：批次在单事务内按输入顺序逐条执行与单条写入一致的校验，全部通过后才按序插入，任一失败整体回滚，绝不会落库半批。
- **批次键幂等**：`measurement_batches.batch_key` 唯一约束 + 有序内容规范化哈希（时刻统一换算 UTC；顺序参与哈希）；首次结果（成功的 `measurement_ids` 或失败的序号/原因）持久化在批次行中，重放直接返回、不重新校验，因此证书事后撤销不影响首次结果；并发同键由唯一约束串行化，至多一批落库，未决方收到 409 `BATCH_KEY_IN_FLIGHT` 重试即可。
- **批次与撤销互斥**：批次事务先按证书 id 升序（固定加锁顺序避免死锁）对全部引用证书加 `FOR UPDATE` 行锁，再校验与写入；并发撤销要么先于批次完成（批次按已撤销拒绝），要么在批次提交后才生效，不存在"通过校验却写入已撤销证书"的交错。
- **批次行只追加**：批次表挂与测量表相同的不可变触发器，首次结果不可改删；测量行以延迟外键（`DEFERRABLE INITIALLY DEFERRED`）关联批次，成功结果随批次行在事务末尾原子写入。
- **不可篡改**：测量表与更正表均挂 `BEFORE UPDATE OR DELETE` 触发器直接报错，应用层也不提供修改/删除接口。
- **追加式更正链**：更正写入 `measurement_corrections` 表，原始登记为 version 1、每次更正 +1；`(measurement_id, version)` 唯一约束 + 测量记录行锁串行化并发更正，同一版本的并发更正最多成功一次，其余收到 409 `VERSION_CONFLICT`。
- **更正校验一致性**：更正与撤销互斥——更正事务对目标证书行加 `FOR UPDATE` 锁后再校验撤销状态与有效期覆盖，并发撤销要么先完成（更正看到已撤销并拒绝）、要么等待更正提交，不会产生基于已撤销证书的无效更正。
- **撤销即生效**：测量查询实时 JOIN 证书状态，撤销立即反映到全部历史记录（含各更正版本）的有效性上，历史数据本身保留不动。
- **审计快照一致视图**：快照生成在单事务内以 `REPEATABLE READ` 运行，"每个测量的最新版本"（`LEFT JOIN LATERAL ... ORDER BY version DESC LIMIT 1`，无更正取原始登记）与证书撤销状态共享同一事务快照；并发更正/撤销要么已提交（整体计入）、要么未提交（完全不可见），不存在交错的撕裂视图。
- **审计快照物化冻结**：命中记录的版本、时刻、证书编号与按当时撤销状态计算的有效性/原因，连同有效/无效总数在生成时写入只追加的 `audit_snapshots` / `audit_snapshot_items` 两表（挂不可变触发器，数据库层拒绝改删）；此后更正、撤销、新写入均不触碰已生成快照。对外只暴露随机 `token`（32 位十六进制），顺序主键不外泄，无法被遍历枚举。
- **快照分页稳定**：明细物化后按 `measurement_id` 升序排序、`(snapshot_id, measurement_id)` 唯一，`limit/offset` 翻页的顺序与内容不受任何后续业务变更影响。

## 目录结构

```
├── app/
│   ├── main.py            # FastAPI 入口、生命周期、路由挂载
│   ├── config.py          # 环境变量配置
│   ├── db.py              # asyncpg 连接池
│   ├── errors.py          # 统一错误格式
│   ├── schemas.py         # 请求/响应模型（AwareDatetime 强制时区）
│   └── routers/
│       ├── certificates.py
│       ├── measurements.py
│       ├── measurement_batches.py
│       └── audit_snapshots.py # 合规审计快照生成 / 摘要 / 稳定分页
├── db/init.sql            # 建表、约束、不可变触发器（容器首次启动自动执行）
├── examples/demo.sh       # 端到端调用示例
├── examples/batch_demo.sh # 测量批次端到端调用示例
├── examples/snapshot_demo.sh # 审计快照端到端调用示例
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
