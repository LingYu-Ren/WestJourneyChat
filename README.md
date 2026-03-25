

![西游记项目主视觉](pics/top.png)

《西游记》角色智能体系统是一个开源平台，可实现以特定角色的口吻和行为方式与用户互动。

- 仿真各个角色的智能体，建立了基于角色特征分析的画像模块、基于该角色参与事件行为细节的记忆模块。
- 智能体可与用户进行自然语言交流。
- 用户友好的前端界面。					

## 目录：

- [项目结构](#项目结构)

- [环境要求](#环境要求)

- [配置文件](#配置文件)

- [快速开始](#快速开始)

  (1)下载原始章节（可选）

  (2)测试 LLM 连通性

  (3)运行时间线抽取

- [关键脚本说明](#关键脚本说明)

- [常见问题与排查](#常见问题与排查)

- [注意事项](#注意事项)

- [参考文档](#参考文档)

- [Demo Video](#Demo-Video)

  

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
│  └─ download_journey_to_the_west.py
└─ test/
   └─ test_llm_connect.py
```

---

## 环境要求

- Python 3.10+
- 可访问 DashScope OpenAI 兼容接口

## 配置文件

根目录 `config.yaml` 需要包含如下字段：

```yaml
api_key: "你的DashScope API Key"

qwen:
  model: "qwen-plus"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  delay: 0.5
```

说明：

 timeline/timeline.py`当前主要使用`api_key`。

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

---

## 关键脚本说明

- `timeline/timeline.py`  
  滚动叙事抽取核心。每章执行“提取事件 → 更新角色 → 压缩摘要”。

- `scripts/download_journey_to_the_west.py`  
  从中文维基文库抓取章节文本并落盘。

---

## 常见问题与排查

1. 角色合并结果不符合预期  
   目前按“主名/别名交叉引用 + 简体归一”做并查集合并；若别名不完整，会影响聚合效果。
2. LLM 输出 JSON 解析失败  
   常见于模型返回额外文本或格式不规范，可重试或降低并发/提高提示约束。

---

## 注意事项

1. 不要将真实密钥和数据库密码提交到公开仓库。
2. `timeline/timeline.py` 当前默认批量处理 1~100 回，如需局部重跑建议改用类方法调用。

---

## 参考文档

- `timeline/timeline.py.md`

```sh
python scripts\run_frontend_server.py --port 8000
python ..\scripts\run_frontend_server.py --port 8000
```

## Demo Video


https://github.com/user-attachments/assets/b13babae-baee-43f7-a3c2-ee5ccafc4a4e


---

## 当前前端问答系统

项目当前已经形成一条完整的“前端角色页 -> 对话页 -> Neo4j 检索 -> 重排 -> LLM 回复”的问答链路。

### 框架组成

- `frontend/index.html` / `frontend/app.js`
  角色列表页，负责展示角色卡片、角色摘要、事件列表，并跳转到对话页。
- `frontend/dialogue.html` / `frontend/dialogue.js`
  对话页，负责维护浏览器侧会话历史，并将当前角色 seed 与最近历史发送到后端。
- `scripts/generate_frontend_data.py`
  从 Neo4j 读取 `Character` / `Event` 数据，生成 `frontend/data/top_roles.json` 与 `frontend/data/top_roles.js`，供前端页面直接使用。
- `scripts/run_frontend_server.py`
  启动本地前端服务，并负责角色初始化、图检索、重排、LLM 调用与日志落盘。
- `scripts/role_dialogue/`
  问答系统的后端能力包，包含角色初始化、图证据检索、上下文构建、重排与日志工具。

### 当前问答流程

当用户在前端点击某个角色并开始对话时，系统按以下步骤工作：

1. 前端从 `frontend/data/top_roles.js` 中读取当前角色的 `name`、`title`、`aliases`、`summary`、`personality`、`events`。
2. 对话页将“当前用户问题 + 当前角色 seed + 最近会话历史”发送到 `/api/role-dialogue`。
3. 后端优先根据角色 `title` 命中 Neo4j 中的 `Character` 节点，匹配优先级为：
   `Character.title` -> `Character.name` -> `Character.aliases`
4. 命中后，后端会用 Neo4j 中的 `summary`、`personality`、事件信息重新初始化当前角色上下文。
5. 随后后端检索该角色的一跳图谱证据：
   `(:Character)-[:PARTICIPATED_IN]->(:Event)`
6. 系统会把事件节点属性和关系属性压缩为候选文本，例如事件标题、地点、事件描述、角色状态、角色决策、结果等。
7. 使用 `qwen3-vl-rerank` 对这些候选文本按“当前用户问题相关性”重排。
8. 选出 Top-5 图谱证据，与角色摘要、角色标签、最近会话历史一起传给聊天模型 `qwen3.5-flash`。
9. LLM 生成最终角色回复并返回前端。

### 当前系统特性汇总

- 支持从 Neo4j 对角色做运行时初始化，而不是只依赖前端静态快照。
- 支持按用户当前问题动态检索角色相关图谱证据，而不是固定塞入全部事件。
- 支持 `qwen3-vl-rerank` 对候选事件证据做相关性排序。
- 默认将 Top-5 图谱证据放入问答上下文，降低无关上下文噪声。
- 会话历史由前端页面维护，后端每次请求只接收最近若干轮历史。
- Neo4j 不可用或驱动缺失时，服务会自动降级为使用前端已有角色数据继续回答。
- 已加入后台文件日志，记录 Neo4j 命中、图检索、重排、LLM 生成与异常降级过程。

### 运行要求

运行当前前端问答系统前，需要满足以下条件：

- Python 环境已安装 `neo4j`、`pyyaml`、`openai`
- `config.yaml` 中已正确配置：
  `api_key`、`neo4j.uri`、`neo4j.user`、`neo4j.password`、`project_id`
- Neo4j 中已存在 `Character` 与 `Event` 数据
- `Character` 节点至少应包含 `title`、`name`、`summary` 等字段

建议安装依赖：

```bash
pip install neo4j pyyaml openai
```

### 推荐运行顺序

1. 如果 Neo4j 中的角色数据有更新，先重新生成前端角色数据：

```bash
python scripts\generate_frontend_data.py
```

2. 启动前端对话服务：

```bash
python scripts\run_frontend_server.py --port 8000
```

3. 浏览器访问：

```text
http://127.0.0.1:8000/frontend/
```

### 可选配置项

如需调整问答检索与重排行为，可在 `config.yaml` 的 `qwen` 下增加以下可选字段：

```yaml
qwen:
  chat_model: "qwen3.5-flash"
  rerank_model: "qwen3-vl-rerank"
  rerank_endpoint: "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
  graph_top_k: 5
  rerank_max_documents: 100
  rerank_timeout_seconds: 20
  history_limit: 12
```

这些参数含义如下：

- `chat_model`：最终生成角色回复的聊天模型
- `rerank_model`：图证据相关性排序模型
- `graph_top_k`：最终放入问答上下文的图谱证据条数
- `rerank_max_documents`：单次送入重排接口的最大候选条数
- `rerank_timeout_seconds`：重排接口请求超时
- `history_limit`：每次请求带入模型的最近历史轮数

当候选条目超过 `rerank_max_documents` 时，系统会先做分批粗排，再做二次重排，最后输出 Top-K。

### 日志

后端日志默认写入：

```text
logs/role_dialogue_server.log
```

日志内容覆盖以下阶段：

- 服务启动与关闭
- 角色初始化命中情况
- Neo4j 查询与图检索结果
- 重排请求、候选数、Top 结果预览
- LLM 生成时长、token 用量、回复摘要
- 所有降级与异常路径
