#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auditable run manifests shared by capability workflows."""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from janussearch.domain.run import ExitCode, RunStatus
from janussearch.infrastructure.fingerprints import fingerprint_payload

LOGGER = logging.getLogger(__name__)
SENSITIVE_MARKERS = ("api_key", "token", "secret", "password", "credential")


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any, key: str = "") -> Any:
    """Remove secret values before they reach a persisted manifest."""
    if any(marker in key.lower() for marker in SENSITIVE_MARKERS):
        return "<redacted>" if value else None
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def git_revision(workdir: Path | None = None) -> str | None:
    """Read the current Git revision without failing non-Git executions."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


@dataclass
class RunManifest:
    """Mutable run record persisted after every lifecycle transition."""

    capability: str
    operation: str
    scope: Mapping[str, Any]
    config: Mapping[str, Any]
    artifacts_root: Path = Path("artifacts/runs")
    workdir: Path = Path(".")
    run_id: str = field(default_factory=lambda: f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}")
    started_at_utc: str = field(default_factory=utc_now_iso)
    finished_at_utc: str | None = None
    status: RunStatus = RunStatus.RUNNING
    exit_code: int | None = None
    steps: list[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    issues: list[Dict[str, Any]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        """Return the canonical manifest path."""
        return self.artifacts_root / self.run_id / "manifest.json"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the manifest using stable, redacted fields."""
        safe_config = _redact(dict(self.config))
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "capability": self.capability,
            "operation": self.operation,
            "scope": _redact(dict(self.scope)),
            "git_revision": git_revision(self.workdir),
            "config_fingerprint": fingerprint_payload(safe_config),
            "config": safe_config,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "steps": self.steps,
            "metrics": self.metrics,
            "issues": self.issues,
            "artifacts": self.artifacts,
        }

    def write(self) -> Path:
        """Persist the current state atomically."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def add_step(
        self,
        name: str,
        status: str,
        *,
        metrics: Mapping[str, Any] | None = None,
        artifacts: list[Path | str] | None = None,
    ) -> None:
        """Record one workflow step and immediately checkpoint it."""
        artifact_strings = [str(item) for item in artifacts or []]
        self.steps.append(
            {
                "name": name,
                "status": status,
                "recorded_at_utc": utc_now_iso(),
                "metrics": dict(metrics or {}),
                "artifacts": artifact_strings,
            }
        )
        self.artifacts.extend(item for item in artifact_strings if item not in self.artifacts)
        self.write()

    def add_issue(self, code: str, message: str, *, severity: str = "error") -> None:
        """Record an explicit warning or error."""
        self.issues.append({"code": code, "message": message, "severity": severity})
        self.write()

    def finish(
        self,
        *,
        exit_code: ExitCode | int,
        warnings: bool = False,
        metrics: Mapping[str, Any] | None = None,
    ) -> Path:
        """Finalize and persist the manifest."""
        self.exit_code = int(exit_code)
        self.finished_at_utc = utc_now_iso()
        self.metrics.update(dict(metrics or {}))
        if self.exit_code != int(ExitCode.SUCCESS):
            self.status = RunStatus.FAILED
        elif warnings or any(item.get("severity") == "warning" for item in self.issues):
            self.status = RunStatus.SUCCEEDED_WITH_WARNINGS
        else:
            self.status = RunStatus.SUCCEEDED
        return self.write()

