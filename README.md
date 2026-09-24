# memory_bench_factory

从结构化 seed 生成长期记忆 benchmark。一个 run 依次产生领域白皮书、可执行世界、公开信息安排、问题、跨期语料和逐题质量结论。世界与语料提供可追溯事实；LLM Agent 负责生成、阅读和语义审查；程序负责阶段边界、身份绑定、断点恢复和结果汇总。

发布仓库提供命令行生成、四选手评测、筛选和导出。Node.js 用于运行原生评测选手；前端展示与演示部署工具已移除。

## 最终流水线

1. `seed → input`：校验并冻结 `--seed-pack` 指定的输入 JSON。
2. `whitepaper`：生成世界蓝图、证据渠道和产线能力映射，并检查业务可执行性。
3. `world`：分步建立实体、关系、事件和时间线；每个小批次落盘，可从检查点继续。
4. `disclosure`：确定信息何时、通过什么途径公开，并完成世界级语义审阅。
5. `orders / well_posed`：为可支持的能力线产生候选任务，先剔除定义不清的任务。
6. `questions`：生成题目和标准答案，对题面语义逐题检查。
7. `corpus`：按期渲染信号文档与干扰文档，并检查正文能否支撑声明。
8. `grounding`：逐题阅读相关语料，给出 `released`、`rejected` 或 `pending_review`；单题失败不终止其他题。
9. `quality`：只核对逐题状态分区和产物散列，生成 `07_release.json`。它不重新判断题目语义，也不会因为题量不足或其他题被淘汰而否决已经通过的题。
10. `calibration`：四个原生选手对质量审查通过的题作答，复用 `agent_harnesses` 的执行器和判分器。明确值采用确定性判分，需要理解自然语言的回答调用配置的 LLM 裁判。评分处理在评测器内部完成。
11. `selection`：删除四个选手全部答对的题，保留至少一个选手答错的题，导出标准发布包。四票必须都有效；超时、缺测或判分失败记为未完成，保存候选并在续跑时补齐。

当前 seed 流程主要覆盖 L1–L8；L9、L10 需要补充相应的材料构造后再批量启用。

## 安装

使用 Python 3.10 以上。完整四选手发布流程还需要 Node.js 22.19.0 以上及 npm。
在仓库根目录执行以下对应系统的命令。最小依赖已包含生成、原生选手评测和发布所需的 Python 包；
`requirements.txt` 用于额外的记忆系统集成。

Windows PowerShell：

```powershell
python -m venv venv
.\venv\Scripts\python.exe -X utf8 -m pip install -r requirements-minimal.txt
Copy-Item .env.example .env
Copy-Item configs/env/secrets.env.example configs/env/secrets.env
```

Linux/macOS：

```bash
python3 -m venv venv
./venv/bin/python -X utf8 -m pip install -r requirements-minimal.txt
cp .env.example .env
cp configs/env/secrets.env.example configs/env/secrets.env
```

在 `.env` 中填写生成与裁判使用的 OpenAI 兼容接口、密钥和模型名；
在 `configs/env/secrets.env` 中填写四选手的 `GPT_API_KEY`、`GPT_BASE_URL`、
`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`。示例值需要替换，所选接口须支持
`examples/release_four.json` 中的模型和协议。真实配置文件和运行输出均被 Git 忽略。

普通生成调用可设置 `LLM_REASONING_EFFORT=low`（如 `glm-5.3-flash`）；仅接受 `llm_transport.RUN_MODEL_CAPABILITIES` 声明的模型与强度，保留原 token 和超时参数，逐调用显式 transport 优先。

安装仓库固定版本的两个原生 CLI（两种系统使用同一 npm 命令）：

```text
node --version
npm install --global @openai/codex@0.153.2 @deepseek-ai/dsh@0.1.2-rc.1
codex --version
dsh --version
```

运行器默认从当前进程的 `PATH` 查找 `codex` 和 `dsh`。如果安装目录未在 `PATH` 中，
在 `configs/env/secrets.env` 中填写 `CODEX_BIN`、`DSH_BIN` 的绝对路径。
Windows 可用 `Get-Command codex.cmd,dsh.cmd` 找到 npm 启动器，填写类似
`C:/Users/your-name/AppData/Roaming/npm/codex.cmd` 的路径；Linux/macOS 可用
`command -v codex` 和 `command -v dsh`。路径使用 `/`，无需额外添加引号。
DSH 的固定 profile 由运行器配置，详见 [configs/dsh/README.md](configs/dsh/README.md)。

## Seed 校验与运行

通过 `--seed-pack <路径>` 提供自己的 seed JSON。输入描述场景、任务机制和生成约束；
格式见 [seed schema](schemas/seed_pack_v2.schema.json) 和 [转换指南](skills/realfiles-to-seedjson/SEEDJSON_GUIDE.md)。
本地输入可以放在已被 Git 忽略的 `seeds/` 目录，也可以放在仓库外。

离线校验指定的输入文件：

```powershell
.\venv\Scripts\python.exe -X utf8 tools/validate_seed_packs.py path/to/seed.json --require-generation-ready
```

完整发布流程：原有生成 → 四选手评测 → 删除全员答对题 → 标准导出。

```powershell
.\venv\Scripts\python.exe -X utf8 -m pipeline.factory --seed-pack path/to/seed.json --min-questions 200 --target-mchars 1 --haystack-ratio 9 --semantic-workers 4 --release
```

Linux/macOS 将解释器替换为 `./venv/bin/python`。以下命令均从仓库根目录运行。

`--release` 使用 [examples/release_four.json](examples/release_four.json)：

| 选手 | 原生运行器 | 接口协议 |
| --- | --- | --- |
| gpt-5.6-sol | Codex | responses |
| deepseek-v4.1-flash | DSH | openai-completions |
| gpt-5.3-codex | Codex | responses |
| deepseek-v4-flash-0731 | DSH | openai-responses |

新作答需要安装原生 Codex / DSH，并按 `configs/env/secrets.env.example` 配置 GPT / DEEPSEEK
接口；DSH 安装见 `configs/dsh/README.md`。生成与裁判接口使用 `.env`。
默认裁判 `glm-5.3-flash`；4题并行、2选手并行、每题600秒、裁判最多2000次物理调用。
裁判额度跨恢复累计，原生选手内部调用由各 CLI 管理。完整发布启动前先检查 CLI、接口配置和裁判配置；
缺少本地配置时在生成前退出。远程连通性、模型访问权限仍须实测。`--only input` 等生成分步命令可独立运行。
这些配置可以通过 `--calibration-config <json>` 显式替换；四选手发布模式固定删除全部共同答对题。
当前原生评分支持既有值／关系／时间等题，`L3_process_trace` 的过程语义评分尚未接入，
该模式在新作答前会明确报错；本发布配置沿用默认关闭的过程题开关。
省略 `--release` 和 `--calibration-config` 可单独运行原有生成部分。

已有完整 run 可以直接补做生成末尾两个阶段：

```powershell
.\venv\Scripts\python.exe -X utf8 -m pipeline.factory --run <run_id> --release --from quality
```

这里的 `quality` 只从已有逐题结果刷新轻量汇总，兼容旧版汇总文件；不会重跑世界、语料或逐题审阅。
当前汇总已经有效时可以直接 `--from calibration`。
校准复用逐题作答与判分缓存；修改裁判后可以重新判分并保留原作答。
补做未完成校准时使用 `--run <run_id> --from calibration`，已完成的作答和有效判分继续复用。
`--to quality` 可以主动停在校准之前。
题量目标衡量筛选前的合格题供给；筛选会减少交付题数，不触发新一轮生成来补足容易题。

### 语料规模与草堆

`--target-mchars 1` 表示正文总量目标为 **100 万字符**；旧参数名 `--target-mtokens` 仍可使用，
其历史实现也按字符计数。这里不宣称任何特定模型的 tokenizer token 数。
`--haystack-ratio 9` 表示草堆字符数至少为其余正文的 9 倍，对应约 90% 的草堆占比；新运行默认 9。
白皮书继续安排草堆主题、文体与每期篇数，语料阶段在清理无效文档后计算实际欠额，仅追加缺少的草堆。
总量和比例共同约束补量，每批最多 8 篇、每批保存，并设有限调用轮数；接口失败保留已完成文档。
实际总字符、草堆字符、占比与欠额写入 `05_corpus_scale.json`。规模不足记录 warning，逐题质量标准沿用原流程。

已有本版本运行调高总量或比例时，从 `--run <run_id> --from corpus` 继续，复用已验收正文、补齐差额。
旧运行未显式设置比例时沿用原行为。扩容改变了评测输入，后续校准会重新评测；同学原有成绩只能用于对应的原语料。
补量执行异常时保留独立 checkpoint，并用 warning 收口；需要补完时执行 `--run <run_id> --force --from corpus`。
普通续跑沿用本次已完成的有限补量结果，避免接口持续失败时自动反复调用。

### 复用已有四选手成绩

复制 `examples/release_four.json`，添加下面两个字段。路径相对配置文件解析；
选手的模型、协议需与原 `run_plan.json` 一致。

```json
{
  "source_benchmark": "path/to/original-standard-benchmark",
  "result_dirs": {
    "codex-gpt56-sol": "path/to/first/run",
    "dsh-deepseek-v41-flash": "path/to/second/run",
    "codex-gpt53-codex": "path/to/third/run",
    "dsh-deepseek-v4-flash-0731": "path/to/fourth/run"
  }
}
```

每个目录包含 `run_plan.json`、`results.jsonl`、`judged.jsonl`。执行
`--run <run_id> --calibration-config <import.json> --from quality` 即可直接筛选和导出，
无新增模型调用。导入会核对实际语料、协议、题面、答案和最新作答，保留原判分版本；
源标准包可包含额外题，只有当前质量合格子集进入筛选。

查看参数和已有运行：

```powershell
.\venv\Scripts\python.exe -X utf8 -m pipeline.factory --help
.\venv\Scripts\python.exe -X utf8 -m pipeline.factory --list-runs
```

中断后使用同一个 run 继续，已完成阶段和逐题检查点会被复用：

```powershell
.\venv\Scripts\python.exe -X utf8 -m pipeline.factory --run <run_id>
```

批量运行入口为 `tools/run_original_bc_batch.py`，须用 `--seeds <路径> [更多路径]` 指定输入；小规模端到端检查入口为 `tools/run_original_bc_smoke.py`。两个入口都调用同一条生产流水线。

## 主要产物

运行目录位于 `output/runs/<run_id>/`：

| 文件 | 内容 |
| --- | --- |
| `00_seed_pack.json` | 冻结后的 seed |
| `01_whitepaper.json` | 世界蓝图、质量约定和产线映射 |
| `02_world.json` | 实例化世界与时间线 |
| `02_disclosure.json` | 信息公开安排 |
| `04_questions.json` | 全部候选题与答案 |
| `05_corpus.json` | 分期语料 |
| `05_corpus_scale.json` | 真实字符数、草堆占比与规模欠额 |
| `06_semantic_review.json` | 逐题 Agent 审阅证据 |
| `06_grounding_report.json` | 通过、淘汰、待审和范围排除的分区 |
| `06_grounded_questions.json` | 当前可直接用于评测的题目子集 |
| `07_release.json` | 上述分区与文件身份的轻量汇总 |
| `08_calibration.json` | 生成期试答与判分状态，指向 `calibration/` 下的逐题结果 |
| `09_selection.json` | 全员答对剔除数、保留数、未决数、release_ready 与最终标准包路径 |
| `09_selected_questions.json` | 筛选后的题目子集 |
| `delivery/<attempt>/benchmark/` | 世界、完整语料、协议及筛选后的题目/答案，兼容现有标准包读取器 |

原生成的全部候选、质量状态、世界与语料原样保存。发布包只包含质量合格且经过四选手筛选的题。
`release_ready=true` 表示筛选完成且导出了非空包；评测未完成时只保留候选与进度。

## 目录

- `pipeline/`：唯一生产流水线及 L1–L10 能力线。
- `eval/`：用发布子集评测记忆系统。
- `agent_harnesses/`：Native Agent Track 与 Memory System Track 的活动评测控制面。
- `tools/`：校验、批量运行、恢复、监控和导出工具。
- `tests/`：离线回归与故障注入。
- `skills/realfiles-to-seedjson/`：把真实资料转换成 seed JSON 的操作规范。

## 最小验证

历史前端测试文件继续保留，在当前命令行发布版中明确跳过；核心流水线和评测测试照常执行。

```bash
./venv/Scripts/python -m compileall -q pipeline eval tools tests
./venv/Scripts/python -B -X utf8 tests/backend_entry_selftest.py
./venv/Scripts/python -B -X utf8 tests/world_agent_json_recovery_selftest.py
./venv/Scripts/python -B -X utf8 tests/grounding_candidate_isolation_selftest.py
./venv/Scripts/python -B -X utf8 tests/release_summary_selftest.py
./venv/Scripts/python -B -X utf8 tests/release_pipeline_selftest.py
./venv/Scripts/python -B -X utf8 tests/native_evaluation_selftest.py
./venv/Scripts/python -B -X utf8 tests/native_results_selftest.py
```
