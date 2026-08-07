#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capability CLI for vector, topic, and cache projections."""

from __future__ import annotations

import json
import logging

from janussearch.application.projections import execute
from janussearch.domain.run import ExitCode
from janussearch.infrastructure.manifests import RunManifest
from janussearch.application.projection_pipeline import build_arg_parser as build_compatible_parser

LOGGER = logging.getLogger("janussearch.projections")


def build_arg_parser():
    """Reuse stable projection arguments under the capability name."""
    parser = build_compatible_parser()
    parser.description = "Build and validate JanusSearch derived projections"
    return parser


def main() -> int:
    """Run a projection operation with recoverable in-place semantics."""
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    manifest = RunManifest(
        capability="projections",
        operation=args.command,
        scope={
            "db_path": args.db_path,
            "vectors_root": args.vectors_root,
            "collection_name": args.collection_name,
        },
        config=vars(args),
    )
    manifest.write()
    try:
        payload, passed = execute(args)
        exit_code = ExitCode.SUCCESS if passed else ExitCode.OPERATION_FAILED
        manifest.add_step(args.command, "passed" if passed else "failed")
        manifest_path = manifest.finish(exit_code=exit_code)
        print(json.dumps({"result": payload, "run_manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
        return int(exit_code)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        manifest.add_issue("projection_operation_failed", str(exc))
        manifest.finish(exit_code=ExitCode.OPERATION_FAILED)
        return int(ExitCode.OPERATION_FAILED)


if __name__ == "__main__":
    raise SystemExit(main())
