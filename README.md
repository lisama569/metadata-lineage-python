# 元数据目录与血缘影响分析服务

一套面向数据实体的元数据目录服务，提供：

1. 数据实体及字段结构的**登记与版本演进**，带兼容性判定；
2. 实体间**字段级血缘**登记、环路/重复检测与上下游影响分析；
3. 属主与标签的**乐观锁并发更新**；
4. 所有成功、被拒绝、校验失败的操作均留下**不可缺失的审计记录**。

## 目录结构

```
metadata_lineage/
  models.py   # 领域模型、错误码、兼容性判定纯函数
  storage.py  # 线程安全内存存储（集中锁 + 索引 + 只追加审计）
  service.py  # 业务服务（演进/血缘/影响/CAS 更新）
  api.py      # FastAPI 路由与错误码 -> HTTP 状态码映射
tests/        # 演进 / 血缘 / 并发审计 / HTTP 四组测试
main.py       # 本地开发入口
```

## 本地验证方法

```bash
# 安装依赖（含 dev 组的 pytest）
uv sync --group dev

# 全量测试（会在 WARNING 级别打印每个关键用例的“输入/判定依据”）
uv run pytest

# 只跑某一组场景
uv run pytest tests/test_schema_evolution.py
uv run pytest tests/test_lineage.py
uv run pytest tests/test_concurrency_audit.py
uv run pytest tests/test_api.py

# 启动服务并交互式试用
uv run fastapi dev main.py        # http://127.0.0.1:8000/docs
```

## 一、结构兼容规则

每个实体拥有一条**不可变版本链**（v1, v2, …）。字段有稳定的 `uid`：
同名字段沿用旧 uid；新增字段分配新 uid；显式改名通过提交 `uid` 把新旧名字
锚定到同一字段身份。

| 变更 | 兼容性 | 拒绝错误码 |
| --- | --- | --- |
| 新增**可空**字段 | 兼容 | — |
| 新增**非空**字段（无默认值支持） | 不兼容 | `BREAKING_NULLABILITY_NARROWED` |
| 删除字段 | 不兼容 | `BREAKING_FIELD_REMOVED` |
| 字段改名（同 uid 换名） | 不兼容 | `BREAKING_FIELD_RENAMED` |
| 类型宽化（见下表） | 兼容 | — |
| 类型收窄（宽化的逆操作） | 不兼容 | `BREAKING_TYPE_NARROWING` |
| 互不相关的类型变更 | 不兼容 | `BREAKING_INCOMPATIBLE_TYPE` |
| 可空 -> 非空（可空性收窄） | 不兼容 | `BREAKING_NULLABILITY_NARROWED` |

允许的类型宽化方向（反向即收窄）：

```
INT32  -> INT64 / FLOAT64 / DECIMAL / STRING
INT64  -> FLOAT64 / DECIMAL / STRING
FLOAT64 -> DECIMAL / STRING
DECIMAL -> STRING
BOOL   -> STRING
BYTES  -> STRING
DATE   -> TIMESTAMP / STRING
TIMESTAMP -> STRING
```

- 不兼容演进**默认被拒绝**，不产生新版本；响应（`extra.breaking_changes`）
  会列出全部不兼容明细，每个明细带可区分错误码。
- 确需发布破坏性版本时可显式 `allow_breaking=true`：版本照常记录、血缘按下文
  “结构演进下的血缘语义”处理，审计中记录全部 breaking 明细与被断开的边。
- 兼容变更保留完整版本记录，字段 uid 在版本间稳定可追溯。

## 二、血缘语义

血缘是**字段级有向边** `(src.entity, src.field_uid) -> (dst.entity, dst.field_uid)`：

- **重复登记**：完全相同的端点对再次登记 → `LINEAGE_DUPLICATE`（409），不重复落库。
- **环路检测**：若新边会使图中出现环路（含自环）→ `LINEAGE_CYCLE`（409），
  边不落库。检测方式：若 `dst` 沿现有有效边已能到达 `src`，则拒绝。
- **影响分析**：`GET /lineage/impact?entity_id=...&field_name=...`
  - `upstream`：沿入边可到达的全部祖先字段；
  - `downstream`：沿出边可到达的全部后代字段（BFS，按 edge_id 确定顺序）；
  - 同时返回参与传播的 `active_edges` 与 `dangling_edges`。

### 结构演进下，被删除/改名字段的处理语义（稳定、不静默丢失、不错连）

- **改名**：字段 uid 不变。血缘边保持 `active`，自动指向新名字；
  影响分析中上下游关系不断、不重定向到其它字段。
- **删除**（仅 `allow_breaking=true` 时允许）：引用该字段 uid 的血缘边被标记为
  `active=false`（**悬空保留**，永不删除、永不静默消失）：
  - 这些边不参与上下游遍历，因此不会“穿透”到错误的下游（不错连）；
  - 仍可在影响结果的 `dangling_edges` 与边存储中查询到；
  - 在最新结构上引用已删除字段登记新血缘 → `LINEAGE_FIELD_NOT_FOUND`。

## 三、并发与审计约定

### 乐观锁（属主 / 标签）

- 每个实体维护单调递增的 `revision`，初始为 0，仅在成功更新后 +1。
- 更新必须携带 `expected_revision`：
  - 与当前一致 → 成功，返回新 revision；
  - 过期 → `CONFLICT_REVISION`（409），写入不生效，revision 不变；
  - 输入本身不合法（如同一标签同时 add/remove、空标签）→
    `VALIDATION_ERROR`（422），revision 不变。
- 三种结果（成功 / 冲突 / 校验失败）错误码与 HTTP 状态码均**可明确区分**。
- 所有“读-判-写”在存储的同一把锁内原子完成；高并发下同一 revision 的并发写
  恰好一个成功，其余全部冲突（已由并发测试断言）。

### 审计（不可缺失、只追加）

每次接口调用（含成功 `success`、业务拒绝 `rejected`、输入校验失败
`validation_failed`）都会追加一条审计，包含：

```
audit_id, at, actor, action, target, status,
reason_code, detail, revision_before, revision_after
```

- 审计只追加，不修改、不删除；在锁内写入，保证顺序与可见性。
- 冲突与拒绝的审计带有 `revision_before`，成功更新同时带前后版本号。
- 通过 `GET /audit` 查询；并发测试断言“N 次尝试必有 N 条审计”，无丢失。

## 四、HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/entities/{id}` | 登记实体（v1） |
| POST | `/entities/{id}/versions` | 登记新版本（默认拒绝不兼容；`allow_breaking` 放行） |
| GET | `/entities/{id}` | 查询实体与全部版本 |
| POST | `/lineage` | 登记字段级血缘（按实体+字段名解析为 uid） |
| GET | `/lineage/impact` | 上下游影响分析（含 dangling 边） |
| PUT | `/entities/{id}/owner-tags` | 乐观锁更新属主/标签 |
| GET | `/audit?limit=` | 查询审计记录 |

错误响应统一格式：

```json
{"error": {"code": "BREAKING_FIELD_REMOVED",
           "detail": "field 'amount' is absent from the new version",
           "extra": {"breaking_changes": [ ... ]}}}
```

状态码映射：未找到 404；冲突类（不兼容演进 / 重复 / 环路 / 实体已存在 /
过期写）409；输入校验失败 422；其它 400。

### 快速上手示例

```bash
uv run fastapi dev main.py &

curl -s -X POST localhost:8000/entities/orders -H 'content-type: application/json' -d '{
  "name": "orders",
  "fields": [
    {"name": "id", "type": "INT64", "nullable": false},
    {"name": "amount", "type": "INT32", "nullable": true}
  ],
  "actor": "alice"}'

# 兼容演进：INT32 -> INT64 宽化 + 新增可空字段
curl -s -X POST localhost:8000/entities/orders/versions -H 'content-type: application/json' -d '{
  "actor": "bob",
  "fields": [
    {"name": "id", "type": "INT64", "nullable": false},
    {"name": "amount", "type": "INT64", "nullable": true},
    {"name": "remark", "type": "STRING", "nullable": true}]}'
```
