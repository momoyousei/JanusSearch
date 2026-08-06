#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run status and process exit-code contracts."""

from __future__ import annotations

from enum import Enum, IntEnum


class ExitCode(IntEnum):
    """Stable process exit codes for every capability CLI."""

    SUCCESS = 0
    OPERATION_FAILED = 1
    USAGE_ERROR = 2


class RunStatus(str, Enum):
    """Lifecycle states persisted in run manifests."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"

