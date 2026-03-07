# `scripts/import_roles_to_neo4j.py` 全面说明文档

## 1. 脚本定位

`import_roles_to_neo4j.py` 把两类原始数据清洗整合后导入 Neo4j，形成可查询的**人物-事件图谱**：

| 输入 | 说明 |
|---|---|
| `timeline/events.json` | 故事时间线事件主数据 |
| `roles/*.json` | 每个角色档案（繁/简混合） |

脚本内置完整流水线，最终产出三类图元素：

- `(:Character)` 节点
- `(:Event)` 节点
- `(Character)-[PARTICIPATED_IN]->(Event)` 关系（附角色参与属性）
- `(Event)-[FOLLOWS]->(Event)` 关系（事件时序链）

---

## 2. 依赖与运行环境

### 2.1 Python 依赖

```bash
pip install zhconv pyyaml openai neo4j
```

若缺少任何依赖，脚本在启动阶段直接 `sys.exit(1)` 并打印安装提示。

### 2.2 外部服务依赖

| 服务 | 说明 |
|---|---|
| **Qwen** | 阿里云 DashScope，OpenAI 兼容接口，model=`qwen-plus` |
| **Neo4j** | 本地 `neo4j://127.0.0.1:7687`，启动时调用 `verify_connectivity()` |

### 2.3 配置来源

- `config.yaml`（项目根）：读取 `api_key` 用于 Qwen 调用
- 脚本内硬编码常量：`NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / PROJECT_ID / QWEN_DELAY`

---

## 3. 执行流程（6 步）

```
[1/6] 读取 timeline/events.json
[2/6] 读取 roles/*.json（繁→简）
[3/6] Union-Find 分组（合并同一角色的多个档案）
[4/6] 连接 Neo4j
[5/6] 写入 Event 节点 + FOLLOWS 时序边
[6/6] 逐角色：合并字段 → Qwen 优化 → 写 Character 节点 → 写 PARTICIPATED_IN 边
```

---

## 4. 代码结构总览

### 4.1 常量与客户端初始化

| 常量 | 值 |
|---|---|
| `ROLES_DIR` | `../roles/` |
| `EVENTS_FILE` | `../timeline/events.json` |
| `NEO4J_URI` | `neo4j://127.0.0.1:7687` |
| `QWEN_MODEL` | `qwen-plus` |
| `QWEN_DELAY` | `0.5s`（API 节流） |

`OpenAI` 客户端以 DashScope base_url 初始化，用于后续 Qwen 调用。

### 4.2 工具函数

| 函数 | 说明 |
|---|---|
| `sc(value)` | 递归将字符串/列表/字典转换为简体中文（zhconv） |
| `norm(name)` | `strip + sc`，用于角色名比对的规范化键 |

### 4.3 读取阶段

**`read_events()`**
- 加载 `timeline/events.json`
- 对整个结构执行 `sc()` 转换
- 返回 `{event_id: event_dict}` 字典

**`read_roles()`**
- 遍历 `roles/*.json`
- `json.load` 后对整个结构执行 `sc()`
- 附加 `_source`（源文件名 stem）便于日志追踪
- 单文件失败只警告，不中断整体导入

### 4.4 分组阶段（Union-Find）

**`build_groups(roles)`** 通过并查集聚合同一角色：

1. 构建 `name_idx`（主名索引）与 `alias_idx`（别名索引）
2. 触发合并的三种条件：
   - A 角色主名出现在 B 的别名里
   - A 角色某别名等于 B 的主名
   - A 角色某别名等于 B 的别名
3. 按根节点输出分组列表

例：`石猴.json`、`美猴王.json`、`孫悟空.json` 因别名交叉，最终合并为一个角色。

### 4.5 组内合并阶段

**`merge_group(group)`**：

- 单元素组：直接返回（移除 `_source`）
- 多元素组：
  - 以 `len(summary) + len(description)` 最大的成员为 primary
  - `aliases` = 所有主名/别名集合去 primary 后排序
  - `personality / traits / abilities`：按插入顺序去重拼接
  - `description`：不同描述用 `" / "` 连接
  - `summary`：不同摘要用换行连接
  - `events`：按 `event_id` 去重后排序

### 4.6 标签优化阶段

**`optimize_fields(role)`**：

1. 三类标签均空则跳过
2. 向 Qwen 发送中文提示词，要求压缩 `personality / traits / abilities`
3. 解码返回的 JSON（自动剥除 markdown 代码块）
4. 解析成功则覆盖原字段；失败则仅告警，保留原值继续写库

### 4.7 写库阶段

**`write_event_nodes(session, events_by_id)`**
- 来源：`timeline/events.json`
- 每条 `MERGE (ev:Event {event_id})` + `SET` 属性（见第 5.2 节）

**`write_event_order(session, events_by_id)`**
- 遍历每个事件的 `time.anchor_ref`
- `anchor_ref` 非空且指向已知事件时，建立：
  ```cypher
  (e:Event)-[:FOLLOWS]->(anchor:Event)
  ```
- `anchor_ref = null` 的事件（如开篇事件）自然成为链条起点

**`write_character(session, role)`**
- `MERGE (c:Character {name})` + `SET` 多个属性（含 `project_id`）

**`write_participations(session, role, events_by_id)`**
- 对角色档案中每个 `event_id` 条目：
  - 跳过 `events_by_id` 中不存在的 `event_id`
  - `MATCH` 已存在的 Character 和 Event 节点
  - `MERGE (c)-[r:PARTICIPATED_IN]->(e)`
  - `SET r.status / r.decision / r.result`（属性挂在**关系**上，非 Event 节点）

---

## 5. Neo4j 图模型

### 5.1 Character 节点

| 角色字段 | Neo4j 属性 | 说明 |
|---|---|---|
| `name` | `c.name` | 主键（MERGE 条件） |
| `title` | `c.title` | 同 name（简体） |
| `aliases` | `c.aliases` | 字符串数组 |
| `description` | `c.description` | 简介 |
| `summary` | `c.summary` | 故事摘要 |
| `personality` | `c.personality` | 优化后性格特质数组 |
| `traits` | `c.traits` | 优化后外在特征数组 |
| `abilities` | `c.abilities` | 优化后能力数组 |
| `PROJECT_ID` | `c.project_id` | 项目标识 |

### 5.2 Event 节点

来源：`timeline/events.json`

| JSON 字段 | Neo4j 属性 | 说明 |
|---|---|---|
| `event_id` | `ev.event_id` | 主键（MERGE 条件） |
| `sequence` | `ev.sequence` | 顺序号 |
| `title` | `ev.title` | 事件标题 |
| `description` | `ev.description` | 事件描述 |
| `cause` | `ev.cause` | 起因 |
| `consequence` | `ev.consequence` | 结果 |
| `location` | `ev.location` | 发生地点 |
| `event_type` | `ev.event_type` | 类型（起源/对话/战斗…） |
| `time.expression` | `ev.time_expression` | 时间描述文字 |
| `time.type` | `ev.time_type` | 时间类型（anchor/relative/duration/implicit） |
| `time.global_order` | `ev.global_order` | 全局排序号 |

### 5.3 关系总览

| 关系 | 方向 | 属性 | 说明 |
|---|---|---|---|
| `PARTICIPATED_IN` | `(Character)→(Event)` | `status` / `decision` / `result` | 角色参与事件的具体行为，来自 `roles/*.json` 的 events 数组 |
| `FOLLOWS` | `(Event)→(Event)` | 无 | 时序锚定关系，来自 `time.anchor_ref` |

#### PARTICIPATED_IN 属性说明

| 属性 | 来源字段 | 含义 |
|---|---|---|
| `r.status` | `event_status` | 角色在此事件中所处状态 |
| `r.decision` | `character_decision` | 角色的决策/行为 |
| `r.result` | `decision_result` | 该决策带来的结果 |

#### FOLLOWS 语义

```
(ch001_003)-[:FOLLOWS]->(ch001_002)-[:FOLLOWS]->(ch001_001)
```

查询某事件的完整前序链：

```cypher
MATCH path = (e:Event {event_id: "ch001_007"})-[:FOLLOWS*]->(root)
RETURN path
```

---

## 6. 幂等性分析

| 操作 | 幂等行为 |
|---|---|
| 写 Character | `MERGE` 不重复创建；`SET` 会覆盖属性 |
| 写 Event | `MERGE` 不重复创建；`SET` 会覆盖属性 |
| 写 PARTICIPATED_IN | `MERGE` 保证同一对 (Character, Event) 只有一条边；`SET` 会更新边属性 |
| 写 FOLLOWS | `MERGE` 保证同一对 (Event, Event) 只有一条边 |

---

## 7. 性能与复杂度

| 阶段 | 复杂度 | 瓶颈 |
|---|---|---|
| 文件读取 | O(N) | 磁盘 I/O |
| 分组 | O(N + A)（A=别名总数） | 近线性 |
| 合并 | 与组内字段长度相关 | 可忽略 |
| LLM 优化 | 每组一次远程调用 | **主要耗时** |
| 写库 Event | O(E)（E=事件数） | 网络往返 |
| 写库 Character + 边 | O(G × avg_events)（G=角色组数） | 网络往返 |

`QWEN_DELAY=0.5s` 每组优化后休眠，降低 API 频率风险。

---

## 8. 运行方式

```bash
python scripts/import_roles_to_neo4j.py
```

典型日志结构：

```
============================================================
Project ID : 6eecbd25-6d19-4ab1-85a2-453e5edd941c
Roles dir  : .../roles
Events file: .../timeline/events.json
Neo4j URI  : neo4j://127.0.0.1:7687
============================================================

[1/6] Reading timeline/events.json...
  N events loaded

[2/6] Reading role files...
  M files loaded

[3/6] Grouping same characters...
  M files → K unique characters

[4/6] Connecting to Neo4j...
  Connected

[5/6] Writing N Event nodes + ordering edges...
  Done

[6/6] Processing K characters...
  [  1/K] 孫悟空 + 石猴 + 美猴王
         personality 350→28  traits 80→15  abilities 120→30
         → saved: 孙悟空
  ...

Done — K characters + N events written to Neo4j.
```

---

## 9. 失败处理与容错

### 9.1 已实现

| 场景 | 处理方式 |
|---|---|
| 缺依赖库 | 启动即 `sys.exit(1)` + 提示安装命令 |
| 某角色文件读取失败 | 只 WARN，继续处理其余文件 |
| Qwen 优化调用失败（超时/JSON 解析错误） | 只 WARN，保留原标签继续写库 |
| `event_id` 在 events.json 中不存在 | 跳过该条参与关系（不创建悬空边） |

### 9.2 建议补充

1. Neo4j 写入失败的重试机制
2. Qwen 调用超时配置与指数退避
3. 导入前后节点/边数量统计对账
4. dry-run 模式（只打印将写入数据，不实际写库）

---

## 10. 维护者快速排查清单

导入异常时，建议按顺序排查：

1. `config.yaml` 中 `api_key` 是否有效（可先运行 `test/test_llm_connect.py` 验证）
2. Neo4j 服务是否启动，账号密码是否正确
3. `timeline/events.json` 是否完整
4. `roles/` 下是否有结构异常的 JSON 文件
5. 日志中是否大量出现 `WARN: ... optimization failed`
6. 若 `PARTICIPATED_IN` 边为 0，检查 `event_id` 是否与 `events.json` 匹配

---

## 11. 一句话总结

`import_roles_to_neo4j.py` 是一个"事件主数据建图 + 角色档案清洗合并 + 标签 AI 压缩 + 参与关系边属性化"的一体化导入脚本，图模型中事件属性来自 `timeline/events.json`，角色参与细节挂载在 `PARTICIPATED_IN` 关系属性上，事件间时序通过 `FOLLOWS` 边表达。
