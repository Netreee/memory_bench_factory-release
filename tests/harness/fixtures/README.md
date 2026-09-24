# 测试夹具：office 历史候选快照

`office__20260902-121616/` 是从 `agent-harnesses/input/validated/` 迁入的**唯一**一个
benchmark 快照，只用于让 `tests/harness/` 的离线回归跑在真实产物形状上
（`00_about.json` / `05_corpus.json` / `06_grounded_questions.json` 的真实 schema、
真实 session/doc/question 计数）。它由 memory-bench-factory 生成。

边界：

- 该场景闭环状态为 `UNMET`，**不是 release benchmark**，也不是正式评测输入；
- 正式实验的 benchmark 不随本仓库分发，由 experiment 的 `benchmarks[].path` 指向
  外部路径（factory 生成产物 `output/runs/<run_id>/` 或任意相对/绝对路径）；
- 迁入背景见 `DECISIONS.md` 的评测端合并决策；测试断言依赖本目录内容
  （`n_questions=40`、`n_docs=221`、`met_status` 以 `UNMET` 开头），改动它会改变回归含义。
