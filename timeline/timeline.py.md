# `timeline/timeline.py` 全面说明文档

## 1. 文件定位与目标

`timeline/timeline.py` 是一个基于 LLM 的叙事时间线抽取与滚动记忆维护脚本，面向《西游记》章节文本实现以下能力：

1. 按章提取结构化事件（带时间、地点、人物、因果、全局顺序）。
2. 维护滚动摘要状态（`state.json`），为下一章提供上下文记忆。
3. 维护全局事件库（`events.json`）。
4. 维护角色档案（`roles/*.json` + `roles/all_roles.txt`），并做基础实体消歧。

核心设计思想是“逐章处理 + 历史压缩 + 持久化状态”，本质是一个轻量的长上下文代理流程。

---

## 2. 依赖与运行前提

## 2.1 Python 依赖

代码显式依赖：

- `openai`（兼容客户端）
- `pyyaml`
- Python 标准库：`json`、`re`、`pathlib`

## 2.2 配置文件

从项目根目录 `config.yaml` 读取配置（`_load_config`），当前只使用：

```yaml
api_key: <your_api_key>
```

## 2.3 LLM 接入

`_get_client` 固定通过 DashScope OpenAI 兼容接口调用：

- `base_url`: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- `model`: `qwen-plus`
- `stream=True`（流式拼接返回）

---

## 3. 目录与数据文件关系

`timeline.py` 运行时依赖以下路径（均由模块常量定义）：

| 路径 | 作用 | 读/写 |
|---|---|---|
| `data/journey_to_the_west/chapters/chapter_XXX.txt` | 输入章节原文 | 读 |
| `prompts/extract_events.md` | 事件抽取提示词模板 | 读 |
| `prompts/compress_summary.md` | 滚动摘要压缩提示词模板 | 读 |
| `prompts/update_character_profile.md` | 角色档案更新提示词模板 | 读 |
| `timeline/state.json` | 当前滚动状态 | 读写 |
| `timeline/events.json` | 全局事件列表（追加） | 读写 |
| `roles/all_roles.txt` | 已有角色 canonical 名称集合 | 读写 |
| `roles/<角色名>.json` | 单角色档案 | 读写 |

---

## 4. 总体处理流程

以 `process_chapter(chapter_num)` 为中心，执行顺序如下：

1. 读取 `chapter_XXX.txt`。
2. 加载 `state.json`（无文件则使用 `_INITIAL_STATE`）。
3. 若 `state["chapter_reached"] >= chapter_num`，直接跳过。
4. 调用 LLM 抽取本章事件（注入 rolling summary）。
5. 将新事件追加到 `events.json`。
6. 基于本章事件更新角色档案（含实体消歧）。
7. 调用 LLM 压缩出新的 rolling summary，写回 `state.json`。
8. 输出关键日志（时间锚点、累计时间、当前地点）。

> 批处理入口 `process_chapters(start, end)` 只是循环调用 `process_chapter`。

---

## 5. 核心数据结构契约

## 5.1 状态对象（`state.json`）

默认初始值 `_INITIAL_STATE`：

```json
{
  "chapter_reached": 0,
  "time_anchor": "故事尚未开始",
  "elapsed_summary": "无",
  "location": "未知",
  "active_characters": [],
  "recent_events": [],
  "open_threads": [],
  "last_global_order": 0
}
```

字段语义：

- `chapter_reached`: 已处理到的章节号（跳章保护依赖它）。
- `time_anchor`: 最新叙事时间锚点。
- `elapsed_summary`: 从故事起点累计到当前的时间摘要。
- `location`: 当前主叙事地点。
- `active_characters`: 主线活跃角色。
- `recent_events`: 最近关键事件（压缩后短句）。
- `open_threads`: 未闭合线索。
- `last_global_order`: 全书范围事件顺序号末值。

## 5.2 事件对象（`events.json` 的元素）

单条事件核心形态（来自 `extract_events.md` 约束）：

```json
{
  "event_id": "ch001_001",
  "sequence": 1,
  "time": {
    "expression": "元会之初",
    "type": "anchor",
    "anchor_ref": null,
    "global_order": 1
  },
  "location": "混沌虚空",
  "characters": [],
  "event_type": "背景",
  "title": "鸿蒙初判",
  "description": "天地未分，混沌始开",
  "cause": "故事尚未开始",
  "consequence": "确立宇宙时间尺度与演化框架"
}
```

关键点：

- `event_id` 期望全局唯一（代码本身不做全局去重）。
- `time.type` 受提示词约束：`relative | duration | implicit | anchor`。
- `global_order` 跨章节递增，依赖 `state.last_global_order` 上下文提示。

## 5.3 角色档案对象（`roles/<name>.json`）

角色档案字段（由 `update_character_profile.md` 约束）：

- `name`
- `aliases[]`
- `description`
- `personality[]`
- `traits[]`
- `abilities[]`
- `summary`（增量累积）
- `events[]`（含 `event_id`、`event_name`、`event_status`、`character_decision`、`decision_result`）

---

## 6. 代码结构详解（按函数）

下述行号基于当前 `timeline/timeline.py`。

## 6.1 模块常量与模板加载（1-41）

- 定位项目根目录、章节目录、状态/事件文件路径。
- 启动时一次性读取三个 Prompt 模板，减少重复 I/O。
- `_RESOLVE_PROMPT` 为实体消歧专用短提示词。
- `_INITIAL_STATE` 提供无状态启动保障。

## 6.2 通用工具函数（46-86）

1. `_load_config`：读取 `config.yaml`。
2. `_get_client`：创建 OpenAI 兼容客户端，固定 DashScope 地址。
3. `_parse_llm_json`：
   - 优先提取 ```json fenced code block。
   - 否则对全字符串 `json.loads`。
4. `_load_state/_save_state`：状态读写。
5. `_load_events/_save_events`：事件列表读写。
6. `_chapter_file`：章节号转文件名（零填充 3 位）。

## 6.3 角色文件工具（90-151）

1. `_load_all_roles`：
   - 读取 `all_roles.txt` 为 `set`。
   - 文件不存在则创建空文件并返回空集。
2. `_save_all_roles`：排序写回，保障稳定性。
3. `_role_file`：
   - 将 Windows 非法文件名字符替换为 `_`。
   - 避免角色名直接落盘失败。
4. `_load_role/_save_role`：单角色档案读写。
5. `_merge_role`：
   - `personality/traits/abilities/aliases` 去重追加。
   - `description` 取最新非空值。
   - `summary` 以全角空格拼接增量片段。
   - `events` 按 `event_id` 去重追加。

## 6.4 `Timeline` 类核心方法（156-369）

1. `__init__`：载入配置并初始化客户端。
2. `_call_llm`：
   - 统一系统指令为“严格输出 JSON，简体中文”。
   - 流式拉取内容并拼接字符串。
3. `_extract_events`：
   - 将当前 `state` 注入 `EXTRACT_EVENTS_PROMPT`。
   - 用章节号、章节文本替换模板占位符。
4. `_resolve_entity`：
   - 对新角色名与已有角色表做 LLM 消歧。
   - 防御规则：若返回未知旧名则回退 `new_name`。
5. `_update_characters_from_events`：
   - 从 `events[*].characters` 收集角色名。
   - 对每个原始名做 canonical 映射。
   - 按 canonical 归并事件后调用角色更新 Prompt。
   - 新档案直接创建；老档案用 `_merge_role` 合并。
   - 解析失败仅警告并跳过该角色，不中断整章流程。
6. `_compress_summary`：
   - 将旧状态 + 本章事件交给压缩 Prompt。
   - 输出作为新状态直接覆盖 `state.json`。
7. `process_chapter`：
   - 单章总控，串联“事件提取→角色更新→摘要压缩”。
8. `process_chapters`：区间批处理，遇缺章中止。
9. `get_events`：支持按 `chapter` 过滤。
10. `get_state`：返回当前状态快照。
11. `reset`：清空 `state.json` 和 `events.json`（不清理 `roles`）。

## 6.5 脚本入口（372-387）

当前主程序会处理第 `95~100` 回，逐章打印简要事件摘要。  
这段属于示例/调试入口，不是通用 CLI。

---

## 7. Prompt 协议与实际行为

## 7.1 `extract_events.md`

约束 LLM 输出章节事件列表，强调：

- 时间表达保留原文词。
- 时间类型标准化。
- 因果链与前情一致。
- 事件标题短且可区分。

## 7.2 `compress_summary.md`

约束将“旧摘要 + 新事件”压缩为下一轮上下文，限制整体 JSON 大小（800 字以内，优先压缩 `recent_events/open_threads`）。

## 7.3 `update_character_profile.md`

约束角色档案是“增量更新语义”：

- 基础词条只增不删。
- `summary` 只写本次新增片段。
- `events` 以 `event_id` 去重。

---

## 8. 幂等性、一致性与边界行为

## 8.1 幂等性现状

- `process_chapter` 的跳过条件依赖 `state.chapter_reached`。
- 事件写入是 `extend` 追加，未做全局去重。
- 因此在“事件已写入但状态未更新”的异常场景下，重跑同章可能重复入库。

## 8.2 一致性现状

当前无事务机制，以下写入是分步完成：

1. `events.json` 追加
2. `roles/*.json` 更新
3. `state.json` 覆盖

中途失败会产生部分提交状态（例如事件已落盘但摘要未更新）。

## 8.3 错误处理策略

- 缺章：抛 `FileNotFoundError`，批处理捕获并停止。
- 角色档案 JSON 解析失败：警告并继续下一个角色。
- 事件/摘要 JSON 解析失败：未显式捕获，会中断当前流程。
- 网络或 API 异常：未重试，直接异常上抛。

---

## 9. 使用方式

## 9.1 作为脚本运行

```bash
python timeline/timeline.py
```

按当前源码会处理 `95~100` 回并打印每章事件。

## 9.2 作为模块调用

```python
from timeline.timeline import Timeline

tl = Timeline()
tl.process_chapter(1)
tl.process_chapters(1, 10)

events_all = tl.get_events()
events_ch1 = tl.get_events(chapter=1)
state = tl.get_state()
```

## 9.3 重置数据

```python
tl.reset()
```

仅清空 `timeline/state.json` 与 `timeline/events.json`，角色档案不会被删除。

---

## 10. 已知风险与改进建议

## 10.1 主要风险

1. `chapter_reached` 完全信任 LLM 输出，若模型返回倒退值会影响跳过逻辑。
2. `events.json` 无全局去重键，重复执行同章存在重复记录风险。
3. `_parse_llm_json` 对非严格 JSON（多余文本、单引号、尾逗号）容错低。
4. `_merge_role` 假设 `events[*].event_id` 存在，异常结构可能触发 `KeyError`。
5. 无 API 重试、超时控制、限流退避。
6. `all_roles.txt` 仅记录 canonical 名称，不显式维护别名索引。

## 10.2 建议优化

1. 给事件增加 `(chapter, event_id)` 去重写入。
2. 在 `process_chapter` 使用“临时文件 + 原子替换”或事务式阶段提交。
3. 增加 JSON schema 校验，分层校验 `events/state/role`。
4. 增加 `_call_llm` 重试与超时策略。
5. 为角色消歧增加本地规则层（正则别名词典）后再调用 LLM。
6. 提供标准 CLI（如 `--start --end --resume --reset --dry-run`）。

---

## 11. 维护者速查清单

日常维护时建议优先检查：

1. `config.yaml` 的密钥是否可用。
2. Prompt 模板占位符是否与代码替换字段一致。
3. `state.json` 中 `chapter_reached` 与 `last_global_order` 是否单调增长。
4. `events.json` 是否出现重复 `event_id`。
5. `roles/all_roles.txt` 与 `roles/*.json` 是否存在明显漏项。

---

## 12. 一句话总结

`timeline/timeline.py` 是一个“以章节为批次、以 rolling summary 为记忆、以 JSON 文件为状态存储”的 LLM 叙事抽取流水线；它已具备可用的主干能力，但在幂等性、事务性与结构校验方面仍有明显工程化提升空间。
