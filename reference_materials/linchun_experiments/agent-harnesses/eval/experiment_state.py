"""实验路径、续跑身份和结果落盘；仅依赖标准库。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def digest(value) -> str:
    """为配置或题目生成稳定指纹。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_factory_root(explicit: str = "", *, start: Path | None = None) -> Path:
    """优先使用 CLI、环境变量，再从脚本位置及当前目录向上发现工厂。"""
    configured = explicit or os.environ.get("MEMORY_BENCH_FACTORY_ROOT", "")
    starts = [Path(configured)] if configured else [start or Path(__file__).resolve(), Path.cwd()]
    for candidate in starts:
        candidate = candidate.expanduser().resolve()
        candidates = [candidate] if configured else [candidate, *candidate.parents]
        for root in candidates:
            if (root / "eval" / "judge.py").is_file() and (root / "config.py").is_file():
                return root
    raise ValueError("找不到工厂目录，请传 --factory-root 或设置 MEMORY_BENCH_FACTORY_ROOT")


def question_hash(question: dict) -> str:
    """题面、答案、评分合同和题号均参与续跑身份。"""
    fields = ("qid", "line", "capability", "entity", "field", "question", "gt", "aux", "strict_scoring", "question_contract")
    return digest({key: question.get(key) for key in fields})


def prepare_experiment(out_dir: Path, config: dict) -> str:
    """阻止同一结果目录混入不同实验；旧版无指纹结果需另开目录。"""
    fingerprint = digest(config)
    manifest = out_dir / "experiment.json"
    results = out_dir / "results.jsonl"
    if manifest.is_file():
        previous = json.loads(manifest.read_text(encoding="utf-8"))
        if previous.get("fingerprint") != fingerprint:
            raise ValueError("输出目录已有不同配置的实验，请使用新的 --out 目录")
    elif results.is_file() and results.stat().st_size:
        raise ValueError("旧结果缺少实验指纹，不能安全续跑；请使用新的 --out 目录")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not manifest.exists():
        manifest.write_text(json.dumps({"fingerprint": fingerprint, "config": config}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return fingerprint


def load_records(path: Path, fingerprint: str) -> dict[str, dict]:
    """只读取当前实验记录，并将重试合并为每题一条。"""
    records = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            key = rec.get("_question_hash")
            if rec.get("_experiment") != fingerprint or key != question_hash(rec):
                raise ValueError("结果身份校验失败，请检查结果文件或使用新的 --out 目录")
            records[key] = rec
    return records


def is_reusable(rec: dict, judge_version: str | None = None) -> bool:
    """调用、判分失败和缺失答案均应重试，而非复用为错误答案。"""
    pred = rec.get("pred")
    grade = rec.get("judgement") or {}
    return (not rec.get("error") and not rec.get("judge_error")
            and grade.get("verdict") in {"correct", "incorrect"}
            and bool(grade.get("version")) and (judge_version is None or grade["version"] == judge_version)
            and type(grade.get("correct")) is bool and grade["correct"] is rec.get("correct")
            and grade["correct"] == (grade["verdict"] == "correct")
            and rec.get("judgeable") is True and type(rec.get("correct")) is bool
            and isinstance(pred, str) and bool(pred.strip())
            and not (pred.lstrip().startswith("[") and "ERROR" in pred))


def save_records(path: Path, records: dict[str, dict]) -> None:
    """原子替换标准结果，避免重试产生重复题影响简单题筛选。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records.values()), encoding="utf-8")
    temporary.replace(path)
