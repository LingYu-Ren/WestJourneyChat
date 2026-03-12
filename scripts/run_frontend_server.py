#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from openai import OpenAI

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_CHAT_MODEL = "qwen3.5-flash"


def _read_config() -> dict[str, Any]:
  with CONFIG_PATH.open("r", encoding="utf-8") as fh:
    return yaml.safe_load(fh) or {}


def _content_text(content: Any) -> str:
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    parts: list[str] = []
    for item in content:
      if isinstance(item, dict) and item.get("type") == "text":
        parts.append(str(item.get("text", "")))
    return "".join(parts)
  return str(content or "")


def _build_role_profile(role_payload: dict[str, Any]) -> str:
  name = str(role_payload.get("name") or "未知角色")
  aliases = role_payload.get("aliases") or []
  summary = str(role_payload.get("summary") or "暂无摘要")
  personality = role_payload.get("personality") or []
  events = role_payload.get("events") or []

  aliases_text = "、".join(str(a) for a in aliases[:8]) if aliases else "暂无"
  traits_text = "、".join(str(t) for t in personality[:12]) if personality else "暂无"
  event_titles = []
  for event in events[:12]:
    if isinstance(event, dict):
      event_titles.append(str(event.get("title") or event.get("event_id") or "未命名事件"))
    else:
      event_titles.append(str(event))
  events_text = "；".join(event_titles) if event_titles else "暂无"

  return (
    f"角色名：{name}\n"
    f"别名：{aliases_text}\n"
    f"性格标签：{traits_text}\n"
    f"角色摘要：{summary}\n"
    f"关键事件：{events_text}\n"
  )


def _generate_reply(
  client: OpenAI,
  model: str,
  message: str,
  role_payload: dict[str, Any],
  history: list[dict[str, str]],
) -> str:
  role_profile = _build_role_profile(role_payload)
  name = str(role_payload.get("name") or "该角色")

  messages: list[dict[str, str]] = [
    {
      "role": "system",
      "content": (
        "你是一个角色扮演助手。"
        "你必须基于给定角色资料进行第一人称回复，保持简洁自然，不要脱离角色设定。"
        "如果用户问题与资料无关，也要以角色口吻给出合理回应，不要编造过度细节。"
      ),
    },
    {
      "role": "system",
      "content": f"当前扮演角色资料如下：\n{role_profile}",
    },
  ]

  for item in history[-12:]:
    role = item.get("role", "")
    content = item.get("content", "")
    if role in {"user", "assistant"} and content:
      messages.append({"role": role, "content": content})

  messages.append({"role": "user", "content": message})

  completion = client.chat.completions.create(
    model=model,
    messages=messages,
    temperature=0.7,
    max_tokens=480,
  )
  raw = _content_text(completion.choices[0].message.content).strip()
  if raw:
    return raw
  return f"{name}：我在听，你可以再说得具体一些。"


class FrontendHandler(SimpleHTTPRequestHandler):
  client: OpenAI | None = None
  model: str = DEFAULT_CHAT_MODEL

  def end_headers(self) -> None:
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")
    self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    super().end_headers()

  def do_OPTIONS(self) -> None:
    self.send_response(204)
    self.end_headers()

  def do_POST(self) -> None:
    path = urlparse(self.path).path
    if path != "/api/role-dialogue":
      self.send_error(404, "Not Found")
      return

    if self.client is None:
      self._write_json(500, {"error": "LLM client is not initialized."})
      return

    try:
      length = int(self.headers.get("Content-Length", "0"))
      body = self.rfile.read(length).decode("utf-8")
      payload = json.loads(body or "{}")

      message = str(payload.get("message") or "").strip()
      role_payload = payload.get("role") or {}
      history = payload.get("history") or []
      if not isinstance(history, list):
        history = []
      if not isinstance(role_payload, dict):
        role_payload = {}
      if not message:
        self._write_json(400, {"error": "message is required"})
        return

      reply = _generate_reply(
        client=self.client,
        model=self.model,
        message=message,
        role_payload=role_payload,
        history=history,
      )
      self._write_json(200, {"reply": reply, "model": self.model})
    except Exception as exc:  # noqa: BLE001
      self._write_json(500, {"error": f"LLM request failed: {exc}"})

  def _write_json(self, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


def main() -> None:
  parser = argparse.ArgumentParser(description="Serve frontend and proxy role dialogue to Qwen.")
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=8000)
  args = parser.parse_args()

  config = _read_config()
  api_key = str(config.get("api_key") or "").strip()
  if not api_key:
    raise RuntimeError("Missing api_key in config.yaml")

  qwen_cfg = config.get("qwen") or {}
  base_url = str(qwen_cfg.get("base_url") or DEFAULT_BASE_URL)
  model = str(qwen_cfg.get("chat_model") or DEFAULT_CHAT_MODEL)

  FrontendHandler.client = OpenAI(api_key=api_key, base_url=base_url)
  FrontendHandler.model = model

  handler = partial(FrontendHandler, directory=str(ROOT_DIR))
  with ThreadingHTTPServer((args.host, args.port), handler) as server:
    print(f"Frontend server listening at http://{args.host}:{args.port}/frontend/")
    print(f"Role dialogue model: {model}")
    server.serve_forever()


if __name__ == "__main__":
  main()
