from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .logging_utils import truncate_text

try:
  from neo4j import Driver
  from neo4j import GraphDatabase
except ImportError:  # pragma: no cover - import guard
  Driver = Any
  GraphDatabase = None


LOGGER = logging.getLogger("role_dialogue.neo4j")


@dataclass(frozen=True)
class Neo4jRoleInitConfig:
  uri: str
  user: str
  password: str
  project_id: str
  database: str | None = None
  max_events: int = 12


class Neo4jRoleInitializer:
  """Initialize role payloads and retrieve one-hop graph evidence from Neo4j."""

  def __init__(self, config: Neo4jRoleInitConfig) -> None:
    if GraphDatabase is None:
      raise RuntimeError("Missing dependency: neo4j. Run `pip install neo4j`.")
    self._config = config
    self._driver: Driver = GraphDatabase.driver(
      config.uri,
      auth=(config.user, config.password),
    )
    LOGGER.info(
      "neo4j initializer created | uri=%s | database=%s | project_id=%s | max_events=%s",
      config.uri,
      config.database or "<default>",
      config.project_id,
      config.max_events,
    )

  @classmethod
  def from_config_file(
    cls,
    config_path: str | Path,
    *,
    max_events: int = 12,
  ) -> "Neo4jRoleInitializer":
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as fh:
      payload = yaml.safe_load(fh) or {}

    neo4j_cfg = payload.get("neo4j") or {}
    config = Neo4jRoleInitConfig(
      uri=str(neo4j_cfg.get("uri") or "").strip(),
      user=str(neo4j_cfg.get("user") or "").strip(),
      password=str(neo4j_cfg.get("password") or "").strip(),
      project_id=str(payload.get("project_id") or "").strip(),
      database=str(neo4j_cfg.get("database") or "").strip() or None,
      max_events=max_events,
    )
    return cls(config)

  def close(self) -> None:
    LOGGER.info("closing neo4j driver")
    self._driver.close()

  def verify_connectivity(self) -> None:
    LOGGER.info("verifying neo4j connectivity")
    self._driver.verify_connectivity()
    LOGGER.info("neo4j connectivity verified")

  def initialize_role(
    self,
    role_seed: Mapping[str, Any] | None,
    *,
    request_id: str | None = None,
  ) -> dict[str, Any]:
    seed = dict(role_seed or {})
    title = self._normalize_text(seed.get("title") or seed.get("name"))
    if not title:
      LOGGER.warning("[%s] skip role initialization: empty title/name", request_id or "-")
      return seed

    LOGGER.info(
      "[%s] role initialization started | title=%s | seed_keys=%s",
      request_id or "-",
      title,
      sorted(seed.keys()),
    )
    matched = self.fetch_role_by_title(title, request_id=request_id)
    if matched is None:
      LOGGER.warning("[%s] role initialization missed | title=%s", request_id or "-", title)
      return seed

    merged = self._merge_role_seed(seed, matched, title)
    LOGGER.info(
      "[%s] role initialization completed | name=%s | title=%s | aliases=%s | events=%s | summary=%s",
      request_id or "-",
      merged.get("name"),
      merged.get("title"),
      len(merged.get("aliases") or []),
      len(merged.get("events") or []),
      truncate_text(merged.get("summary"), 120),
    )
    return merged

  def fetch_role_by_title(
    self,
    title: str,
    *,
    request_id: str | None = None,
  ) -> dict[str, Any] | None:
    normalized_title = self._normalize_text(title)
    if not normalized_title:
      return None

    query, params = self._build_match_query(normalized_title)
    LOGGER.info(
      "[%s] neo4j role match query | title=%s | project_id=%s",
      request_id or "-",
      normalized_title,
      params["project_id"],
    )
    LOGGER.debug("[%s] neo4j role match cypher: %s", request_id or "-", " ".join(query.split()))

    with self._session() as session:
      record = session.run(query, params).single()

    if record is None:
      LOGGER.info("[%s] neo4j role match returned no record | title=%s", request_id or "-", normalized_title)
      return None

    payload = {
      "name": self._normalize_text(record.get("name")),
      "title": self._normalize_text(record.get("title")),
      "aliases": self._normalize_list(record.get("aliases")),
      "description": self._normalize_text(record.get("description")),
      "summary": self._normalize_text(record.get("summary")),
      "personality": self._normalize_list(record.get("personality")),
      "events": self._normalize_events(record.get("events")),
    }
    LOGGER.info(
      "[%s] neo4j role match hit | name=%s | title=%s | aliases=%s | personality=%s | events=%s | summary=%s",
      request_id or "-",
      payload["name"],
      payload["title"],
      len(payload["aliases"]),
      len(payload["personality"]),
      len(payload["events"]),
      truncate_text(payload["summary"], 120),
    )
    return payload

  def retrieve_related_items(
    self,
    role_payload: Mapping[str, Any] | None,
    *,
    request_id: str | None = None,
  ) -> list[dict[str, Any]]:
    seed = dict(role_payload or {})
    title = self._normalize_text(seed.get("title") or seed.get("name"))
    if not title:
      LOGGER.warning("[%s] skip graph retrieval: empty title/name", request_id or "-")
      return []

    query, params = self._build_related_items_query(title)
    LOGGER.info("[%s] neo4j graph retrieval started | title=%s", request_id or "-", title)
    LOGGER.debug("[%s] neo4j graph retrieval cypher: %s", request_id or "-", " ".join(query.split()))

    with self._session() as session:
      records = list(session.run(query, params))

    items: list[dict[str, Any]] = []
    for record in records:
      event_id = self._normalize_text(record.get("event_id"))
      event_title = self._normalize_text(record.get("event_title")) or event_id
      if not event_id and not event_title:
        continue

      items.append(
        {
          "event_id": event_id,
          "event_title": event_title,
          "global_order": record.get("global_order"),
          "text": self._build_related_item_text(
            role_name=self._normalize_text(record.get("role_name")),
            role_title=self._normalize_text(record.get("role_title")),
            event_id=event_id,
            event_title=event_title,
            event_type=self._normalize_text(record.get("event_type")),
            location=self._normalize_text(record.get("location")),
            description=self._normalize_text(record.get("description")),
            cause=self._normalize_text(record.get("cause")),
            consequence=self._normalize_text(record.get("consequence")),
            status=self._normalize_text(record.get("status")),
            decision=self._normalize_text(record.get("decision")),
            result=self._normalize_text(record.get("result")),
          ),
        }
      )

    preview = ", ".join(
      f"{item.get('event_title') or item.get('event_id')}#{item.get('event_id')}"
      for item in items[:5]
    )
    LOGGER.info(
      "[%s] neo4j graph retrieval completed | title=%s | items=%s | preview=%s",
      request_id or "-",
      title,
      len(items),
      preview or "<empty>",
    )
    return items

  def _build_match_query(self, title: str) -> tuple[str, dict[str, Any]]:
    predicate_sql, priority_sql = self._match_sql()
    query = f"""
    MATCH (c:Character {{project_id: $project_id}})
    WHERE {predicate_sql}
    OPTIONAL MATCH (c)-[r:PARTICIPATED_IN]->(e:Event)
    WITH
      c,
      r,
      e,
      CASE {priority_sql} ELSE 99 END AS match_priority
    ORDER BY match_priority ASC, e.global_order ASC, e.event_id ASC
    WITH
      c,
      match_priority,
      collect(
        CASE
          WHEN e IS NULL THEN null
          ELSE {{
            title: coalesce(e.title, e.event_id),
            event_id: e.event_id,
            status: coalesce(r.status, ""),
            decision: coalesce(r.decision, ""),
            result: coalesce(r.result, "")
          }}
        END
      ) AS raw_events
    RETURN
      c.name AS name,
      c.title AS title,
      c.aliases AS aliases,
      c.description AS description,
      c.summary AS summary,
      c.personality AS personality,
      [item IN raw_events WHERE item IS NOT NULL][0..$max_events] AS events
    ORDER BY match_priority ASC, size(coalesce(c.summary, "")) DESC
    LIMIT 1
    """
    return query, {
      "project_id": self._config.project_id,
      "title": title,
      "max_events": self._config.max_events,
    }

  def _build_related_items_query(self, title: str) -> tuple[str, dict[str, Any]]:
    predicate_sql, priority_sql = self._match_sql()
    query = f"""
    MATCH (c:Character {{project_id: $project_id}})
    WHERE {predicate_sql}
    WITH c, CASE {priority_sql} ELSE 99 END AS match_priority
    ORDER BY match_priority ASC, size(coalesce(c.summary, "")) DESC
    LIMIT 1
    OPTIONAL MATCH (c)-[r:PARTICIPATED_IN]->(e:Event)
    RETURN
      c.name AS role_name,
      c.title AS role_title,
      e.event_id AS event_id,
      e.title AS event_title,
      e.description AS description,
      e.cause AS cause,
      e.consequence AS consequence,
      e.location AS location,
      e.event_type AS event_type,
      e.global_order AS global_order,
      r.status AS status,
      r.decision AS decision,
      r.result AS result
    ORDER BY e.global_order ASC, e.event_id ASC
    """
    return query, {
      "project_id": self._config.project_id,
      "title": title,
    }

  def _match_sql(self) -> tuple[str, str]:
    matchers = [
      ("c.title = $title", 0),
      ("c.name = $title", 1),
      ("$title IN coalesce(c.aliases, [])", 2),
    ]
    predicate_sql = " OR ".join(predicate for predicate, _ in matchers)
    priority_sql = " ".join(
      f"WHEN {predicate} THEN {priority}" for predicate, priority in matchers
    )
    return predicate_sql, priority_sql

  def _merge_role_seed(
    self,
    seed: Mapping[str, Any],
    matched: Mapping[str, Any],
    requested_title: str,
  ) -> dict[str, Any]:
    return {
      "name": matched.get("name") or seed.get("name") or requested_title,
      "title": matched.get("title") or seed.get("title") or requested_title,
      "aliases": matched.get("aliases") or self._normalize_list(seed.get("aliases")),
      "summary": matched.get("summary")
      or self._normalize_text(seed.get("summary"))
      or self._normalize_text(seed.get("description")),
      "description": matched.get("description")
      or self._normalize_text(seed.get("description")),
      "personality": matched.get("personality")
      or self._normalize_list(seed.get("personality")),
      "events": matched.get("events") or self._normalize_events(seed.get("events")),
    }

  def _session(self) -> Any:
    session_kwargs: dict[str, Any] = {}
    if self._config.database:
      session_kwargs["database"] = self._config.database
    return self._driver.session(**session_kwargs)

  @staticmethod
  def _build_related_item_text(
    *,
    role_name: str,
    role_title: str,
    event_id: str,
    event_title: str,
    event_type: str,
    location: str,
    description: str,
    cause: str,
    consequence: str,
    status: str,
    decision: str,
    result: str,
  ) -> str:
    lines = [
      "条目类型：角色相关事件",
      f"角色：{role_name or role_title or '未知角色'}",
      f"事件：{event_title or event_id or '未命名事件'}",
    ]
    if event_id:
      lines.append(f"事件ID：{event_id}")
    if event_type:
      lines.append(f"事件类型：{event_type}")
    if location:
      lines.append(f"地点：{location}")
    if description:
      lines.append(f"事件描述：{description}")
    if cause:
      lines.append(f"前因：{cause}")
    if consequence:
      lines.append(f"后果：{consequence}")
    if status:
      lines.append(f"角色状态：{status}")
    if decision:
      lines.append(f"角色决策：{decision}")
    if result:
      lines.append(f"结果：{result}")
    return "\n".join(lines)

  @staticmethod
  def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())

  @classmethod
  def _normalize_list(cls, values: Any) -> list[str]:
    if not isinstance(values, list):
      return []
    return [cls._normalize_text(value) for value in values if cls._normalize_text(value)]

  @classmethod
  def _normalize_events(cls, values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
      return []

    events: list[dict[str, str]] = []
    for item in values:
      if not isinstance(item, Mapping):
        continue
      event_id = cls._normalize_text(item.get("event_id"))
      title = cls._normalize_text(item.get("title")) or event_id
      if not event_id and not title:
        continue
      events.append(
        {
          "title": title,
          "event_id": event_id,
          "status": cls._normalize_text(item.get("status")),
          "decision": cls._normalize_text(item.get("decision")),
          "result": cls._normalize_text(item.get("result")),
        }
      )
    return events
