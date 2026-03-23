# ChatPlatformXiyOU

![西游记项目主视觉](pics/top.png)

基于《西游记》文本的叙事时间线抽取与角色图谱构建项目。  
项目当前主线能力：

1. 从维基文库下载并清洗章节文本。
2. 用 Qwen 对章节做事件抽取与滚动摘要更新。
3. 基于事件更新角色档案（含实体消歧）。

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

​			timeline/timeline.py` 当前主要使用 `api_key`。

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

4. 角色合并结果不符合预期  
   目前按“主名/别名交叉引用 + 简体归一”做并查集合并；若别名不完整，会影响聚合效果。

5. LLM 输出 JSON 解析失败  
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

<video src="./bandicam%202026-03-12%2020-52-10-472.mp4" controls width="960"></video>
If the embedded player does not load in your Markdown viewer, open the file directly:
[bandicam 2026-03-12 20-52-10-472.mp4](./bandicam%202026-03-12%2020-52-10-472.mp4)