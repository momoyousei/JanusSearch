#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Typed application errors used to preserve CLI exit-code semantics."""


class ConfigurationError(ValueError):
    """User-supplied scope or configuration is invalid."""

