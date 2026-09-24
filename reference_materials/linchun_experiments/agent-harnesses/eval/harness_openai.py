"""OpenAI Agents SDK：与 dsh/Codex/Claude 同一份文件树，用 grep + read 查阅。"""
from __future__ import annotations

import time
from typing import Any

from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from agents.exceptions import MaxTurnsExceeded
from agents.memory.sqlite_session import SQLiteSession

from costing import usage_from_agents_result
from memory_store import MemoryStore


INSTRUCTIONS = """你是长期记忆问答助手。工作目录里的 sessions/ 与 INDEX.md 是全部记忆。
你必须用工具查阅后再作答，不要用工具之外的世界知识编造。

可用工具（四路同一套，只有这两个）：
- grep_memory：在全部文档正文中检索关键词，返回带路径和周期的命中（含晚期周期，不只最早几条）
- read_file：按相对路径读一篇（路径来自 grep_memory）

规则：
1. 先 grep_memory 定位，再 read_file 读必要文档。不要枚举全树，不要反复空搜。
2. 时间线/排序题必须核对相关周期，不要只看最早命中。
3. 查到能答题的事实后立刻停止调用工具，只输出最终答案。
4. 信息冲突时，官方通报/周报优先于传闻/群聊转述。
5. 最终只给极简答案：一个词、人名、数字、短语，或拒答固定措辞。不要解释。
6. 排序题只给出从早到晚的事件顺序。选择题只从选项里抄一个动作名。
"""


def _model_settings(model: str, *, max_tokens: int = 4096) -> ModelSettings:
    name = (model or "").lower()
    if "deepseek" in name:
        return ModelSettings(
            max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
    if name.startswith("gpt-4o") or name.startswith("gpt-4-"):
        return ModelSettings(max_tokens=max_tokens, temperature=0)
    # gpt-5.x：luna/sol 等带工具时不要 temperature，reasoning 置 none
    return ModelSettings(max_tokens=max_tokens, reasoning={"effort": "none"})


def make_agent(store: MemoryStore, model: str) -> Agent:
    @function_tool
    def grep_memory(query: str) -> str:
        """在全部记忆文档中检索关键词，返回带周期和相对路径的片段。可搜人名、实体、字段。"""
        return store.grep_text(str(query))

    @function_tool
    def read_file(path: str) -> str:
        """按相对路径读取一篇记忆文档，例如 sessions/s03_2025-01-20/12_公告_xxx.md。"""
        return store.read_file_text(str(path))

    return Agent(
        name="memory-eval",
        instructions=INSTRUCTIONS,
        model=model,
        tools=[grep_memory, read_file],
        model_settings=_model_settings(model),
    )


def answer_one(
    agent: Agent,
    provider,
    protocol: str,
    question: str,
    *,
    max_turns: int = 12,
    session_id: str = "q",
) -> dict[str, Any]:
    user = (
        (protocol + "\n\n" if protocol else "")
        + "【问题】"
        + question
        + "\n\n请用 grep_memory / read_file 查阅后只给最终答案（极简）。查到即可停止工具调用："
    )
    run_cfg = RunConfig(model_provider=provider, tracing_disabled=True)
    session = SQLiteSession(session_id)
    t0 = time.monotonic()

    def _out(result) -> str:
        return str(getattr(result, "final_output", None) or "").strip()

    try:
        result = Runner.run_sync(
            agent, user, max_turns=max_turns, session=session, run_config=run_cfg
        )
        pred = _out(result)
        n_items = len(getattr(result, "new_items", []) or [])
        out = {
            "pred": pred,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "n_items": n_items,
            "error": None,
        }
        try:
            usage = usage_from_agents_result(result)
            if usage:
                out["usage"] = usage
        except Exception:
            pass
        return out
    except MaxTurnsExceeded as e:
        closer = Agent(
            name="memory-eval-close",
            instructions=(
                "根据到目前为止已经查到的记忆给出最终极简答案。"
                "禁止再调用工具。只输出答案本身。"
            ),
            model=agent.model,
            tools=[],
            model_settings=_model_settings(str(agent.model), max_tokens=1024),
        )
        try:
            result = Runner.run_sync(
                closer,
                "现在必须给出最终答案，只输出答案本身。",
                max_turns=2,
                session=session,
                run_config=run_cfg,
            )
            pred = _out(result)
            out = {
                "pred": pred,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "n_items": None,
                "error": None if pred else f"MaxTurnsExceeded:{e}",
            }
            try:
                usage = usage_from_agents_result(result)
                if usage:
                    out["usage"] = usage
            except Exception:
                pass
            return out
        except Exception as e2:
            return {
                "pred": "",
                "elapsed_s": round(time.monotonic() - t0, 2),
                "n_items": None,
                "error": f"MaxTurnsExceeded+{type(e2).__name__}:{str(e2)[:160]}",
            }
    except Exception as e:
        return {
            "pred": f"[SOLVE_ERROR:{type(e).__name__}]",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "n_items": None,
            "error": f"{type(e).__name__}:{str(e)[:200]}",
        }
