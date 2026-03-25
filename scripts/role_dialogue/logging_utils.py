from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def setup_logging(root_dir: Path, config: dict[str, Any]) -> tuple[logging.Logger, Path]:
  log_cfg = config.get("logging") or {}
  log_dir = root_dir / str(log_cfg.get("dir") or "logs")
  log_dir.mkdir(parents=True, exist_ok=True)

  log_file = log_dir / str(log_cfg.get("filename") or "role_dialogue_server.log")
  level_name = str(log_cfg.get("level") or "INFO").upper()
  level = getattr(logging, level_name, logging.INFO)
  max_bytes = int(log_cfg.get("max_bytes") or 5 * 1024 * 1024)
  backup_count = int(log_cfg.get("backup_count") or 5)

  logger = logging.getLogger("role_dialogue")
  logger.setLevel(level)
  logger.propagate = False
  logger.handlers.clear()

  handler = RotatingFileHandler(
    log_file,
    maxBytes=max_bytes,
    backupCount=backup_count,
    encoding="utf-8",
  )
  handler.setLevel(level)
  handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
  )
  logger.addHandler(handler)

  return logger, log_file


def truncate_text(value: Any, limit: int = 180) -> str:
  text = " ".join(str(value or "").split())
  if len(text) <= limit:
    return text
  return f"{text[:limit]}..."
