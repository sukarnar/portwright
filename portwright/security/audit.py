"""Structured audit log (JSON lines). Never logs request bodies or credentials."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


class AuditLogger:
    """One logger per audit file, so several projects in one process (Studio, tests,
    embedded apps) never write into each other's logs."""

    def __init__(self, path: str | None):
        self.path = str(Path(path).resolve()) if path else None
        if not self.path:
            self.logger = None
            return
        name = "portwright.audit." + hashlib.sha1(self.path.encode()).hexdigest()[:12]
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(self.path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

    def record(self, **event) -> None:
        if self.logger is None:
            return
        event.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.logger.info(json.dumps(event, default=str))
        for h in self.logger.handlers:
            h.flush()
