#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility alias for the capability implementation."""

import sys
from janussearch.application import corpus_pipeline as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
