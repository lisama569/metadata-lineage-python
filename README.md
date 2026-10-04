# 元数据目录与血缘影响分析服务

一套面向数据实体的元数据目录服务，提供：

1. 数据实体及字段结构的**登记与版本演进**，带兼容性判定；
2. 实体间**字段级血缘**登记、环路/重复检测与上下游影响分析（带容量上限与截断标记）；
3. 属主与标签的**乐观锁并发更新**（增量改动 / 整体替换 / 显式清空），并支持**批量原子更新**；
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
uv run pytest tests/test_owner_tags_update.py   # 属主/标签细粒度更新语义
uv run pytest tests/test_batch_update.py        # 批量原子更新
uv run pytest tests/test_impact_limits.py       # 影响分析容量与截断
uv run pytest tests/test_api.py
uv run pytest tests/test_api_new_features.py    # 新能力的 HTTP 层行为
```

截断结论的专项验证集中在 `tests/test_impact_limits.py`，每个用例都会在
WARNING 日志中打印 `CASE <用例名> | INPUT/EVIDENCE: {...}`，可直接看出该
场景的图结构、上限参数、返回节点与判定依据：

```bash
# -s 保留日志输出；可用 -k 只看某一类场景
uv run pytest tests/test_impact_limits.py -s -k "converging or equals or control"
```

覆盖的场景包括：上限恰好覆盖全部节点（深度等于图深 / `max_nodes` 等于可达
节点数）、分支汇合与边界回流（挂着边但无新节点，判完整）、真正截断的对照
链/菱形图、同一输入 5 次调用结论一致、上限非法仍拒绝、强制演进后悬空边
不参与传播也不触发截断。

```bash
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

### 影响分析的容量上限与截断

图变大后可加容量保护（服务层 `impact(ref, max_depth=..., max_nodes=...)`，
HTTP 同名查询参数）：

- `max_depth`：沿每个方向最多追多少层（`1` = 仅直接邻居）；
- `max_nodes`：每个方向最多返回多少个节点；
- 两个参数都必须是正整数，否则 `VALIDATION_ERROR`（422）；
- **截断判据（`truncated`）**：只在「确实还有可达节点没有返回出来」时为
  `true`。遍历结束后，若存在一条当前有效边，其起点已在结果中、终点不在
  结果中，说明上限挡住了链路上更深处（或其他分支上）真实存在的节点，
  才标记截断；上下游两侧使用同一判据。
- **以下情形即使设了上限、边界上还挂着节点，也判为完整（`truncated=false`）**：
  - 上限完全够用：从该字段出发直接连出去的上下游节点已构成全部可达集合，
    再往外没有新节点；
  - 分支汇合：几条分支最终都连到前面已经出现过的节点（如
    `a->b, a->c, b->c`，从 a 追 1 层时边界节点 b 虽挂着 `b->c`，但 c
    已经作为 a 的直接邻居返回，没有遗漏）；
  - `max_nodes` 恰好等于实际可达节点数：返回的节点正好是全部可达节点，
    只有严格小于（存在没返回出来的节点）才算截断；
  - `max_depth` 等于图的实际深度：第 N 层节点的下游若都已在前面出现过
    （回指 / 汇合），同样完整返回；
- **真正截断的对照场景**：`a->b->c->d` 从 a 设 `max_depth=1`（c/d 未
  返回）、菱形图从 a 设 `max_nodes=2`（d 未返回）等，均为 `truncated=true`；
- **截断结论稳定可复现**：遍历按 edge_id 排序的 BFS，同样的输入与同样的图，
  多次调用返回的顺序与内容完全一致（被截断保留的是哪部分也一致）；
- 结构演进后悬空的边（`active=false`）不参与遍历，既不计入传播、也不触发
  截断标记，只出现在 `dangling_edges` 中。

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
  - 输入本身不合法（如同一标签同时 add/remove、空标签、set_tags 与增量混用、
    空白属主名）→ `VALIDATION_ERROR`（422），revision 不变。
- 三种结果（成功 / 冲突 / 校验失败）错误码与 HTTP 状态码均**可明确区分**。
- 所有“读-判-写”在存储的同一把锁内原子完成；高并发下同一 revision 的并发写
  恰好一个成功，其余全部冲突（已由并发测试断言）。

### 属主 / 标签更新语义：“没提到” ≠ “清空”

一次更新请求中，**凡是没有提到的内容一律保持原值**；属主与标签互不干扰：

| 请求内容 | 效果 |
| --- | --- |
| `owner` 字段不出现 | 属主保持原值 |
| `"owner": null`（显式空值） | **清空**属主 |
| `"owner": "bob"` | 属主改为 bob |
| `add_tags` / `remove_tags` | 标签**增量**改动（在现有集合上加/减） |
| `set_tags: [...]` | 标签**整体替换**为给定集合（`[]` 即清空全部标签） |
| 标签相关字段都不出现 | 标签保持原值 |

- `set_tags`（整体替换）与 `add_tags`/`remove_tags`（增量改动）**互斥**，
  同批给出 → `VALIDATION_ERROR`；
- 只改标签时属主原样不动，只改属主时标签原样不动；
- 服务层用 `_UNSET` 哨兵区分“未提供”与“显式 None”；HTTP 层按请求体中
  字段是否出现区分（`"owner": null` 是清空，不写 `owner` 是保持）。

### 批量原子更新

`POST /owner-tags/batch`（服务层 `batch_update_owner_tags`）一次提交多个实体的
属主/标签改动，每个条目带自己的 `expected_revision`：

- **整批原子性**：全部条目通过校验才落库；任何一条失败（版本过期 / 输入不合法 /
  实体不存在 / 同一实体在一批中重复出现），整批都不生效；
- **逐条结论**：响应始终为 200，`applied` 表示整批是否落库，`results` 中每条
  给出 `status`：
  - `success`：本条成功（仅当整批 applied=true）；
  - `conflict`（`CONFLICT_REVISION`）：该实体版本号过期；
  - `validation_failed`（`VALIDATION_ERROR`）：该条输入不合法；
  - `not_found`（`ENTITY_NOT_FOUND`）：实体不存在；
  - `aborted`（`BATCH_ABORTED`）：本条自身合法，但因整批原子性未落库；
- **并发安全**：整批的“读-判-写”在同一把锁内完成，并发下同一实体的过期
  批量写不会被误放行（恰有一批整体生效）；
- **审计**：每个实体的每次尝试都有独立审计记录（action=
  `batch_update_owner_tags`），成功条目带前后 revision，不会整批只留一条。

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
| GET | `/lineage/impact` | 上下游影响分析（含 dangling 边；`max_depth`/`max_nodes` 容量上限，`truncated` 截断标记） |
| PUT | `/entities/{id}/owner-tags` | 乐观锁更新属主/标签（增量 / `set_tags` 整体替换 / `owner:null` 显式清空） |
| POST | `/owner-tags/batch` | 批量原子更新属主/标签（逐条结论，整批全成或全不生效） |
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

# 标签整体替换（set_tags），owner 不提则保持原值
curl -s -X PUT localhost:8000/entities/orders/owner-tags -H 'content-type: application/json' -d '{
  "expected_revision": 0, "set_tags": ["pii", "core"], "actor": "carol"}'

# 显式清空属主（"owner": null），标签不动
curl -s -X PUT localhost:8000/entities/orders/owner-tags -H 'content-type: application/json' -d '{
  "expected_revision": 1, "owner": null, "actor": "carol"}'

# 批量原子更新：整批全成或全不生效，逐条返回结论
curl -s -X POST localhost:8000/owner-tags/batch -H 'content-type: application/json' -d '{
  "items": [
    {"entity_id": "orders", "expected_revision": 2, "owner": "dave"},
    {"entity_id": "customers", "expected_revision": 0, "set_tags": ["pii"]}]}'

# 影响分析加容量上限：最多追 2 层、每方向最多 10 个节点
curl -s 'localhost:8000/lineage/impact?entity_id=orders&field_name=amount&max_depth=2&max_nodes=10'
```
