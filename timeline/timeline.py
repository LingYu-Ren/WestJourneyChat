import json
import re
import yaml
from pathlib import Path
from openai import OpenAI

ROOT = Path(__file__).parent.parent
TIMELINE_DIR = Path(__file__).parent
CHAPTERS_DIR = ROOT / "data" / "journey_to_the_west" / "chapters"

EVENTS_FILE = TIMELINE_DIR / "events.json"
STATE_FILE = TIMELINE_DIR / "state.json"

EXTRACT_EVENTS_PROMPT = (ROOT / "prompts" / "extract_events.md").read_text(encoding="utf-8")
COMPRESS_SUMMARY_PROMPT = (ROOT / "prompts" / "compress_summary.md").read_text(encoding="utf-8")
UPDATE_CHARACTER_PROMPT = (ROOT / "prompts" / "update_character_profile.md").read_text(encoding="utf-8")

ROLES_DIR = ROOT / "roles"
ALL_ROLES_FILE = ROLES_DIR / "all_roles.txt"

_RESOLVE_PROMPT = """你是实体消歧专家。判断【新角色名】是否与【已有角色列表】中的某个角色指代同一人。

规则：
- 若是同一角色（别名、简称、全称、带地名前缀等），返回已有角色的名称
- 若不是同一角色，返回新角色名本身
- 只返回一个名称，不要任何解释

新角色名：{new_name}
已有角色列表：{existing_list}
"""

_INITIAL_STATE = {
    "chapter_reached": 0,
    "time_anchor": "故事尚未开始",
    "elapsed_summary": "无",
    "location": "未知",
    "active_characters": [],
    "recent_events": [],
    "open_threads": [],
    "last_global_order": 0,
}


# ── 工具函数 ──────────────────────────────────────────────

def _load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_client(config: dict) -> OpenAI:
    return OpenAI(
        api_key=config["api_key"],
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )


def _parse_llm_json(text: str) -> dict:
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = match.group(1) if match else text.strip()
    return json.loads(raw)


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return dict(_INITIAL_STATE)


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_events() -> list:
    if EVENTS_FILE.exists():
        return json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    return []


def _save_events(events: list) -> None:
    EVENTS_FILE.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")


def _chapter_file(chapter_num: int) -> Path:
    return CHAPTERS_DIR / f"chapter_{chapter_num:03d}.txt"


# ── 角色文件工具 ──────────────────────────────────────────

def _load_all_roles() -> set:
    if not ALL_ROLES_FILE.exists():
        ALL_ROLES_FILE.write_text("", encoding="utf-8")
        return set()
    text = ALL_ROLES_FILE.read_text(encoding="utf-8").strip()
    return set(line.strip() for line in text.splitlines() if line.strip())


def _save_all_roles(roles: set) -> None:
    ALL_ROLES_FILE.write_text("\n".join(sorted(roles)), encoding="utf-8")


def _role_file(name: str) -> Path:
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return ROLES_DIR / f"{safe_name}.json"


def _load_role(name: str) -> dict | None:
    path = _role_file(name)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_role(profile: dict) -> None:
    ROLES_DIR.mkdir(exist_ok=True)
    name = profile["name"]
    _role_file(name).write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _merge_role(existing: dict, updated: dict) -> dict:
    """将 LLM 返回的更新档案合并到已有档案，列表字段去重追加，events 按 event_id 去重。"""
    for field in ("personality", "traits", "abilities", "aliases"):
        seen = set(existing.get(field, []))
        merged = list(existing.get(field, []))
        for item in updated.get(field, []):
            if item not in seen:
                seen.add(item)
                merged.append(item)
        existing[field] = merged

    # description 取最新值
    if updated.get("description"):
        existing["description"] = updated["description"]

    # summary 增量追加：LLM 只返回本次新增片段，拼接到已有内容后
    new_summary_fragment = updated.get("summary", "").strip()
    if new_summary_fragment:
        existing_summary = existing.get("summary", "").strip()
        existing["summary"] = (existing_summary + "　" + new_summary_fragment).strip() if existing_summary else new_summary_fragment

    # events 按 event_id 去重追加
    existing_event_ids = {e["event_id"] for e in existing.get("events", [])}
    existing.setdefault("events", [])
    for ev in updated.get("events", []):
        if ev["event_id"] not in existing_event_ids:
            existing["events"].append(ev)
            existing_event_ids.add(ev["event_id"])

    return existing


# ── Timeline 类 ───────────────────────────────────────────

class Timeline:
    """
    滚动上下文时间线提取器。

    核心思路（参考 RecurrentGPT / MemGPT）：
    - 每处理一章，将本章文本 + 压缩后的前情摘要一起传给 LLM
    - LLM 在有上下文的情况下提取带时序的事件节点
    - 处理完毕后，将摘要压缩更新，供下一章使用
    - 所有事件追加写入 events.json，状态持久化至 state.json
    """

    def __init__(self):
        config = _load_config()
        self.client = _get_client(config)

    def _call_llm(self, prompt: str, temperature: float = 0) -> str:
        stream = self.client.chat.completions.create(
            model="qwen-plus",
            messages=[
                {
                    "role": "system",
                    "content": "你是专业的文学分析助手，严格按照要求输出 JSON，使用简体中文。",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            stream=True,
        )
        chunks = []
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                chunks.append(delta)
        return "".join(chunks).strip()

    def _extract_events(self, chapter_num: int, text: str, state: dict) -> dict:
        """调用 LLM 从章节文本中提取事件，rolling summary 作为上下文。"""
        prompt = (
            EXTRACT_EVENTS_PROMPT
            .replace("{{ROLLING_SUMMARY}}", json.dumps(state, ensure_ascii=False))
            .replace("{{CHAPTER_NUM}}", str(chapter_num))
            .replace("{{CHAPTER_NUM_PADDED}}", f"{chapter_num:03d}")
            .replace("{{TEXT}}", text)
        )
        raw = self._call_llm(prompt)
        return _parse_llm_json(raw)

    def _resolve_entity(self, new_name: str, existing_roles: set) -> str:
        """LLM 实体消歧：将 new_name 对齐到已有角色的 canonical name。"""
        if not existing_roles:
            return new_name
        prompt = _RESOLVE_PROMPT.format(
            new_name=new_name,
            existing_list="、".join(sorted(existing_roles)),
        )
        result = self._call_llm(prompt)
        # 防止 LLM 幻觉返回不存在的名字
        if result != new_name and result not in existing_roles:
            return new_name
        return result

    def _update_characters_from_events(self, events: list[dict]) -> None:
        """
        从本章事件中提取所有涉及角色，更新或创建对应的角色档案文件。
        流程：
          1. 收集本章所有事件中出现的角色名
          2. LLM 实体消歧 → canonical name
          3. 读取已有档案（若有）
          4. 筛选该角色参与的事件
          5. LLM 更新档案（含事件决策记录）
          6. 合并写入 roles/{name}.json
        """
        existing_roles = _load_all_roles()

        # 收集本章所有角色名（去重）
        raw_names: set[str] = set()
        for ev in events:
            for ch in ev.get("characters", []):
                name = ch.strip()
                if name:
                    raw_names.add(name)

        if not raw_names:
            return

        # 实体消歧：映射 raw_name → canonical_name
        name_map: dict[str, str] = {}
        for raw in raw_names:
            canonical = self._resolve_entity(raw, existing_roles)
            name_map[raw] = canonical
            if canonical != raw:
                print(f"  [对齐] {raw} → {canonical}")

        # 按 canonical name 分组处理
        canonical_names = set(name_map.values())
        for canonical in canonical_names:
            # 找出该角色参与的事件（raw name 可能多个）
            raw_aliases = {r for r, c in name_map.items() if c == canonical}
            char_events = [
                ev for ev in events
                if raw_aliases & set(ev.get("characters", []))
            ]

            existing_profile = _load_role(canonical)
            existing_json = json.dumps(existing_profile or {}, ensure_ascii=False)

            prompt = (
                UPDATE_CHARACTER_PROMPT
                .replace("{{CHARACTER_NAME}}", canonical)
                .replace("{{EXISTING_PROFILE}}", existing_json)
                .replace("{{CHARACTER_EVENTS}}", json.dumps(char_events, ensure_ascii=False))
            )
            raw_result = self._call_llm(prompt)
            try:
                updated_profile = _parse_llm_json(raw_result)
            except Exception:
                print(f"  [警告] 角色档案解析失败：{canonical}")
                continue

            if existing_profile:
                final_profile = _merge_role(existing_profile, updated_profile)
                print(f"  [更新角色] {canonical}")
            else:
                final_profile = updated_profile
                existing_roles.add(canonical)
                print(f"  [新建角色] {canonical}")

            _save_role(final_profile)

        _save_all_roles(existing_roles)

    def _compress_summary(self, chapter_num: int, current_state: dict, new_events: list) -> dict:
        """调用 LLM 将当前 state + 新事件压缩为新的 rolling summary。"""
        prompt = (
            COMPRESS_SUMMARY_PROMPT
            .replace("{{CURRENT_SUMMARY}}", json.dumps(current_state, ensure_ascii=False))
            .replace("{{CHAPTER_NUM}}", str(chapter_num))
            .replace("{{NEW_EVENTS}}", json.dumps(new_events, ensure_ascii=False))
        )
        raw = self._call_llm(prompt)
        return _parse_llm_json(raw)

    def process_chapter(self, chapter_num: int) -> list[dict]:
        """
        处理单个章节：
        1. 读取章节文本
        2. 加载当前 rolling state
        3. 提取事件（带上下文）
        4. 追加写入 events.json
        5. 压缩更新 state.json
        返回本章提取的事件列表。
        """
        chapter_file = _chapter_file(chapter_num)
        if not chapter_file.exists():
            raise FileNotFoundError(f"章节文件不存在: {chapter_file}")

        text = chapter_file.read_text(encoding="utf-8")
        state = _load_state()

        if state["chapter_reached"] >= chapter_num:
            print(f"[跳过] 第 {chapter_num} 回已处理")
            return []

        print(f"[提取事件] 第 {chapter_num} 回 ...")
        extracted = self._extract_events(chapter_num, text, state)
        events: list[dict] = extracted.get("events", [])

        # 追加写入全局事件列表
        all_events = _load_events()
        all_events.extend(events)
        _save_events(all_events)
        print(f"  → 提取事件 {len(events)} 条（全局共 {len(all_events)} 条）")

        # 更新角色档案
        print(f"[更新角色] 第 {chapter_num} 回 ...")
        self._update_characters_from_events(events)

        # 压缩更新 rolling summary
        print(f"[更新摘要] 第 {chapter_num} 回 ...")
        new_state = self._compress_summary(chapter_num, state, events)
        _save_state(new_state)

        # 打印时间信息
        print(f"  → 时间锚点：{new_state.get('time_anchor')}")
        print(f"  → 叙事时间累计：{new_state.get('elapsed_summary')}")
        print(f"  → 当前地点：{new_state.get('location')}")

        return events

    def process_chapters(self, start: int = 1, end: int = 100) -> None:
        """批量处理多个章节。"""
        for ch in range(start, end + 1):
            try:
                self.process_chapter(ch)
            except FileNotFoundError as e:
                print(f"[警告] {e}")
                break

    def get_events(self, chapter: int | None = None) -> list[dict]:
        """返回所有事件，可按章节过滤。"""
        events = _load_events()
        if chapter is not None:
            events = [e for e in events if e.get("chapter") == chapter]
        return events

    def get_state(self) -> dict:
        """返回当前 rolling summary 状态。"""
        return _load_state()

    def reset(self) -> None:
        """清空时间线数据，重新开始。"""
        _save_state(dict(_INITIAL_STATE))
        _save_events([])
        print("[重置] 时间线数据已清空")


if __name__ == "__main__":
    tl = Timeline()

    # 处理前两章作为测试
    for ch in range(1, 101):
        events = tl.process_chapter(ch)
        print(f"\n第 {ch} 回事件列表：")
        for e in events:
            t = e.get("time", {})
            print(
                f"  [{e['event_id']}] "
                f"T={t.get('expression') or t.get('type')} | "
                f"{e.get('location')} | "
                f"{e.get('description')}"
            )
        print()
