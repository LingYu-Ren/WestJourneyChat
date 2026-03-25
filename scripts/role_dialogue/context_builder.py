from __future__ import annotations

from typing import Any


class DialogueContextBuilder:
  def __init__(self, *, history_limit: int = 12, evidence_top_k: int = 5) -> None:
    self.history_limit = history_limit
    self.evidence_top_k = evidence_top_k

  def build_rerank_query(self, role_payload: dict[str, Any], user_message: str) -> str:
    name = str(role_payload.get("name") or role_payload.get("title") or "该角色").strip()
    summary = str(role_payload.get("summary") or "").strip()
    summary_text = summary[:240] if summary else "暂无摘要"
    return (
      f"角色：{name}\n"
      f"角色摘要：{summary_text}\n"
      f"用户问题：{user_message.strip()}\n"
      "请根据该角色回答用户问题时的相关性，对候选知识图谱证据排序。"
    )

  def build_messages(
    self,
    *,
    role_payload: dict[str, Any],
    history: list[dict[str, str]],
    user_message: str,
    evidence_items: list[dict[str, Any]],
  ) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
      {
        "role": "system",
        "content": (
          "你是一个角色扮演助手。"
          "你必须基于给定角色资料，以第一人称、符合角色设定的口吻自然回答。"
          "如果提供了知识图谱证据，优先依据这些证据回答具体事实。"
          "如果证据不足，可以做符合角色设定的合理回应，但不要编造过度细节。"
        ),
      },
      {
        "role": "system",
        "content": f"当前扮演角色资料如下：\n{self._build_role_profile(role_payload)}",
      },
      {
        "role": "system",
        "content": (
          "以下是与当前问题最相关的知识图谱证据：\n"
          f"{self._build_evidence_block(evidence_items[: self.evidence_top_k])}"
        ),
      },
    ]

    for item in history[-self.history_limit :]:
      role = item.get("role", "")
      content = item.get("content", "")
      if role in {"user", "assistant"} and content:
        messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_message})
    return messages

  def _build_role_profile(self, role_payload: dict[str, Any]) -> str:
    name = str(role_payload.get("name") or "未知角色").strip()
    aliases = role_payload.get("aliases") or []
    summary = str(role_payload.get("summary") or "暂无摘要").strip()
    personality = role_payload.get("personality") or []
    events = role_payload.get("events") or []

    aliases_text = "、".join(str(a).strip() for a in aliases[:8] if str(a).strip()) or "暂无"
    traits_text = "、".join(str(t).strip() for t in personality[:12] if str(t).strip()) or "暂无"

    event_titles: list[str] = []
    for event in events[:12]:
      if isinstance(event, dict):
        title = str(event.get("title") or event.get("event_id") or "未命名事件").strip()
      else:
        title = str(event).strip()
      if title:
        event_titles.append(title)
    events_text = "；".join(event_titles) if event_titles else "暂无"

    return (
      f"角色名：{name}\n"
      f"别名：{aliases_text}\n"
      f"性格标签：{traits_text}\n"
      f"角色摘要：{summary}\n"
      f"关键事件：{events_text}\n"
    )

  def _build_evidence_block(self, evidence_items: list[dict[str, Any]]) -> str:
    if not evidence_items:
      return "暂无可用图谱证据。"

    blocks: list[str] = []
    for index, item in enumerate(evidence_items, 1):
      text = str(item.get("text") or "").strip()
      score = item.get("score")
      header = f"证据{index}" if score is None else f"证据{index}（相关度 {float(score):.4f}）"
      blocks.append(f"{header}\n{text}")
    return "\n\n".join(blocks)
