#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Central environment configuration for OpenAI-compatible services."""

from __future__ import annotations

import os


def env_text(name: str, default: str) -> str:
    """Return one stripped environment value or its deterministic default."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def embed_base_url(default: str) -> str:
    """Resolve the embedding endpoint."""
    return env_text("JANUS_EMBED_BASE_URL", default)


def embed_model(default: str) -> str:
    """Resolve the embedding model."""
    return env_text("JANUS_EMBED_MODEL", default)


def llm_base_url(default: str) -> str:
    """Resolve the LLM endpoint."""
    return env_text("JANUS_LLM_BASE_URL", default)


def llm_model(default: str) -> str:
    """Resolve the LLM model."""
    return env_text("JANUS_LLM_MODEL", default)
