"""用少量新题真实作答，验证评测结果到简单题筛选的完整链路。

python tools/run_filter_smoke.py --run output/runs/<run> --out output/filter_smoke/<eval>
复用 linchun 的 MemoryStore，不依赖 Agent CLI、向量模型或 Agents SDK。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_harness_module(name: str):
    """从独立参考副本加载纯数据模块，避免两个 eval 包相互遮蔽。"""
    path = ROOT / "reference_materials/linchun_experiments/agent-harnesses/eval" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"filter_smoke_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def keyword_docs(question: str, docs: list, limit: int = 3) -> list:
    """只从题面提取检索词，不读取 gold、实体标签或证据标注。"""
    chunks = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", question.lower())
    terms = {chunk for chunk in chunks if len(chunk) > 1}
    terms.update(chunk[i:i + 2] for chunk in chunks for i in range(len(chunk) - 1))
    scored = [(sum(len(term) for term in terms if term in doc.content.lower()), i, doc)
              for i, doc in enumerate(docs)]
    return [doc for score, _, doc in sorted(scored, key=lambda row: (-row[0], row[1]))[:limit] if score > 0]


def generate_questions(source: Path, limit: int) -> Path:
    """固定已有世界与语料，真实重跑良定义检查、出题和接地，隔离筛题验证范围。"""
    from pipeline.factory import ART, ANSWER_PROTOCOL, stage_well_posed, stage_questions, stage_grounding
    from pipeline.run import Run, new_run_id, _run_stage, _finalize_run
    run = Run("agent", new_run_id("agent") + "_filter_smoke", tag="frozen-world-filter-smoke",
              config_meta={"smoke": True, "generation_scope": "new_questions_on_frozen_world",
                           "source": str(source.resolve()), "question_limit": limit})
    names = ["00_input.json", "01_whitepaper.json", "02_world.json", "05_corpus.json"]
    if (source / "00_about.json").is_file():
        names.append("00_about.json")
    for name in names:
        shutil.copyfile(source / name, run.dir / name)
    if "00_about.json" not in names:
        run.write("00_about.json", {"answer_protocol": ANSWER_PROTOCOL})
    orders = json.loads((source / "03_orders.json").read_text(encoding="utf-8"))
    orders = [q for q in orders if q.get("capability") != "L10_admission"]
    orders.sort(key=lambda q: q.get("capability") != "IE")
    run.write(ART["orders"], orders[:limit])
    run.write("smoke_provenance.json", {
        "scope": "reused_world_and_corpus_fresh_question_generation",
        "source": str(source.resolve()),
        "protocol_source": "source_run" if "00_about.json" in names else "pipeline.factory.ANSWER_PROTOCOL",
        "source_hashes": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                          for name in [*names, "03_orders.json"]}})
    _run_stage(run, "well_posed", stage_well_posed, "03_well_posed_report.json")
    _run_stage(run, "questions", stage_questions, ART["questions"])
    _run_stage(run, "grounding", stage_grounding, ART["grounding"])
    _finalize_run(run, ["well_posed", "questions", "grounding"])
    return run.dir


def main():
    """真实调用两个轻量基线，保留原始结果并验证三种筛选参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", type=Path, help="已有的新生成题库")
    source.add_argument("--frozen-source", type=Path, help="复用此目录的世界和语料，先真实生成少量新题")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--top-k", type=int, default=3, help="关键词基线读取的文档上限")
    parser.add_argument("--model", help="本次生成和作答模型；默认沿用项目配置")
    parser.add_argument("--allow-unverified", action="store_true", help="明确把重新裁剪的烟测题库作为研究输入，不能产生正式成绩")
    parser.add_argument("--judge-mode", choices=["deterministic", "llm"], default="deterministic",
                        help="默认用项目评分器的确定性分支，节省判分调用；llm 启用原有语义判分")
    args = parser.parse_args()
    if args.limit < 1 or args.workers < 1 or args.top_k < 1:
        parser.error("limit、workers 和 top-k 必须为正数")
    if not args.allow_unverified:
        parser.error("烟测会生成未取得发布资格的子集；须显式 --allow-unverified，并将结果仅作研究验证")

    import config
    if args.model:
        config.MODEL = args.model
    if args.frozen_source:
        args.run = generate_questions(args.frozen_source, args.limit)
    from eval.judge import is_judgeable, judge_record
    from eval.grading import JUDGE_VERSION
    from pipeline.quality import require_release
    from eval.question_filter import export_filtered_benchmark, filter_questions, question_key
    artifacts = load_harness_module("artifacts")
    memory = load_harness_module("memory_store")
    corpus, questions, about = artifacts.load_run(args.run)
    release = require_release(args.run / "06_grounded_questions.json", allow_unverified=args.allow_unverified,
                              corpus_path=args.run / "05_corpus.json")
    # 明确按能力和原顺序选取少量可判分题，降低初次验证成本。
    candidates = [q for q in questions if is_judgeable(q) and q.get("capability") != "L10_admission"]
    candidates.sort(key=lambda q: q.get("capability") != "IE")
    selected = candidates[:args.limit]
    if not selected:
        raise ValueError("生成结果中没有可用于冒烟的题")
    args.out.mkdir(parents=True, exist_ok=False)
    bench = args.out / "06_grounded_questions.json"
    bench.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
    for name in ("05_corpus.json", "00_about.json"):
        shutil.copyfile(args.run / name, args.out / name)
    store = memory.MemoryStore(artifacts.sessions_from_corpus(corpus), args.out / "memory_files")
    protocol = artifacts.render_protocol(about)
    model = args.model or config.MODEL
    systems = ["fullcontext", f"keyword_top{args.top_k}"]
    results = {system: [] for system in systems}

    def solve(system, q):
        """记录真实答案、检索范围和当前判分器结果，失败保持显式错误。"""
        t0 = time.monotonic()
        docs = store.docs if system == "fullcontext" else keyword_docs(q["question"], store.docs, limit=args.top_k)
        context = "\n\n".join(doc.labeled for doc in docs)
        record = {**q, "system": system, "model": model, "judgeable": True,
                  "judge_mode": args.judge_mode,
                  "retrieved_doc_ids": [doc.doc_id for doc in docs],
                  "context_chars": len(context), "pred": "", "correct": None}
        try:
            record["pred"] = config.chat([
                {"role": "system", "content": "你是记忆问答助手。仅根据提供的资料回答，只给简短最终答案。资料不足时明确拒答。\n" + protocol},
                {"role": "user", "content": f"【资料】\n{context}\n\n【问题】{q['question']}"}],
                model=model, temperature=0, max_tokens=4096)
            record["judgement"] = judge_record(q, record["pred"], use_llm=args.judge_mode == "llm")
            record["correct"] = record["judgement"]["correct"]
            if record["judgement"]["verdict"] == "error":
                record["judge_error"] = record["judgement"]["reason"]
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        record["elapsed_s"] = round(time.monotonic() - t0, 3)
        return record

    print(f"[smoke] {len(selected)} questions x {len(systems)} systems; model={model}", flush=True)
    with (args.out / "attempts.jsonl").open("w", encoding="utf-8") as log, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(solve, system, q): system for system in systems for q in selected}
        for future in as_completed(futures):
            rec = future.result()
            results[rec["system"]].append(rec)
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            print(f"[{rec['system']}] correct={rec['correct']} elapsed={rec['elapsed_s']}s error={rec.get('error')}", flush=True)
    order = {question_key(q): i for i, q in enumerate(selected)}
    for records in results.values():
        records.sort(key=lambda row: order[question_key(row)])
    payload = {"bench": str(bench.resolve()), "source_run": str(args.run.resolve()),
               "release": release, "result_scope": "research_only", "judge_version": JUDGE_VERSION,
               "model": model, "systems": systems, "judge_mode": args.judge_mode,
               "results": {s: {"records": rows} for s, rows in results.items()},
               "source_hashes": {name: hashlib.sha256((args.run / name).read_bytes()).hexdigest()
                                 for name in ("06_grounded_questions.json", "05_corpus.json", "00_about.json")}}
    path = args.out / "results.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 从逐题真实答案独立计算预期剔除集合，再核对正式导出的题库。
    easy_indices = {i for i in range(len(selected)) if all(
        results[s][i]["correct"] is True and not results[s][i].get("error") for s in systems)}
    checks = {}
    for name, ratio in (("drop_all", 0.0), ("keep_half", 0.5), ("keep_all", 1.0)):
        report = export_filtered_benchmark(bench, results, args.out / name,
            keep_easy_ratio=ratio, seed=7, result_paths=[path], allow_unverified=args.allow_unverified)
        kept = json.loads((args.out / name / "06_grounded_questions.json").read_text(encoding="utf-8"))
        expected_n = len(selected) - len(easy_indices) + int(len(easy_indices) * ratio)
        assert len(kept) == expected_n, (name, len(kept), expected_n)
        assert kept == [q for q in selected if q in kept]
        assert all(q in kept for i, q in enumerate(selected) if i not in easy_indices)
        assert sum(selected[i] in kept for i in easy_indices) == int(len(easy_indices) * ratio)
        if ratio == 0:
            assert kept == [q for i, q in enumerate(selected) if i not in easy_indices]
        elif ratio == 1:
            assert kept == selected
        checks[name] = report["counts"]
    # 独立故障注入只在内存副本上执行；真实结果文件保持原样。
    if easy_indices:
        i = min(easy_indices)
        faulty = {s: list(rows) for s, rows in results.items()}
        faulty[systems[0]] = [r for j, r in enumerate(faulty[systems[0]]) if j != i]
        kept, _ = filter_questions(selected, faulty)
        assert selected[i] in kept
        checks["missing_result_retained"] = True
        for field in ("error", "judge_error"):
            faulty = {s: list(rows) for s, rows in results.items()}
            faulty[systems[0]][i] = {**faulty[systems[0]][i], field: "injected smoke failure"}
            kept, _ = filter_questions(selected, faulty)
            assert selected[i] in kept
            checks[f"{field}_retained"] = True
    errors = sum(bool(r.get("error") or r.get("judge_error")) for rows in results.values() for r in rows)
    checks.update({"actual_all_correct": len(easy_indices),
                   "errors": errors, "answer_call_attempts": len(selected) * len(systems),
                   "judge_mode": args.judge_mode, "removal_exercised": bool(easy_indices),
                   "status": ("passed_with_eval_errors" if errors else "passed") if easy_indices else "inconclusive_no_all_correct"})
    (args.out / "validation.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False), flush=True)
    return 0 if easy_indices else 2


if __name__ == "__main__":
    raise SystemExit(main())
