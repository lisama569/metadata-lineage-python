# 元数据目录与血缘影响分析服务

一个线程安全的内存式数据实体目录与字段级血缘服务（Python 3.14 + FastAPI）。
设计目标：在并发更新与结构演进下，**登记、关系维护与查询结论保持可预期**——
所有拒绝都有可区分的原因编码，所有变更（含失败）都有审计记录，字段的删除与
改名绝不静默丢失或错连血缘。

## 1. 概念模型

| 概念 | 说明 |
| --- | --- |
| `Entity` | 逻辑表/数据集；持有 `current_version`（结构版本号）与 `revision`（元数据修订号） |
| `EntityVersion` | 不可变结构快照（字段名+类型），版本号单调递增、永不覆盖 |
| `FieldType` | `INT < BIGINT < DOUBLE` 构成拓宽链；`STRING/BOOL/DATE/BINARY` 互相独立 |
| `LineageEdge` | 字段级有向边 `source -> target`（source 产出 target），状态 `ACTIVE/BROKEN` |
| `AuditRecord` | 只增不改的审计记录（序号、时间戳、actor、动作、成功与否、原因、明细） |

核心代码：

- `metadata_lineage/models.py`：领域模型与类型拓宽表
- `metadata_lineage/catalog.py`：实体登记、结构兼容判定、显式退役/改名、乐观锁更新
- `metadata_lineage/lineage.py`：血缘登记（查重/查环）、上下游影响分析
- `metadata_lineage/service.py`：把目录与血缘图装配为一致服务（端点校验 + 联动钩子）
- `metadata_lineage/app.py`：HTTP 接口

## 2. 结构兼容规则

`evolve_schema(entity_id, new_fields)` 对新旧快照做纯函数式判定：

| 变更 | 判定 | 拒绝原因 |
| --- | --- | --- |
| 字段不变 / 仅新增字段 | 兼容，产生新版本 | — |
| 同名字段类型沿拓宽链变化（`INT→BIGINT→DOUBLE`） | 兼容 | — |
| 同名字段反向收窄（如 `DOUBLE→INT`） | **拒绝** | `TYPE_NARROWED` |
| 同名字段变成无拓宽关系的类型（如 `INT→STRING`） | **拒绝** | `TYPE_INCOMPATIBLE` |
| 字段消失，且没有同类型新增字段与之配对 | **拒绝（隐式删除）** | `FIELD_REMOVED` |
| 字段消失，同时存在**同类型**的新增字段（一减一增，数量一致、按同类型可一一配对） | **拒绝（隐式改名）** | `FIELD_RENAMED` |

规则要点：

1. 不兼容的提交**不会产生新版本**，`current_version` 保持不变；历史版本快照全部保留，可经 `list_versions` / `get_version(v)` 追溯。
2. 删除字段的唯一合法路径是 **显式退役** `retire_field()`；改名的唯一合法路径是 **显式改名** `rename_field()`。这两条路径都会产生新版本并联动血缘。
3. 改名识别是确定性启发式（按同类型配对），文档化其边界：`STRING b` 消失 + `STRING b2` 新增判为改名；类型对不上则判为删除。不做猜测式自动跟随。

## 3. 血缘语义

### 3.1 登记

`add_edge(source, target)`，端点为 `(entity_id, field_name)`：

| 情况 | 结果 |
| --- | --- |
| 端点字段不存在 | 拒绝 `ENDPOINT_NOT_FOUND` |
| 端点字段已显式退役 | 拒绝 `ENDPOINT_RETIRED` |
| 使用改名前的旧字段名 | 拒绝 `ENDPOINT_NOT_FOUND`，错误信息给出当前名（不错连、不静默跟随） |
| 完全相同的 ACTIVE 边已存在 | 拒绝 `DUPLICATE_EDGE` |
| 自环，或登记后形成有向环 | 拒绝 `CYCLE_DETECTED`（查环沿 target 下游 BFS，能回到 source 即成环） |

同对边只有 BROKEN 记录时，重新登记会激活原边（保留首次登记时间）。

### 3.2 影响分析

- `upstream(entity, field)`：沿 ACTIVE 边**反向**闭包——所有直接/间接产出该字段的字段。
- `downstream(entity, field)`：沿 ACTIVE 边**正向**闭包——所有直接/间接依赖该字段的字段。
- 只遍历 ACTIVE 边，结论对入参确定。

### 3.3 删除/改名字段在血缘中的处理（关键稳定性约定）

- **显式退役** `retire_field()`：引用该字段的全部 ACTIVE 边**原子置为 `BROKEN` 并保留**。
  BROKEN 边不参与影响分析（链路被显式切断），但不会消失，可用 `broken_edges()` 与审计记录核查。
  之后不得再以退役字段登记血缘（`ENDPOINT_RETIRED`）。
- **显式改名** `rename_field()`：引用旧名的边**原子改写端点**、状态保持 ACTIVE，血缘连续；
  旧名登记一律拒绝。若改名使某条边与已有 ACTIVE 边完全撞键（改名产生重复血缘），
  改写得到的边置为 BROKEN 保留、已有 ACTIVE 边不动——**不覆盖、不错连、不丢失**。

## 4. 并发与审计约定

### 4.1 乐观锁（属主与标签）

实体有单调递增的 `revision`：结构演进、退役/改名、属主/标签的每次成功变更都 +1。

- `update_owner(entity, owner, expected_revision)`
- `update_tags(entity, add={...}, remove={...}, expected_revision=N)`（`expected_revision=None` 表示无条件写入）

三种结论通过 `UpdateOutcome` 明确区分，互不混淆：

| 结论 | 条件 | 是否生效 |
| --- | --- | --- |
| `APPLIED` | expected = 当前 revision 且校验通过 | 是，revision +1 |
| `REJECTED_STALE` | expected ≠ 当前 revision（过期写入） | 否，revision 不变 |
| `REJECTED_INVALID` | 空属主、空标签、add/remove 交集非空等 | 否，revision 不变 |

测试 `test_concurrent_tag_updates_exactly_one_wins` 用双线程屏障验证：同一 revision
上并发打不同标签时恰有一方 `APPLIED`、另一方 `REJECTED_STALE`，失败方可读取最新
revision 后重试成功。

### 4.2 审计

任何一次成功或失败的变更都追加一条 `AuditRecord`（序号连续、时间戳、actor、
动作名、明细、`success`、失败时的 `reason`）。失败的结构演进、过期写入同样留痕；
`audit_log()` 只增不改。HTTP 层可通过 `GET /audit` 查看。

## 5. HTTP 接口摘要

| 方法/路径 | 用途 |
| --- | --- |
| `POST /entities` | 登记实体 |
| `GET /entities/{id}` / `.../versions` | 查询实体 / 全部版本快照 |
| `POST /entities/{id}/evolve` | 提交结构演进 |
| `POST /entities/{id}/retire-field` | 显式退役字段 |
| `POST /entities/{id}/rename-field` | 显式改名字段 |
| `PUT /entities/{id}/owner` / `/tags` | 乐观锁更新（body 带 `expected_revision`） |
| `POST /lineage/edges` | 登记血缘 |
| `GET /lineage/edges?include_broken=true` | 列出边 |
| `GET /lineage/upstream/{id}?field=` / `downstream/...` | 影响范围 |
| `GET /audit` | 审计记录 |

错误映射：不存在类原因 → `404`；校验类 → `400`；结构不兼容/重复/环路/退役端点 →
`409`；过期写入也是 `409`。响应体统一为 `{"error": <原因编码>, "message": ...}`，
调用方按 `error` 区分失败原因，例如 `FIELD_REMOVED` 与 `FIELD_RENAMED`。

## 6. 本地验证方法

```bash
uv sync                       # 安装依赖（fastapi/pytest/httpx）
uv run pytest                 # 全部 21 个用例；live log 打印每个场景的输入与判定依据
uv run pytest -q              # 精简输出
uv run uvicorn main:app --reload   # 启动 HTTP 服务
```

测试覆盖：

- `tests/test_schema_evolution.py`：拓宽兼容、`TYPE_NARROWED` / `TYPE_INCOMPATIBLE` /
  `FIELD_REMOVED` / `FIELD_RENAMED` 四类拒绝可区分、版本留痕、显式退役/改名
- `tests/test_lineage.py`：重复登记、自环与环路、端点校验、上下游闭包、
  退役置 BROKEN 不丢失、改名端点原子改写
- `tests/test_concurrency_audit.py`：过期/校验失败/成功三分、双线程并发标签冲突
  恰一方成功、结构演进推进 revision 使旧写入过期、审计记录无缺失
- `tests/test_http_api.py`：HTTP 状态码与 `error` 原因编码

测试日志示例（`log_cli` 默认开启 WARNING，断言辅助 `log_decision` 打印输入与判定）：

```
=== 隐式改名 b->b2（同类型一减一增） ===
输入: {'fields': ['a:INT', 'b2:STRING']}
判定: REJECTED FIELD_RENAMED: possible implicit rename(s) b->b2; use rename_field() ...
```

## 7. 边界与取舍

- 存储为进程内内存结构，用 `RLock` 保证单次操作原子；分布式持久化不在本版范围。
- 改名识别采用“同类型一一配对”的确定性启发式，已在文档中固定其行为。
- BROKEN 边需要人工处置（重新接线或确认废弃）；系统不做自动修复以避免错连。
