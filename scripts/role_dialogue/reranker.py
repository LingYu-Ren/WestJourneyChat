from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from .logging_utils import truncate_text


DEFAULT_RERANK_ENDPOINT = (
  "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)

LOGGER = logging.getLogger("role_dialogue.rerank")


@dataclass(frozen=True)
class RerankConfig:
  api_key: str
  endpoint: str = DEFAULT_RERANK_ENDPOINT
  model: str = "qwen3-vl-rerank"
  top_k: int = 5
  max_documents: int = 100
  timeout_seconds: int = 20


class QwenVLReranker:
  def __init__(self, config: RerankConfig) -> None:
    self._config = config
    LOGGER.info(
      "reranker configured | model=%s | endpoint=%s | top_k=%s | max_documents=%s | timeout_seconds=%s",
      config.model,
      config.endpoint,
      config.top_k,
      config.max_documents,
      config.timeout_seconds,
    )

  @classmethod
  def from_config(cls, payload: dict[str, Any]) -> "QwenVLReranker":
    qwen_cfg = payload.get("qwen") or {}
    config = RerankConfig(
      api_key=str(payload.get("api_key") or "").strip(),
      endpoint=str(qwen_cfg.get("rerank_endpoint") or DEFAULT_RERANK_ENDPOINT).strip(),
      model=str(qwen_cfg.get("rerank_model") or "qwen3-vl-rerank").strip(),
      top_k=max(int(qwen_cfg.get("graph_top_k") or 5), 1),
      max_documents=max(int(qwen_cfg.get("rerank_max_documents") or 100), 1),
      timeout_seconds=max(int(qwen_cfg.get("rerank_timeout_seconds") or 20), 1),
    )
    return cls(config)

  @property
  def top_k(self) -> int:
    return self._config.top_k

  def rerank(
    self,
    query_text: str,
    documents: list[dict[str, Any]],
    *,
    top_k: int | None = None,
    request_id: str | None = None,
  ) -> list[dict[str, Any]]:
    cleaned_docs = [doc for doc in documents if str(doc.get("text") or "").strip()]
    if not cleaned_docs:
      LOGGER.warning("[%s] rerank skipped: no documents", request_id or "-")
      return []

    wanted_top_k = max(top_k or self._config.top_k, 1)
    LOGGER.info(
      "[%s] rerank started | model=%s | candidates=%s | top_k=%s | query=%s",
      request_id or "-",
      self._config.model,
      len(cleaned_docs),
      wanted_top_k,
      truncate_text(query_text, 160),
    )

    if len(cleaned_docs) <= self._config.max_documents:
      reranked = self._rerank_once(query_text, cleaned_docs, wanted_top_k, request_id=request_id)
      self._log_results(reranked, request_id=request_id)
      return reranked

    stage_one_take = min(max(wanted_top_k * 4, wanted_top_k), self._config.max_documents)
    LOGGER.info(
      "[%s] rerank using multi-stage flow | batch_size=%s | stage_one_take=%s",
      request_id or "-",
      self._config.max_documents,
      stage_one_take,
    )

    stage_one_docs: list[dict[str, Any]] = []
    for offset in range(0, len(cleaned_docs), self._config.max_documents):
      chunk = cleaned_docs[offset : offset + self._config.max_documents]
      LOGGER.info(
        "[%s] rerank stage1 batch | offset=%s | size=%s",
        request_id or "-",
        offset,
        len(chunk),
      )
      stage_one_docs.extend(
        self._rerank_once(
          query_text,
          chunk,
          min(stage_one_take, len(chunk)),
          request_id=request_id,
        )
      )

    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in stage_one_docs:
      key = self._document_key(item)
      if key in seen_keys:
        continue
      seen_keys.add(key)
      deduped.append(item)

    LOGGER.info(
      "[%s] rerank stage1 completed | shortlisted=%s",
      request_id or "-",
      len(deduped),
    )

    final_candidates = deduped[: self._config.max_documents]
    reranked = self._rerank_once(query_text, final_candidates, wanted_top_k, request_id=request_id)
    self._log_results(reranked, request_id=request_id)
    return reranked

  def _rerank_once(
    self,
    query_text: str,
    documents: list[dict[str, Any]],
    top_k: int,
    *,
    request_id: str | None = None,
  ) -> list[dict[str, Any]]:
    payload = {
      "model": self._config.model,
      "input": {
        "query": {"text": query_text},
        "documents": [{"text": str(item.get("text") or "")} for item in documents],
      },
      "parameters": {
        "return_documents": True,
        "top_n": min(top_k, len(documents)),
      },
    }
    LOGGER.debug(
      "[%s] rerank http payload | top_n=%s | documents=%s",
      request_id or "-",
      min(top_k, len(documents)),
      len(documents),
    )
    response = self._post_json(payload, request_id=request_id)

    usage = response.get("usage") or {}
    LOGGER.info(
      "[%s] rerank http completed | returned=%s | total_tokens=%s",
      request_id or "-",
      len(((response.get("output") or {}).get("results") or [])),
      usage.get("total_tokens"),
    )

    results = ((response.get("output") or {}).get("results") or [])
    reranked: list[dict[str, Any]] = []
    for result in results:
      if not isinstance(result, dict):
        continue
      index = int(result.get("index", -1))
      if index < 0 or index >= len(documents):
        continue
      source = dict(documents[index])
      source["score"] = result.get("relevance_score")
      reranked.append(source)
    return reranked

  def _post_json(
    self,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
  ) -> dict[str, Any]:
    if not self._config.api_key:
      raise RuntimeError("Missing api_key for rerank request.")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
      self._config.endpoint,
      data=body,
      method="POST",
      headers={
        "Authorization": f"Bearer {self._config.api_key}",
        "Content-Type": "application/json",
      },
    )
    try:
      with request.urlopen(http_request, timeout=self._config.timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
      detail = exc.read().decode("utf-8", errors="ignore")
      LOGGER.error("[%s] rerank http error | code=%s | detail=%s", request_id or "-", exc.code, detail)
      raise RuntimeError(f"Rerank request failed: HTTP {exc.code} {detail}") from exc
    except error.URLError as exc:
      LOGGER.error("[%s] rerank network error | reason=%s", request_id or "-", exc.reason)
      raise RuntimeError(f"Rerank request failed: {exc.reason}") from exc

    data = json.loads(raw or "{}")
    if data.get("code") or (data.get("message") and "output" not in data):
      LOGGER.error("[%s] rerank api returned failure | payload=%s", request_id or "-", data)
      raise RuntimeError(f"Rerank request failed: {data}")
    return data

  def _log_results(self, reranked: list[dict[str, Any]], *, request_id: str | None = None) -> None:
    preview = "; ".join(
      f"{truncate_text(item.get('event_title') or item.get('event_id'), 40)}"
      f"(score={item.get('score')})"
      for item in reranked[:5]
    )
    LOGGER.info(
      "[%s] rerank completed | selected=%s | preview=%s",
      request_id or "-",
      len(reranked),
      preview or "<empty>",
    )

  @staticmethod
  def _document_key(document: dict[str, Any]) -> str:
    return "|".join(
      [
        str(document.get("event_id") or ""),
        str(document.get("text") or ""),
      ]
    )
