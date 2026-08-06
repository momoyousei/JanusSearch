#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility shim for the relocated generic conference collector."""

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from janussearch.collectors.generic import main


if __name__ == "__main__":
    raise SystemExit(main())
