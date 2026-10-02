# 元数据目录与血缘影响分析服务

一套面向数据实体的元数据目录服务，提供：

1. 数据实体及字段结构的**登记与版本演进**，带兼容性判定；
2. 实体间**字段级血缘**登记、环路/重复检测与**带容量保护的**上下游影响分析；
3. 属主与标签的**乐观锁并发更新**（支持属主/标签互不干扰的细粒度语义），
   以及**批量原子更新**；
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
uv run pytest tests/test_owner_tags_semantics.py   # 细粒度属主/标签语义
uv run pytest tests/test_batch_update.py           # 批量原子更新 + 并发
uv run pytest tests/test_impact_limits.py          # 影响分析容量/截断/可复现
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
  - `downstream`：沿出边可到达的全部后代字段（BFS，按 `(entity_id, field_uid)`
    确定遍历顺序）；
  - 同时返回参与传播的 `active_edges` 与 `dangling_edges`。

#### 容量保护与截断约定（可选查询参数）

| 参数 | 含义 |
| --- | --- |
| `max_upstream_depth` / `max_downstream_depth` | 上/下游最多追溯的层数（按边计；`0`=不追） |
| `max_upstream_nodes` / `max_downstream_nodes` | 上/下游最多返回的节点数（不含起点） |

- 四个参数都可选；不传（或为 `null`）表示该方向不限。负数 → `VALIDATION_ERROR`（422）。
- 只要任一方向触到上限，响应 `truncated=true`，且
  `upstream_truncated` / `downstream_truncated` 分别标明原因：
  `"depth"`（触到层数上限）或 `"nodes"`（触到节点数上限）；未截断方向为 `null`。
- **深度截断判定精确**：仅当深度边界之外仍存在尚未访问到的节点时才标记截断；
  若边界外的边都指向已访问节点（菱形图收敛），不算遗漏、不标截断。
- **确定性 / 可复现**：遍历按层（BFS）展开，同一层邻居一律按
  `(entity_id, field_uid)` 排序后依次访问。同样的图与入参，多次调用的
  节点顺序与内容完全一致；截断时同样可复现。
- **容量保护与悬空边**：只有 `active` 边参与遍历；结构演进产生的悬空边
  （见下）即使在深度/节点上限下也绝不被当作有效传播，结论在演进后依然正确。
- 服务层等价方法：
  `CatalogService.impact(ref, max_upstream_depth=..., max_downstream_depth=...,
  max_upstream_nodes=..., max_downstream_nodes=...)`。

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
  - 输入本身不合法（如同一标签同时 add/remove、空标签、空白属主、
    `replace_tags` 与 add/remove 混用）→ `VALIDATION_ERROR`（422），revision 不变。
- 三种结果（成功 / 冲突 / 校验失败）错误码与 HTTP 状态码均**可明确区分**。
- 所有“读-判-写”在存储的同一把锁内原子完成；高并发下同一 revision 的并发写
  恰好一个成功，其余全部冲突（已由并发测试断言）。

#### 属主 / 标签的细粒度更新语义（“没提到”与“显式清空”严格区分）

更新属主与更新标签是**两件互相独立的事**：请求里没提到的一边必须原样保留，
不会被另一边的改动顺带改掉。

**属主 `owner`（三态）**

| 请求中的形态 | 含义 |
| --- | --- |
| 请求体**不含** `owner` 字段 | **没提到** → 属主保持原值（哪怕原值是 null） |
| `"owner": null` | **显式清空属主**（和“没填”不同） |
| `"owner": "bob"` | 设置为 bob（空白字符串非法，422） |

**标签集合（两种模式互斥）**

| 形态 | 含义 |
| --- | --- |
| `add_tags` / `remove_tags` | **增量改动**：在现有集合上加/减 |
| `replace_tags: ["a","b"]` | **整体替换**：标签集合直接变成本次给出的集合 |
| `replace_tags: []` | **整体清空全部标签**（空集合，而非“没提到”） |
| 不含任何标签字段 | 标签**原样不动** |
| `replace_tags: null` | 非法（422）：清空请用 `[]` |

`replace_tags` 不得与 `add_tags` / `remove_tags` 同时出现，否则 422。

> 服务层用哨兵 `metadata_lineage.service.UNSET` 表达“没提到”；HTTP 层依据
> 请求 JSON 中字段是否实际出现来判定（与是否为 `null` 无关）。审计 detail 中
> 会记录每次成功更新的 `owner_mode`（`set`/`cleared`/`unchanged`）与
> `tag_mode`（`add_remove`/`replace`/`unchanged`），便于核对“只改一边”。

### 批量原子更新

`POST /entities/owner-tags:batch`，请求体 `{"items": [...]}`，每条 item 与单实体
更新字段一致（`entity_id`、`expected_revision`、`owner`、`add_tags`、
`remove_tags`、`replace_tags`、`actor`），同样遵循上面的“没提到/显式清空/整体替换”
语义。

- **按各自版本号校验**：每个实体用自己携带的 `expected_revision` 做乐观锁判定，
  互不影响。
- **整批原子**：要么全部成功落库，要么只要有**任何一个**实体版本过期、输入不合法
  或实体不存在，**整批全部不落库**（包括自身校验已通过的条目）。
- 批量业务结果通过 HTTP 200 响应体返回（不使用整体错误码）：

  ```json
  {"batch_id": "...", "committed": false, "results": [
    {"index": 0, "entity_id": "a", "status": "BATCH_ABORTED",
     "reason_code": "BATCH_ABORTED", "current_revision": 1, "revision_after": null,
     "detail": "item passed its own checks but was not written because ..."},
    {"index": 1, "entity_id": "b", "status": "CONFLICT_REVISION", ...},
    {"index": 2, "entity_id": "c", "status": "VALIDATION_ERROR", ...},
    {"index": 3, "entity_id": "d", "status": "ENTITY_NOT_FOUND", ...}]}
  ```

  逐条状态：

  | status | 含义 |
  | --- | --- |
  | `SUCCESS` | 随整批一起提交成功（带 `revision_after`） |
  | `CONFLICT_REVISION` | 该实体版本号过期（`current_revision` 为当前值） |
  | `VALIDATION_ERROR` | 该条输入不合法（含同一实体在一批中重复出现） |
  | `ENTITY_NOT_FOUND` | 该实体不存在 |
  | `BATCH_ABORTED` | 本条自身校验通过，但因同批其它条目失败而**未写入** |

  仅当所有条目都为 `SUCCESS` 时 `committed=true`。空批次 → 422。
- **并发安全**：整批的“读-判-写”在存储同一把锁内完成；过期写不会因并发交错
  被误放行（8 个并发批量对同组实体同时以旧版本写入时恰有一批成功）。
- **审计**：每个实体的**每一次尝试**都有独立一条 `batch_update_owner_tags` 审计
  （成功/冲突/校验失败/未找到/因整批回滚而未写入均记录），共享同一 `batch_id`，
  绝不是整批只留一条。

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
| GET | `/lineage/impact` | 上下游影响分析（含 dangling 边；支持四个容量参数） |
| PUT | `/entities/{id}/owner-tags` | 乐观锁更新属主/标签（字段缺省=不动，`owner:null`=清空，`replace_tags`=整体替换） |
| POST | `/entities/owner-tags:batch` | 批量原子更新（逐条结果，整批提交或整批回滚） |
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

# 只整体替换标签（请求体不含 owner -> 属主原样保留）
curl -s -X PUT localhost:8000/entities/orders/owner-tags -H 'content-type: application/json' -d '{
  "expected_revision": 0, "replace_tags": ["pii", "finance"]}'

# 只显式清空属主（不含任何标签字段 -> 标签原样保留）
curl -s -X PUT localhost:8000/entities/orders/owner-tags -H 'content-type: application/json' -d '{
  "expected_revision": 1, "owner": null}'

# 批量原子更新：各带各的版本号；任一失败整批回滚
curl -s -X POST localhost:8000/entities/owner-tags:batch -H 'content-type: application/json' -d '{
  "items": [
    {"entity_id": "orders", "expected_revision": 2, "add_tags": ["critical"]},
    {"entity_id": "payments", "expected_revision": 0, "owner": "team-x"}]}'

# 影响分析带容量保护：下游最多追 3 层、最多返回 10 个节点
curl -s "localhost:8000/lineage/impact?entity_id=orders&field_name=id&max_downstream_depth=3&max_downstream_nodes=10"
```
