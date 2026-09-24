"""CLI harness：把同一份 session 文件树交给 claude / codex / dsh 查阅作答。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from costing import usage_from_claude_json, usage_from_codex_text

HARNESS_ROOT = Path(__file__).resolve().parents[1]
_MCP_SERVER = HARNESS_ROOT / "eval" / "memory_mcp.py"
_MCP_PYTHON = HARNESS_ROOT / "openai-agents-sdk" / ".venv" / "bin" / "python"


def _memory_mcp_cmd(workspace: Path) -> tuple[str, list[str]]:
    py = str(_MCP_PYTHON if _MCP_PYTHON.is_file() else sys.executable)
    return py, [str(_MCP_SERVER), str(Path(workspace).resolve())]


def write_memory_mcp_config(workspace: Path) -> Path:
    """四路共用的 grep_memory + read_file MCP。"""
    workspace = Path(workspace).resolve()
    command, args = _memory_mcp_cmd(workspace)
    cfg = {
        "mcpServers": {
            "memory": {
                "type": "stdio",
                "command": command,
                "args": args,
            }
        }
    }
    path = workspace / ".memory-mcp.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def write_dsh_memory_patch(workspace: Path) -> Path:
    """关掉 bash/glob 等额外工具，只挂 MemoryStore MCP。"""
    workspace = Path(workspace).resolve()
    command, args = _memory_mcp_cmd(workspace)
    args_yaml = ", ".join(repr(a) for a in args)
    text = f"""# 与 Agents/Claude 对齐：只保留 grep_memory + read_file
- id: tool-bash
  disabled: true
- id: tool-pwsh
  disabled: true
- id: tool-jobs
  disabled: true
- id: tool-skill
  disabled: true
- id: tool-fs-search
  disabled: true
- id: tool-fs
  disabled: true
- id: mcp-memory
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: memory
    transport: stdio
    command: {command!r}
    args: [{args_yaml}]
    failOnStartupError: true
"""
    path = workspace / ".dsh-memory-tools.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def apply_cli_env() -> None:
    """补齐 PATH，并把 Claude 派生变量与仓库 .env 对齐。"""
    npm_bin = str(HARNESS_ROOT / ".npm-global" / "bin")
    os.environ["PATH"] = npm_bin + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("CODEX_HOME", str(HARNESS_ROOT / "codex-home"))
    os.environ.setdefault("CLAUDE_CONFIG_DIR", str(HARNESS_ROOT / "claude-home"))
    os.environ.setdefault("NPM_CONFIG_PREFIX", str(HARNESS_ROOT / ".npm-global"))
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    if "maas.aliyuncs.com" in base and "compatible-mode" in base:
        os.environ["ANTHROPIC_BASE_URL"] = base.split("/compatible-mode")[0] + "/apps/anthropic"
    elif base.endswith("/v1"):
        os.environ["ANTHROPIC_BASE_URL"] = base[:-3]
    elif base:
        os.environ["ANTHROPIC_BASE_URL"] = base
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
        os.environ["ANTHROPIC_AUTH_TOKEN"] = key
    model = os.environ.get("MODEL", "")
    if model:
        os.environ["ANTHROPIC_MODEL"] = model
        os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
        os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
        os.environ["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
        os.environ["ANTHROPIC_CUSTOM_MODEL_OPTION"] = model
    os.environ.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    os.environ.setdefault("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "1")
    os.environ.setdefault("CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT", "1")


_FULL_CORPUS_SYSTEM = """你是长期记忆问答助手，不是编程助手。
工作目录里的 sessions/ 与 INDEX.md 是全部记忆。
只用 Grep/Read（或等价的 rg/read）。先按实体名检索，再读必要文档。
时间线/排序题必须核对相关周期，不要只看最早几条命中。
禁止 Glob 全树、禁止 Bash、禁止写文件、禁止再开子任务。
查到能答题的事实后立刻停止工具。答案必须极简：一个词、人名、数字或短语，或拒答固定措辞。不要解释。"""

_WORKSPACE_AGENTS_MD = (
    "你是长期记忆问答助手。sessions/ 与 INDEX.md 是全部观察记录。\n"
    "只用 grep_memory（或等价 grep/rg）按实体名检索，再 read_file（或 read）。\n"
    "时间线/排序题必须核对相关周期，不要只读最早命中。\n"
    "禁止全树空搜、禁止写文件、禁止再开子任务。最终只给极简答案。\n"
)

_HALLUCINATED_ROOTS = (
    "/workspace",
    "/root",
    "/wkspace",
    "/weka",
    "/home/user/workspace",
    "/wkspace/ai_knowledge_base",
)


def _claude_system(cwd: Path) -> str:
    """Claude 专用：写死本机 cwd，纠正 DeepSeek 的训练路径先验。"""
    root = str(Path(cwd).resolve())
    return (
        "你是长期记忆问答助手，不是编程助手。\n"
        f"唯一合法工作目录是：{root}\n"
        "记忆只在该目录下的 INDEX.md 与 sessions/**/*.md。\n"
        "可用工具只有两个（与其他 harness 同一套 MemoryStore）：\n"
        "mcp__memory__grep_memory（参数 query）和 mcp__memory__read_file（参数 path）。\n"
        "1) 第一步必须调用 mcp__memory__grep_memory，query=问题中的实体中文名。不要用短名 grep_memory，那个工具不存在。\n"
        "2) 再 mcp__memory__read_file 读命中的相对路径 .md。不要读目录，不要先读完整 INDEX。\n"
        "禁止尝试这些路径（训练幻觉，必然失败）："
        + " ".join(_HALLUCINATED_ROOTS)
        + "\n"
        "若工具失败：换相对路径再 grep_memory，不要报告权限，也不要因此答「无此项/查无此记录」。\n"
        "时间线/排序题必须核对相关周期，不要只看最早几条命中。\n"
        "禁止 Glob、Bash、写文件、子任务。查到能答题的事实后立刻停止工具。\n"
        "答案必须极简：一个词、人名、数字或短语，或拒答固定措辞。不要解释。"
    )


def _prompt(protocol: str, question: str) -> str:
    return (
        "工作目录里的 sessions/ 与 INDEX.md 是全部记忆。"
        "必须用 grep_memory（或等价 grep/rg）定位后再 read_file（或 read），不要编造，也不要枚举全树。\n"
        "先按实体名/字段检索，再读必要文档；时间线/排序题要核对相关周期，不要只看最早几条命中。\n"
        "查到能答题的事实后立刻停止工具。\n"
        "最终只给极简答案（一个词、人名、数字或短语，或拒答固定措辞），不要解释。\n\n"
        + (protocol + "\n\n" if protocol else "")
        + "【问题】"
        + question
        + "\n\n请查阅后只给最终答案："
    )


def _claude_prompt(
    protocol: str,
    question: str,
    cwd: Path,
    *,
    entity: str | None = None,
    field: str | None = None,
) -> str:
    root = str(Path(cwd).resolve())
    hint_bits = [x for x in (entity, field) if x]
    hint = f"mcp__memory__grep_memory 的 query 用：{' / '.join(hint_bits)}\n" if hint_bits else ""
    return (
        f"当前工作目录是 {root}。记忆只在 INDEX.md 与 sessions/。\n"
        "只调用 mcp__memory__grep_memory 与 mcp__memory__read_file。不要调用短名 grep_memory（不存在）。\n"
        "第一步必须 mcp__memory__grep_memory，不要读 /workspace、/root、/wkspace 或任何目录。\n"
        "工具失败时改相对路径再搜，不要据此拒答或报告权限。\n"
        + hint
        + "查到能答题的事实后立刻停止工具。最终只给极简答案，不要解释。\n\n"
        + (protocol + "\n\n" if protocol else "")
        + "【问题】"
        + question
        + "\n\n请用 mcp__memory__grep_memory 查阅后只给最终答案："
    )


def ensure_workspace_instructions(workspace: Path) -> None:
    """四路共用的全库检索约定，写进 Codex/Claude/dsh 会读的说明文件。"""
    workspace = Path(workspace).resolve()
    (workspace / "AGENTS.md").write_text(_WORKSPACE_AGENTS_MD, encoding="utf-8")
    claude_md = (
        _WORKSPACE_AGENTS_MD
        + f"\n当前工作目录：{workspace}\n"
        "只读本目录 INDEX.md 与 sessions/。禁止 /workspace /root /wkspace /weka。\n"
        "第一步 Grep 实体名（path=sessions），再 Read 命中的 .md。不要 Read 目录。\n"
        "EACCES/不存在时改相对路径，不要报告权限或查无。\n"
    )
    (workspace / "CLAUDE.md").write_text(claude_md, encoding="utf-8")


def _dsh_route_for_model(model: str) -> tuple[str, str | None]:
    """按模型选 dsh 提供方与推理强度。luna/sol 关 reasoning；mini 开 low。"""
    name = (model or "").lower()
    base = os.environ.get("OPENAI_BASE_URL", "")
    if "maas.aliyuncs.com" in base or name.startswith("deepseek"):
        return "aliyun", None
    if any(tag in name for tag in ("luna", "terra", "sol")):
        return "aiaiapi", None
    if re.search(r"gpt-4o(?!-mini)", name) or name == "gpt-4o":
        return "aiaiapi", None
    if "mini" in name or name.startswith("gpt-5"):
        return "aiaiapi", "low"
    return "aiaiapi", None


def _sync_dsh_default_model(model: str) -> None:
    path = HARNESS_ROOT / "dsh-home" / "settings.yaml"
    if not path.is_file() or not model:
        return
    provider, effort = _dsh_route_for_model(model)
    text = path.read_text(encoding="utf-8")
    block = f"agent-default-model:\n  provider: {provider}\n  model: {model}"
    if effort:
        block += f"\n  reasoningEffort: {effort}"
    updated = re.sub(
        r"agent-default-model:\n  provider: \S+\n  model: \S+(?:\n  reasoningEffort: \S+)?",
        block,
        text,
        count=1,
    )
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def _parse_claude_stdout(stdout: str) -> tuple[str, str | None]:
    """返回 (pred, parse_error)。JSON 元数据不当作答案。"""
    text = (stdout or "").strip()
    if not text:
        return "", "empty_stdout"
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        if text.startswith("{") and '"stop_reason"' in text:
            return "", "json_metadata_only"
        return text, None
    for key in ("result", "text", "answer"):
        val = data.get(key)
        if isinstance(val, str) and val.strip() and not val.strip().startswith("{"):
            if val.startswith("API Error:") or "reasoning_effort" in val:
                return "", val[:180]
            return val.strip(), None
    if data.get("is_error") or data.get("subtype") == "error_max_turns":
        errors = data.get("errors") or []
        hint = errors[0] if errors else data.get("subtype") or "claude_error"
        return "", f"{hint}"
    return "", "no_result_field"


def _parse_codex_stdout(stdout: str, last_path: Path) -> str:
    if last_path.is_file():
        last = last_path.read_text(encoding="utf-8", errors="replace").strip()
        if last:
            return last
    text = (stdout or "").strip()
    if not text:
        return ""
    # --json 时取最后一条 agent 文本
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") in {"item.completed", "agent_message", "message"}:
            msg = obj.get("item") or obj
            content = msg.get("text") or msg.get("content") or ""
            if isinstance(content, str) and content.strip():
                return content.strip()
    # 普通 exec：最后一段非横幅文本
    skip = ("Reading additional input", "OpenAI Codex", "workdir:", "model:", "provider:", "tokens used")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    useful = [ln for ln in lines if not any(ln.startswith(s) or s in ln[:24] for s in skip)]
    return (useful[-1] if useful else text).strip()


def _stderr_summary(stderr: str, n: int = 360) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    interesting = [
        ln for ln in lines
        if any(k in ln.lower() for k in ("error", "429", "401", "403", "timeout", "saturated"))
    ]
    blob = " | ".join(interesting[-4:] if interesting else lines[-4:])
    return blob[-n:]


def _looks_rate_limited(stderr: str, pred: str) -> bool:
    blob = f"{stderr}\n{pred}".lower()
    return "429" in blob or "rate limit" in blob or "负载已饱和" in blob or "too many requests" in blob


def _inline_memory(run_dir: Path, *, limit_chars: int = 14000) -> str:
    parts: list[str] = []
    for name in ("PROTOCOL.md", "INDEX.md"):
        p = run_dir / name
        if p.is_file():
            parts.append(f"## {name}\n{p.read_text(encoding='utf-8', errors='ignore')[:2500]}")
    sess = run_dir / "sessions"
    if sess.is_dir():
        files = sorted(sess.rglob("*.md"))[:8]
        for p in files:
            body = p.read_text(encoding="utf-8", errors="ignore")[:1600]
            parts.append(f"## {p.relative_to(run_dir)}\n{body}")
    text = "\n\n".join(parts)
    return text[:limit_chars]


def answer_cli(
    harness: str,
    workspace: Path,
    protocol: str,
    question: str,
    *,
    timeout_s: int = 180,
    max_turns: int | None = None,
    entity: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    apply_cli_env()
    model = os.environ.get("MODEL", "gpt-5-mini-2025-08-07")
    workspace = Path(workspace)
    run_dir = workspace
    ensure_workspace_instructions(workspace)
    if harness == "dsh":
        _sync_dsh_default_model(model)
    prompt = (
        _claude_prompt(protocol, question, run_dir, entity=entity, field=field)
        if harness == "claude"
        else _prompt(protocol, question)
    )

    last_msg = Path(tempfile.gettempdir()) / f"codex-last-{os.getpid()}.txt"
    if last_msg.exists():
        last_msg.unlink()

    env = os.environ.copy()
    claude_session = str(uuid.uuid4()) if harness == "claude" else ""
    if harness == "claude":
        claude_turns = (
            max_turns
            if max_turns and max_turns > 12
            else int(os.environ.get("CLAUDE_MAX_TURNS", "20"))
        )
        claude_effort = os.environ.get("CLAUDE_EFFORT", "")
        if not claude_effort and not (model or "").lower().startswith("deepseek"):
            claude_effort = "low"
        mcp_cfg = write_memory_mcp_config(run_dir)
        cmd = [
            "claude", "--bare", "-p",
            "--model", model,
            # 不要 --tools "" / --strict-mcp-config / allowedTools：这几项会让 MCP 工具根本挂不上。
            "--mcp-config", str(mcp_cfg),
            "--dangerously-skip-permissions",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(run_dir.resolve()),
            "--max-turns", str(claude_turns),
            "--max-budget-usd", os.environ.get("CLAUDE_MAX_BUDGET_USD", "8.00"),
            "--output-format", "json",
            "--session-id", claude_session,
            # 不用 --system-prompt 覆盖默认提示：默认提示里有 cwd，覆盖后 DeepSeek 会去读训练路径。
            "--append-system-prompt", _claude_system(run_dir),
            "--", prompt,
        ]
        if claude_effort:
            cmd[cmd.index("--model") + 2 : cmd.index("--model") + 2] = ["--effort", claude_effort]
    elif harness == "codex":
        codex_provider = (
            "aliyun" if "maas.aliyuncs.com" in os.environ.get("OPENAI_BASE_URL", "") else "aiaiapi"
        )
        cmd = [
            "codex", "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox", "read-only",
            "--color", "never",
            "-C", str(run_dir),
            "-m", model,
            "-c", f"model_provider={codex_provider}",
            "--output-last-message", str(last_msg),
            prompt,
        ]
    elif harness == "dsh":
        env["DSH_PERMISSION_MODE"] = "read-only"
        cmd = ["dsh", "--profile", "headless", "--", prompt]
    else:
        raise ValueError(f"unknown cli harness: {harness}")

    attempts = 2 if harness == "claude" else 1
    t0 = time.monotonic()
    last: dict[str, Any] | None = None
    for attempt in range(attempts):
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(run_dir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired:
            last = {
                "pred": "[SOLVE_ERROR:TimeoutExpired]",
                "elapsed_s": round(time.monotonic() - t0, 2),
                "n_items": None,
                "error": f"TimeoutExpired:{timeout_s}s",
            }
            break
        except FileNotFoundError as e:
            return {
                "pred": "[SOLVE_ERROR:FileNotFoundError]",
                "elapsed_s": round(time.monotonic() - t0, 2),
                "n_items": None,
                "error": f"FileNotFoundError:{e}",
            }

        stdout = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        parse_err = None
        if harness == "claude":
            pred, parse_err = _parse_claude_stdout(stdout)
        elif harness == "codex":
            pred = _parse_codex_stdout(stdout, last_msg)
        else:
            pred = stdout

        err = None
        if not pred:
            if parse_err:
                err = parse_err
            elif proc.returncode != 0:
                err = f"exit={proc.returncode}:{_stderr_summary(stderr)}"
            else:
                err = "empty_pred"
            pred = f"[SOLVE_ERROR:{err.split(':')[0]}]"
        elif proc.returncode != 0 and harness == "codex" and _looks_rate_limited(stderr, pred):
            err = f"exit={proc.returncode}:{_stderr_summary(stderr)}"
            pred = f"[SOLVE_ERROR:RateLimit]"

        last = {
            "pred": pred,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "n_items": None,
            "error": err,
            "returncode": proc.returncode,
        }
        if harness == "claude":
            usage = usage_from_claude_json(stdout)
        elif harness == "codex":
            usage = usage_from_codex_text(stdout, stderr)
        else:
            usage = None
        if usage:
            last["usage"] = usage
        if (
            harness == "claude"
            and err
            and "maximum number of turns" in (err + parse_err if parse_err else err)
            and claude_session
        ):
            closer = [
                "claude", "--bare", "-p",
                "--resume", claude_session,
                "--model", model,
                "--tools", "",
                "--dangerously-skip-permissions",
                "--permission-mode", "bypassPermissions",
                "--max-turns", "2",
                "--output-format", "json",
                "--",
                "根据到目前为止已经查到的记忆，立刻给出最终极简答案。"
                "禁止再调用任何工具。只输出答案本身。",
            ]
            try:
                proc3 = subprocess.run(
                    closer,
                    cwd=str(run_dir),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=min(90, timeout_s),
                    env=env,
                )
            except subprocess.TimeoutExpired:
                break
            stdout3 = (proc3.stdout or b"").decode("utf-8", errors="replace").strip()
            pred3, parse3 = _parse_claude_stdout(stdout3)
            if pred3:
                last = {
                    "pred": pred3,
                    "elapsed_s": round(time.monotonic() - t0, 2),
                    "n_items": None,
                    "error": None,
                    "returncode": proc3.returncode,
                }
                break
            last["error"] = err if not parse3 else f"{err}; closer:{parse3}"
            break
        if err and _looks_rate_limited(stderr, pred) and harness == "codex":
            # 多轮工具调用容易把分组打满；改成单次、禁止工具的补答。
            excerpt = _inline_memory(run_dir)
            fallback_prompt = (
                "以下是已检索到的记忆摘录。禁止调用任何工具，根据摘录作答。\n"
                "只输出极简答案。\n\n"
                + excerpt
                + "\n\n【问题】"
                + question
                + "\n答案："
            )
            fb_cmd = [
                "codex", "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox", "read-only",
                "--color", "never",
                "-C", str(run_dir),
                "-m", model,
                "-c", f"model_provider={codex_provider}",
                "--output-last-message", str(last_msg),
                fallback_prompt,
            ]
            time.sleep(8)
            try:
                proc2 = subprocess.run(
                    fb_cmd,
                    cwd=str(run_dir),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=min(90, timeout_s),
                    env=os.environ.copy(),
                )
            except subprocess.TimeoutExpired:
                break
            stdout2 = (proc2.stdout or b"").decode("utf-8", errors="replace")
            stderr2 = (proc2.stderr or b"").decode("utf-8", errors="replace")
            pred2 = _parse_codex_stdout(stdout2, last_msg)
            if pred2 and not _looks_rate_limited(stderr2, pred2):
                last = {
                    "pred": pred2,
                    "elapsed_s": round(time.monotonic() - t0, 2),
                    "n_items": None,
                    "error": None,
                    "returncode": proc2.returncode,
                }
                break
            last["error"] = f"exit={proc.returncode}:{_stderr_summary(stderr2 or stderr)}"
            break
        if err and _looks_rate_limited(stderr, pred) and attempt + 1 < attempts:
            time.sleep(15 * (attempt + 1))
            continue
        break

    return last or {
        "pred": "[SOLVE_ERROR:unknown]",
        "elapsed_s": round(time.monotonic() - t0, 2),
        "n_items": None,
        "error": "unknown",
    }
