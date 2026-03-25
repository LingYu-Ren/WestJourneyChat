#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import yaml
from openai import OpenAI

from role_dialogue import DialogueContextBuilder, Neo4jRoleInitializer, QwenVLReranker
from role_dialogue.logging_utils import setup_logging, truncate_text

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_CHAT_MODEL = "qwen3.5-flash"
LOGGER = logging.getLogger("role_dialogue.server")


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


def _generate_reply(
  *,
  client: OpenAI,
  model: str,
  message: str,
  role_payload: dict[str, Any],
  history: list[dict[str, str]],
  context_builder: DialogueContextBuilder,
  evidence_items: list[dict[str, Any]],
  request_id: str,
) -> str:
  role_name = str(role_payload.get("name") or "该角色")
  messages = context_builder.build_messages(
    role_payload=role_payload,
    history=history,
    user_message=message,
    evidence_items=evidence_items,
  )

  LOGGER.info(
    "[%s] llm generation started | model=%s | role=%s | history=%s | evidence=%s | message=%s",
    request_id,
    model,
    role_name,
    len(history),
    len(evidence_items),
    truncate_text(message, 160),
  )
  started_at = perf_counter()
  completion = client.chat.completions.create(
    model=model,
    messages=messages,
    temperature=0.7,
    max_tokens=480,
  )
  elapsed_ms = int((perf_counter() - started_at) * 1000)

  usage = getattr(completion, "usage", None)
  raw = _content_text(completion.choices[0].message.content).strip()
  LOGGER.info(
    "[%s] llm generation completed | duration_ms=%s | prompt_tokens=%s | completion_tokens=%s | reply=%s",
    request_id,
    elapsed_ms,
    getattr(usage, "prompt_tokens", None),
    getattr(usage, "completion_tokens", None),
    truncate_text(raw, 180),
  )
  if raw:
    return raw

  fallback = f"{role_name}：我在听，你可以再说得具体一些。"
  LOGGER.warning("[%s] llm returned empty content, using fallback reply", request_id)
  return fallback


class FrontendHandler(SimpleHTTPRequestHandler):
  client: OpenAI | None = None
  model: str = DEFAULT_CHAT_MODEL
  role_initializer: Neo4jRoleInitializer | None = None
  reranker: QwenVLReranker | None = None
  context_builder: DialogueContextBuilder | None = None

  def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
    LOGGER.info("http access | client=%s | " + format, self.address_string(), *args)

  def end_headers(self) -> None:
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")
    self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    super().end_headers()

  def do_OPTIONS(self) -> None:
    self.send_response(204)
    self.end_headers()

  def do_POST(self) -> None:
    request_id = uuid4().hex[:8]
    path = urlparse(self.path).path
    if path != "/api/role-dialogue":
      LOGGER.warning("[%s] unexpected POST path: %s", request_id, path)
      self.send_error(404, "Not Found")
      return

    if self.client is None:
      LOGGER.error("[%s] llm client is not initialized", request_id)
      self._write_json(500, {"error": "LLM client is not initialized."})
      return

    request_started_at = perf_counter()
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

      LOGGER.info(
        "[%s] request received | client=%s | role_seed=%s | history=%s | message=%s",
        request_id,
        self.client_address[0],
        {k: role_payload.get(k) for k in ("name", "title")},
        len(history),
        truncate_text(message, 160),
      )

      if not message:
        LOGGER.warning("[%s] request rejected: message is required", request_id)
        self._write_json(400, {"error": "message is required"})
        return

      if self.role_initializer is not None:
        try:
          role_payload = self.role_initializer.initialize_role(role_payload, request_id=request_id)
        except Exception:
          LOGGER.exception("[%s] role initialization fallback", request_id)

      evidence_items: list[dict[str, Any]] = []
      if (
        self.role_initializer is not None
        and self.reranker is not None
        and self.context_builder is not None
      ):
        try:
          related_items = self.role_initializer.retrieve_related_items(
            role_payload,
            request_id=request_id,
          )
          rerank_query = self.context_builder.build_rerank_query(role_payload, message)
          LOGGER.info(
            "[%s] graph evidence ranking | candidates=%s | query=%s",
            request_id,
            len(related_items),
            truncate_text(rerank_query, 180),
          )
          evidence_items = self.reranker.rerank(
            rerank_query,
            related_items,
            top_k=self.context_builder.evidence_top_k,
            request_id=request_id,
          )
        except Exception:
          LOGGER.exception("[%s] graph evidence fallback", request_id)
      else:
        LOGGER.warning(
          "[%s] graph retrieval disabled | initializer=%s | reranker=%s | context_builder=%s",
          request_id,
          self.role_initializer is not None,
          self.reranker is not None,
          self.context_builder is not None,
        )

      reply = _generate_reply(
        client=self.client,
        model=self.model,
        message=message,
        role_payload=role_payload,
        history=history,
        context_builder=self.context_builder or DialogueContextBuilder(),
        evidence_items=evidence_items,
        request_id=request_id,
      )
      elapsed_ms = int((perf_counter() - request_started_at) * 1000)
      LOGGER.info(
        "[%s] request completed | duration_ms=%s | evidence=%s | reply_length=%s",
        request_id,
        elapsed_ms,
        len(evidence_items),
        len(reply),
      )
      self._write_json(200, {"reply": reply, "model": self.model, "request_id": request_id})
    except Exception:
      LOGGER.exception("[%s] request failed", request_id)
      self._write_json(
        500,
        {
          "error": "LLM request failed. Check server log for details.",
          "request_id": request_id,
        },
      )

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
  root_logger, log_file = setup_logging(ROOT_DIR, config)
  root_logger.info("server bootstrap started")

  api_key = str(config.get("api_key") or "").strip()
  if not api_key:
    raise RuntimeError("Missing api_key in config.yaml")

  qwen_cfg = config.get("qwen") or {}
  base_url = str(qwen_cfg.get("base_url") or DEFAULT_BASE_URL)
  model = str(qwen_cfg.get("chat_model") or DEFAULT_CHAT_MODEL)

  FrontendHandler.client = OpenAI(api_key=api_key, base_url=base_url)
  FrontendHandler.model = model
  FrontendHandler.reranker = QwenVLReranker.from_config(config)
  FrontendHandler.context_builder = DialogueContextBuilder(
    history_limit=max(int(qwen_cfg.get("history_limit") or 12), 1),
    evidence_top_k=FrontendHandler.reranker.top_k,
  )

  try:
    FrontendHandler.role_initializer = Neo4jRoleInitializer.from_config_file(CONFIG_PATH)
    FrontendHandler.role_initializer.verify_connectivity()
  except Exception:
    FrontendHandler.role_initializer = None
    LOGGER.exception("role initializer disabled")

  handler = partial(FrontendHandler, directory=str(ROOT_DIR))
  LOGGER.info(
    "frontend server starting | host=%s | port=%s | model=%s | log_file=%s",
    args.host,
    args.port,
    model,
    log_file,
  )
  print(f"Frontend server listening at http://{args.host}:{args.port}/frontend/")
  print(f"Role dialogue model: {model}")
  print(f"Log file: {log_file}")

  try:
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
      server.serve_forever()
  finally:
    if FrontendHandler.role_initializer is not None:
      FrontendHandler.role_initializer.close()
    LOGGER.info("server shutdown completed")


if __name__ == "__main__":
  main()
