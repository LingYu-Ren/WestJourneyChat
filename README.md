# ChatPlatformXiyOU

![西游记项目主视觉](pics/top.png)

基于《西游记》文本的叙事时间线抽取与角色图谱构建项目。  
项目当前主线能力：

1. 从维基文库下载并清洗章节文本。
2. 用 Qwen 对章节做事件抽取与滚动摘要更新。
3. 基于事件更新角色档案（含实体消歧）。
4. 将角色、事件、时序关系导入 Neo4j 图数据库。

---

## 项目结构

```text
.
├─ config.yaml
├─ data/journey_to_the_west/
│  ├─ chapters/chapter_001.txt ... chapter_100.txt
│  └─ journey_to_the_west.txt
├─ prompts/
│  ├─ extract_events.md
│  ├─ compress_summary.md
│  └─ update_character_profile.md
├─ timeline/
│  ├─ timeline.py
│  ├─ events.json
│  └─ state.json
├─ roles/
│  ├─ all_roles.txt
│  └─ *.json
├─ scripts/
│  ├─ download_journey_to_the_west.py
│  └─ import_roles_to_neo4j.py
└─ test/
   └─ test_llm_connect.py
```

---

## 环境要求

- Python 3.10+
- 可访问 DashScope OpenAI 兼容接口
- Neo4j（用于图谱导入）

安装依赖：

```bash
pip install openai pyyaml neo4j zhconv
```

---

## 配置文件

根目录 `config.yaml` 需要包含如下字段：

```yaml
api_key: "你的DashScope API Key"

neo4j:
  uri: "neo4j://127.0.0.1:7687"
  user: "neo4j"
  password: "你的Neo4j密码"

project_id: "一个UUID或项目唯一标识"

qwen:
  model: "qwen-plus"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  delay: 0.5
```

说明：

1. `timeline/timeline.py` 当前主要使用 `api_key`。
2. `scripts/import_roles_to_neo4j.py` 会使用 `neo4j.*`、`project_id`、`qwen.*`。

---

## 快速开始

## 1) 下载原始章节（可选）

如果 `data/journey_to_the_west/chapters` 已完整，可跳过。

```bash
python scripts/download_journey_to_the_west.py
```

可选参数：

```bash
python scripts/download_journey_to_the_west.py --output-dir data/journey_to_the_west --delay-seconds 0.2
```

## 2) 测试 LLM 连通性

```bash
python test/test_llm_connect.py
```

## 3) 运行时间线抽取

```bash
python timeline/timeline.py
```

当前代码默认会循环处理第 `1~100` 回，产出：

- `timeline/events.json`：全量事件列表
- `timeline/state.json`：滚动摘要状态
- `roles/*.json`：角色档案
- `roles/all_roles.txt`：角色主名索引

## 4) 导入 Neo4j

```bash
python scripts/import_roles_to_neo4j.py
```

脚本执行逻辑（最新代码）：

1. 读取 `timeline/events.json` 创建 Event 节点。
2. 根据 `time.anchor_ref` 建 `:FOLLOWS` 时序边。
3. 读取并合并 `roles/*.json` 创建 Character 节点。
4. 将角色参与信息写入 `(:Character)-[:PARTICIPATED_IN]->(:Event)` 关系属性。

---

## 图模型说明（Neo4j）

节点：

- `:Character`
- `:Event`

关系：

- `(:Character)-[r:PARTICIPATED_IN]->(:Event)`
- `(:Event)-[:FOLLOWS]->(:Event)`

说明：

1. `PARTICIPATED_IN` 的 `status/decision/result` 写在关系上，表示“该角色在该事件中的参与语义”。
2. `FOLLOWS` 由 `time.anchor_ref` 生成，表示相对时序锚定关系。

---

## 常用 Cypher 查询

统计节点与关系：

```cypher
MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY cnt DESC;
```

查看某角色参与事件（按全局顺序）：

```cypher
MATCH (c:Character {name: "孙悟空"})-[r:PARTICIPATED_IN]->(e:Event)
RETURN e.event_id, e.title, e.global_order, r.status, r.decision, r.result
ORDER BY e.global_order;
```

查看事件时序链：

```cypher
MATCH (e:Event)-[:FOLLOWS]->(a:Event)
RETURN e.event_id, e.title, a.event_id, a.title
LIMIT 100;
```

清空当前数据库全部节点与关系：

```cypher
MATCH (n) DETACH DELETE n;
```

---

## 关键脚本说明

- `timeline/timeline.py`  
  滚动叙事抽取核心。每章执行“提取事件 → 更新角色 → 压缩摘要”。

- `scripts/import_roles_to_neo4j.py`  
  图导入核心。读取 `events.json` + `roles`，做简繁归一、并查集合并、标签优化、图写入。

- `scripts/download_journey_to_the_west.py`  
  从中文维基文库抓取章节文本并落盘。

---

## 常见问题与排查

1. 报 `openai/pyyaml/neo4j/zhconv not installed`  
   执行依赖安装命令并确认当前 Python 环境正确。

2. Neo4j 连接失败  
   检查 `config.yaml` 中 `neo4j.uri/user/password`，确认数据库已启动且网络可达。

3. 导入脚本报找不到 `timeline/events.json`  
   先运行 `python timeline/timeline.py` 生成事件文件。

4. 角色合并结果不符合预期  
   目前按“主名/别名交叉引用 + 简体归一”做并查集合并；若别名不完整，会影响聚合效果。

5. LLM 输出 JSON 解析失败  
   常见于模型返回额外文本或格式不规范，可重试或降低并发/提高提示约束。

---

## 注意事项

1. 不要将真实密钥和数据库密码提交到公开仓库。
2. `timeline/timeline.py` 当前默认批量处理 1~100 回，如需局部重跑建议改用类方法调用。
3. `events.json` 与 `roles` 数据量较大时，导入脚本会运行较久（含 Qwen 标签优化步骤）。

---

## 参考文档

- `timeline/timeline.py.md`
- `scripts/import_roles_to_neo4j.py.md`
- `scripts/neo4j命令.md`

```sh
python scripts\run_frontend_server.py --port 8000
python ..\scripts\run_frontend_server.py --port 8000


```
