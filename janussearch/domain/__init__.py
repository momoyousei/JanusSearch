#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Domain models shared by JanusSearch capabilities."""

from janussearch.domain.errors import ConfigurationError
from janussearch.domain.run import ExitCode, RunStatus

__all__ = ["ConfigurationError", "ExitCode", "RunStatus"]
