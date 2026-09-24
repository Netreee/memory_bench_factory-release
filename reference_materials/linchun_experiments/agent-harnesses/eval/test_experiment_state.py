"""路径、续跑隔离及筛题接口的离线回归；不读取密钥、不调用模型。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

FACTORY = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(FACTORY))
from eval.grading import JUDGE_VERSION

from experiment_state import find_factory_root, is_reusable, load_records, prepare_experiment, question_hash, save_records
import run_eval


QUESTION = {"qid": "q1", "line": "L1_fact", "capability": "IE", "entity": "甲", "field": "颜色", "question": "甲是什么颜色？", "gt": "红色", "aux": {}, "strict_scoring": {"mode": "exact"}}


def grade(verdict, path="fixture", reason="offline fixture"):
    return {"version": JUDGE_VERSION, "verdict": verdict, "correct": {"correct": True, "incorrect": False}.get(verdict),
            "path": path, "reason": reason}


class ExperimentStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_factory_discovery_and_explicit_override(self):
        factory = self.root / "factory"
        (factory / "eval").mkdir(parents=True)
        (factory / "eval" / "judge.py").write_text("", encoding="utf-8")
        (factory / "config.py").write_text("", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(find_factory_root(start=factory / "nested" / "script.py"), factory)
            self.assertEqual(find_factory_root(str(factory)), factory)
        with patch.dict(os.environ, {"MEMORY_BENCH_FACTORY_ROOT": str(factory)}, clear=True):
            self.assertEqual(find_factory_root(), factory)
            with self.assertRaises(ValueError):
                find_factory_root(str(self.root / "missing"))

    def test_question_contract_changes_invalidate_resume(self):
        original = question_hash(QUESTION)
        self.assertEqual(original, question_hash(dict(reversed(list(QUESTION.items())))))
        for field in ("question", "gt", "aux", "strict_scoring", "qid", "question_contract"):
            with self.subTest(field=field):
                self.assertNotEqual(original, question_hash({**QUESTION, field: "changed"}))

    def test_configuration_and_legacy_results_are_isolated(self):
        out = self.root / "run"
        first = prepare_experiment(out, {"model": "a", "inputs_hash": "one"})
        self.assertEqual(first, prepare_experiment(out, {"model": "a", "inputs_hash": "one"}))
        for changed in ({"model": "b", "inputs_hash": "one"}, {"model": "a", "inputs_hash": "two"}):
            with self.assertRaises(ValueError):
                prepare_experiment(out, changed)
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "results.jsonl").write_text('{"key":"old"}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            prepare_experiment(legacy, {})

    def test_failures_retry_and_success_is_reusable(self):
        base = {"pred": "红色", "correct": False, "judgeable": True, "judgement": grade("incorrect")}
        self.assertTrue(is_reusable(base))
        for change in ({"error": "timeout"}, {"judge_error": "timeout"}, {"correct": None}, {"correct": "true"}, {"pred": ""}, {"pred": "[SOLVE_ERROR] timeout"}, {"judgeable": False}, {"judgement": grade("error")}, {"judgement": None}):
            with self.subTest(change=change):
                self.assertFalse(is_reusable({**base, **change}))

    def test_result_identity_validation_and_deduplication(self):
        path = self.root / "results.jsonl"
        rec = {**QUESTION, "_question_hash": question_hash(QUESTION), "_experiment": "fp", "correct": False}
        save_records(path, {rec["_question_hash"]: rec})
        rec = {**rec, "correct": True}
        save_records(path, {rec["_question_hash"]: rec})
        rows = load_records(path, "fp")
        self.assertEqual(len(rows), 1)
        self.assertTrue(next(iter(rows.values()))["correct"])
        with self.assertRaises(ValueError):
            load_records(path, "other")
        rec["gt"] = "绿"
        save_records(path, {rec["_question_hash"]: rec})
        with self.assertRaises(ValueError):
            load_records(path, "fp")

    def test_help_without_sdk_or_factory_import(self):
        result = subprocess.run([sys.executable, "-X", "utf8", str(Path(run_eval.__file__)), "--help"], capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--factory-root", result.stdout)

    def test_runner_resume_retry_and_question_filter_compatibility(self):
        factory = self.root / "factory"
        (factory / "eval").mkdir(parents=True)
        (factory / "eval" / "judge.py").write_text("# test stub\n", encoding="utf-8")
        (factory / "eval" / "grading.py").write_text("# test stub\n", encoding="utf-8")
        (factory / "config.py").write_text("# test stub\n", encoding="utf-8")
        run = self.root / "input"
        run.mkdir()
        corpus = {"sessions": [{"session_id": 1, "date": "2026-09-16", "docs": [{"doc_id": "d1", "content": "甲是红色。", "type": "记录"}]}]}
        for name, value in (("05_corpus.json", corpus), ("06_grounded_questions.json", [QUESTION]), ("00_about.json", {})):
            (run / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        fake_cli = types.ModuleType("harness_cli")
        fake_cli.apply_cli_env = lambda: None
        fake_cli.ensure_workspace_instructions = lambda _: None
        calls = []

        def answer(*args, **kwargs):
            calls.append(1)
            return {"pred": "[SOLVE_ERROR] timeout" if len(calls) == 1 else "红色", "error": "timeout" if len(calls) == 1 else None, "elapsed_s": 0}

        fake_cli.answer_cli = answer
        fake_judge = types.ModuleType("eval.judge")
        fake_judge.classify_refusal = lambda *args: None
        fake_judge.gold_display = lambda q: q["gt"]
        fake_judge.is_judgeable = lambda q: True
        fake_judge.judge_record = lambda q, pred, **kwargs: grade("correct" if pred == q["gt"] else "incorrect")
        fake_judge.judgement = grade
        fake_judge.literal_match = lambda p, gs: p in gs
        fake_judge.judge_spec = lambda q: ("exact", None, None)
        out = self.root / "results"
        argv = ["run_eval.py", "--factory-root", str(factory), "--run", str(run), "--harness", "codex", "--model", "offline-test", "--out", str(out), "--allow-unverified"]
        with patch.dict(sys.modules, {"harness_cli": fake_cli, "eval.judge": fake_judge}), patch.object(run_eval, "_load_env"), patch.object(run_eval, "_ensure_experiment_env"), patch.object(run_eval, "attach_cost"), patch.object(run_eval, "summarize_costs", return_value={}), patch.object(sys, "argv", argv), patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                self.assertEqual(run_eval.main(), 0)
        self.assertEqual(len(calls), 2)
        rows = [json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qid"], QUESTION["qid"])
        self.assertEqual(rows[0]["strict_scoring"], QUESTION["strict_scoring"])
        self.assertTrue(rows[0]["correct"])
        self.assertEqual(len((out / "attempts.jsonl").read_text(encoding="utf-8").splitlines()), 2)
        local_factory = find_factory_root(start=Path(__file__).resolve())
        spec = importlib.util.spec_from_file_location("offline_question_filter", local_factory / "eval" / "question_filter.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        kept, report = module.filter_questions([QUESTION], {"one": rows, "two": rows})
        self.assertEqual(kept, [])
        self.assertEqual(report["counts"]["removed_easy"], 1)

    def test_real_judge_failure_retries_only_grading(self):
        """Run the real scoring implementation behind a fake solver transport."""
        factory = self.root / "factory"
        (factory / "eval").mkdir(parents=True)
        for name in ("eval/judge.py", "eval/grading.py", "config.py"):
            (factory / name).write_text("# isolated fixture\n", encoding="utf-8")
        q = {**QUESTION, "capability": "KU", "strict_scoring": None,
             "question_contract": {"version": 1, "answer_kind": "value", "allowed_aliases": [],
                                   "scoring_scope": "primary_answer", "abstention_kind": None}}
        run = self.root / "input"
        run.mkdir()
        for name, payload in (("05_corpus.json", {"sessions": []}), ("06_grounded_questions.json", [q]), ("00_about.json", {})):
            (run / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        fake_config = types.ModuleType("config")
        fake_config.MODEL = "offline-test"
        from unittest.mock import Mock
        fake_config.chat_json = Mock(side_effect=[TimeoutError("offline timeout"), {"correct": False, "reason": "different final value"}])
        spec = importlib.util.spec_from_file_location("harness_real_judge", FACTORY / "eval/judge.py")
        real_judge = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"config": fake_config}):
            spec.loader.exec_module(real_judge)
        fake_cli = types.ModuleType("harness_cli")
        fake_cli.apply_cli_env = lambda: None
        fake_cli.ensure_workspace_instructions = lambda _: None
        fake_cli.answer_cli = Mock(return_value={"pred": "a paraphrased answer", "elapsed_s": 0})
        out = self.root / "results"
        argv = ["run_eval.py", "--factory-root", str(factory), "--run", str(run), "--harness", "codex",
                "--model", "offline-test", "--out", str(out), "--allow-unverified"]
        with patch.dict(sys.modules, {"harness_cli": fake_cli, "eval.judge": real_judge}), \
             patch.object(run_eval, "_load_env"), patch.object(run_eval, "_ensure_experiment_env"), \
             patch.object(run_eval, "attach_cost"), patch.object(run_eval, "summarize_costs", return_value={}), \
             patch.object(sys, "argv", argv), patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            run_eval.main()
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["aggregate"]["overall"]["n"], 0)
            self.assertEqual(summary["aggregate"]["n_errors"], 1)
            self.assertTrue(summary["meta"]["release"]["override"])
            run_eval.main()
            run_eval.main()
        self.assertEqual(fake_cli.answer_cli.call_count, 1)
        self.assertEqual(fake_config.chat_json.call_count, 2)
        rec = json.loads((out / "results.jsonl").read_text(encoding="utf-8").strip())
        self.assertIs(rec["correct"], False)
        self.assertTrue(rec["_prediction_resumed"])
        self.assertEqual(rec["judgement"]["version"], JUDGE_VERSION)
        self.assertTrue(is_reusable(rec, JUDGE_VERSION))


if __name__ == "__main__":
    unittest.main()
