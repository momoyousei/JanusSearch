#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the capability-oriented JanusSearch architecture."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from janussearch.application import corpus
from janussearch.application.doctor import check_vectors
from janussearch.application.evaluation import evaluation_fingerprint, status
from janussearch.collectors.registry import get_collector, supported_venues
from janussearch.domain.errors import ConfigurationError
from janussearch.domain.run import ExitCode, RunStatus
from janussearch.infrastructure.fingerprints import fingerprint_payload
from janussearch.infrastructure.manifests import RunManifest
from tools.m1_pipeline import run_validate
from tools.m2_db import canonical_source_file_key


class TestRunContracts(unittest.TestCase):
    """Run manifests and fingerprints remain stable and secret-safe."""

    def test_exit_codes_are_stable(self) -> None:
        self.assertEqual(int(ExitCode.SUCCESS), 0)
        self.assertEqual(int(ExitCode.OPERATION_FAILED), 1)
        self.assertEqual(int(ExitCode.USAGE_ERROR), 2)

    def test_payload_fingerprint_is_order_independent(self) -> None:
        self.assertEqual(
            fingerprint_payload({"b": 2, "a": 1}),
            fingerprint_payload({"a": 1, "b": 2}),
        )

    def test_manifest_redacts_credentials_and_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = RunManifest(
                capability="evaluate",
                operation="online",
                scope={"suite": "online"},
                config={"embed_api_key": "secret-value", "top_k": 20},
                artifacts_root=root / "runs",
                workdir=root,
                run_id="test-run",
            )
            manifest.write()
            manifest.add_step("healthcheck", "passed")
            manifest.finish(exit_code=ExitCode.SUCCESS)

            payload = json.loads(manifest.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["config"]["embed_api_key"], "<redacted>")
            self.assertNotIn("secret-value", manifest.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], RunStatus.SUCCEEDED.value)
            self.assertEqual(payload["exit_code"], 0)


class TestCollectorRegistry(unittest.TestCase):
    """Collector routing is explicit and complete for the current corpus."""

    def test_current_venues_are_registered(self) -> None:
        expected = {
            "AAAI", "ACL", "ACMMM", "AISTATS", "CVPR", "ECCV", "ICCV", "ICDE",
            "ICLR", "ICML", "IJCAI", "KDD", "NEURIPS", "SIGIR", "TPAMI", "VLDB", "WWW",
        }
        self.assertEqual(set(supported_venues()), expected)

    def test_specialized_and_generic_commands(self) -> None:
        acl = get_collector("acl").command(
            venue="ACL", years="2024-2025", output_root=Path("snapshot")
        )
        self.assertEqual(acl[:2], ["-m", "tools.acl_collect"])
        self.assertIn("--years", acl)

        neurips = get_collector("neurips").command(
            venue="NEURIPS", years="2025", output_root=Path("snapshot")
        )
        self.assertEqual(neurips[:2], ["-m", "janussearch.collectors.virtual"])
        self.assertIn("NEURIPS-2025", neurips)

        vldb = get_collector("vldb").command(
            venue="VLDB", years="2026", output_root=Path("snapshot")
        )
        self.assertEqual(vldb[:2], ["-m", "tools.pvldb_collect"])

    def test_unsupported_venue_is_configuration_error(self) -> None:
        with self.assertRaises(ConfigurationError):
            get_collector("unknown")


class TestCatalogPathIdentity(unittest.TestCase):
    """Catalog manifests do not depend on relative versus absolute invocation."""

    def test_absolute_input_root_maps_to_canonical_source_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_root = Path(temp_dir).resolve() / "data" / "raw"
            file_path = input_root / "acl" / "2025.json"
            self.assertEqual(
                canonical_source_file_key(file_path, input_root),
                "data/raw/acl/2025.json",
            )


class TestCorpusSafety(unittest.TestCase):
    """Staging and publication do not bypass gates."""

    @staticmethod
    def _source_payload(official_count: int = 2) -> dict:
        return {
            "query": {"venue_code": "TEST", "year": 2025, "provider": "fixture"},
            "source": {"provider": "fixture"},
            "generated_at_utc": "2026-08-07T00:00:00+00:00",
            "reconciliation": {"external_title_count": official_count},
            "papers": [
                {
                    "paper_title": "One complete paper",
                    "title": "One complete paper",
                    "authors": ["Alice"],
                    "abstract": "A complete abstract.",
                    "track": "conference",
                    "track_group": "main",
                    "presentation_level": "poster",
                    "source_provider": "fixture",
                    "url": "https://example.org/paper",
                }
            ],
        }

    def test_alignment_is_warning_by_default_and_failure_in_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "TEST-25.json"
            source.write_text(json.dumps(self._source_payload()), encoding="utf-8")
            warning_report, warning_pass = run_validate(
                input_glob=str(source),
                report_path=root / "warning.json",
                stats_md_path=root / "warning.md",
                threshold_authors=90.0,
                threshold_abstract=85.0,
                enforce_official_alignment=False,
            )
            strict_report, strict_pass = run_validate(
                input_glob=str(source),
                report_path=root / "strict.json",
                stats_md_path=root / "strict.md",
                threshold_authors=90.0,
                threshold_abstract=85.0,
                enforce_official_alignment=True,
            )

            self.assertTrue(warning_pass)
            self.assertEqual(warning_report["summary"]["gate_fail_files"], 0)
            self.assertEqual(warning_report["summary"]["alignment_warning_files"], 1)
            self.assertTrue(warning_report["files"][0]["warnings"])
            self.assertFalse(strict_pass)
            self.assertEqual(strict_report["summary"]["gate_fail_files"], 1)

    def test_publish_preserves_relative_shape_and_replaces_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staging = root / "staging"
            canonical = root / "raw"
            source = staging / "test" / "2025.json"
            target = canonical / "test" / "2025.json"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_text('{"version": 2}\n', encoding="utf-8")
            target.write_text('{"version": 1}\n', encoding="utf-8")

            result = corpus.publish(staging, canonical)

            self.assertEqual(result["published_count"], 1)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["version"], 2)
            self.assertFalse(any(target.parent.glob(".*.publish.*")))

    def test_reconcile_inherits_stable_id_after_retitle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staging = root / "staging"
            canonical = root / "raw"
            output = root / "reconciled"
            old_path = canonical / "test" / "2026.json"
            new_path = staging / "test" / "2026.json"
            old_path.parent.mkdir(parents=True)
            new_path.parent.mkdir(parents=True)
            old_path.write_text(
                json.dumps(
                    {
                        "papers": [
                            {
                                "paper_id": "S2-stable",
                                "title": "Old title",
                                "authors": ["Alice"],
                                "openreview_id": "forum-1",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            new_path.write_text(
                json.dumps(
                    {
                        "papers": [
                            {
                                "paper_id": "S2-generated",
                                "title": "New title",
                                "authors": ["Alice"],
                                "openreview_id": "forum-1",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = corpus.reconcile(
                staging_root=staging,
                canonical_root=canonical,
                output_root=output,
            )

            reconciled = json.loads((output / "test" / "2026.json").read_text(encoding="utf-8"))
            self.assertEqual(reconciled["papers"][0]["paper_id"], "S2-stable")
            self.assertEqual(report["files"][0]["mapping_method_counts"], {"stable_id": 1})

    def test_reconcile_blocks_unapproved_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staging = root / "staging"
            canonical = root / "raw"
            old_path = canonical / "test" / "2026.json"
            new_path = staging / "test" / "2026.json"
            old_path.parent.mkdir(parents=True)
            new_path.parent.mkdir(parents=True)
            old_path.write_text(
                json.dumps(
                    {
                        "papers": [
                            {"paper_id": "S2-one", "title": "One", "authors": ["A"]},
                            {"paper_id": "S2-two", "title": "Two", "authors": ["B"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            new_path.write_text(
                json.dumps(
                    {"papers": [{"paper_id": "S2-one", "title": "One", "authors": ["A"]}]}
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Unknown removals blocked"):
                corpus.reconcile(
                    staging_root=staging,
                    canonical_root=canonical,
                    output_root=root / "reconciled",
                )

    def test_staging_validation_uses_source_pointer_for_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "snapshot" / "TEST-25.json"
            staging = root / "staging"
            reports = root / "reports"
            source.parent.mkdir()
            source.write_text(json.dumps(self._source_payload()), encoding="utf-8")
            corpus.prepare(
                input_glob=str(source),
                staging_root=staging,
                reports_root=reports,
                enrich=False,
                timeout=1.0,
                retries=0,
                max_records_per_file=0,
                min_interval=0.0,
                enable_arxiv_title=False,
                enable_papers_cool=False,
                papers_cool_policy="full_fields",
            )

            warning_report, warning_pass = corpus.validate(
                input_glob=str(staging / "*/*.json"),
                reports_root=reports / "warning",
                threshold_authors=90.0,
                threshold_abstract=85.0,
                strict_official_alignment=False,
            )
            strict_report, strict_pass = corpus.validate(
                input_glob=str(staging / "*/*.json"),
                reports_root=reports / "strict",
                threshold_authors=90.0,
                threshold_abstract=85.0,
                strict_official_alignment=True,
            )
            self.assertTrue(warning_pass)
            self.assertEqual(warning_report["summary"]["alignment_warning_files"], 1)
            self.assertFalse(strict_pass)
            self.assertEqual(strict_report["summary"]["alignment_fail_files"], 1)

    @patch("tools.corpus.corpus.publish")
    @patch("tools.corpus.corpus.validate", return_value=({"summary": {}}, False))
    def test_publish_cli_does_not_publish_after_failed_validation(
        self,
        _validate_mock,
        publish_mock,
    ) -> None:
        from tools import corpus as corpus_cli

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staging = root / "staging"
            canonical = root / "raw"
            staging.mkdir()
            argv = [
                "tools.corpus",
                "publish",
                "--staging-root",
                str(staging),
                "--canonical-root",
                str(canonical),
            ]
            with patch("sys.argv", argv), patch.object(
                corpus_cli,
                "RunManifest",
                side_effect=lambda **kwargs: RunManifest(
                    **kwargs,
                    artifacts_root=root / "runs",
                    workdir=root,
                    run_id="publish-failure",
                ),
            ):
                self.assertEqual(corpus_cli.main(), 1)
            publish_mock.assert_not_called()

    def test_invalid_corpus_scope_returns_usage_exit_code(self) -> None:
        from tools import corpus as corpus_cli

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            argv = ["tools.corpus", "plan", "--venue", "UNKNOWN", "--years", "2025"]
            with patch("sys.argv", argv), patch.object(
                corpus_cli,
                "RunManifest",
                side_effect=lambda **kwargs: RunManifest(
                    **kwargs,
                    artifacts_root=root / "runs",
                    workdir=root,
                    run_id="invalid-scope",
                ),
            ):
                self.assertEqual(corpus_cli.main(), 2)


class TestFreshnessAndDiagnostics(unittest.TestCase):
    """Evaluation status and doctor detect drift and corruption."""

    def test_status_rejects_changed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "papers.db"
            vectors = root / "vectors"
            topics = root / "topics.json"
            fixed = root / "fixed.yaml"
            report = root / "eval.json"
            db_path.write_bytes(b"db-v1")
            vectors.mkdir()
            (vectors / "state").write_bytes(b"vectors-v1")
            topics.write_text("{}", encoding="utf-8")
            fixed.write_text("cases: []", encoding="utf-8")
            fingerprint = evaluation_fingerprint(
                db_path=db_path,
                vectors_root=vectors,
                topics_file=topics,
                fixed_query_file=fixed,
            )
            report.write_text(
                json.dumps(
                    {
                        "summary": {
                            "overall_pass": True,
                            "generated_at_utc": "2026-08-07T00:00:00+00:00",
                            "suite": "offline",
                            "input_fingerprint": fingerprint,
                        }
                    }
                ),
                encoding="utf-8",
            )
            current, current_pass = status(
                report_path=report,
                db_path=db_path,
                vectors_root=vectors,
                topics_file=topics,
                fixed_query_file=fixed,
            )
            self.assertTrue(current_pass)
            self.assertFalse(current["stale"])

            topics.write_text('{"changed": true}', encoding="utf-8")
            stale, stale_pass = status(
                report_path=report,
                db_path=db_path,
                vectors_root=vectors,
                topics_file=topics,
                fixed_query_file=fixed,
            )
            self.assertFalse(stale_pass)
            self.assertTrue(stale["stale"])

    def test_vector_check_reports_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vectors = Path(temp_dir) / "vectors"
            vectors.mkdir()
            fake_chromadb = SimpleNamespace(
                PersistentClient=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("corrupt store"))
            )
            fake_config = SimpleNamespace(Settings=lambda **kwargs: kwargs)
            with patch.dict(
                "sys.modules",
                {"chromadb": fake_chromadb, "chromadb.config": fake_config},
            ):
                checks = list(check_vectors(vectors, "papers_v1"))
            self.assertEqual(checks[0]["status"], "error")
            self.assertIn("corrupt store", checks[0]["message"])


class TestSkillReplacement(unittest.TestCase):
    """Focused Skills replace the two discoverable legacy Skills."""

    def test_four_skills_exist_and_legacy_metadata_is_absent(self) -> None:
        root = Path(__file__).resolve().parents[1] / ".agent" / "skills"
        for name in ("janussearch", "janus-query", "janus-corpus", "janus-ops"):
            self.assertTrue((root / name / "SKILL.md").is_file())
            self.assertTrue((root / name / "agents" / "openai.yaml").is_file())
        self.assertFalse((root / "janussearch-agent" / "SKILL.md").exists())
        self.assertFalse((root / "paper-search" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
