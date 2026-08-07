#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility alias for the package collector."""

import sys
from janussearch.collectors import acl as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
