"""
s04_subagent.py — Subagent（子代理）

s04 在 s03 基础上新增一个机制：
  parent 可以通过 task 工具启动 subagent，把子任务委托出去。

核心设计：上下文隔离

  parent messages:  [用户问题, ..., task工具结果="摘要"]
  subagent messages:[子任务描述, ..., 执行细节]  ← 全新列表，互不干扰

  subagent 干完活后，只把最后的文本摘要返回给 parent。
  中间所有的工具调用、执行细节、错误尝试都留在 subagent 的 messages 里，
  不会污染 parent 的上下文，parent 只看到结果。

工具权限分层：
  CHILD_TOOLS  = [bash, read_file, write_file, edit_file]    ← subagent 只能干活
  PARENT_TOOLS = CHILD_TOOLS + [task]                        ← parent 额外能派任务

  subagent 没有 task 工具，不能再派子 subagent（防止无限递归）。

run_subagent() 的关键设计：
  - range(30) 而非 while True：给 subagent 设执行上限，防止失控
  - 独立的 SUBAGENT_SYSTEM 提示词，引导它完成任务后输出摘要
  - 返回值只取最后一轮的文本，摘要作为 task 工具的结果回传 parent

s04 → s05 的演进：子任务能分担，但 LLM 缺乏专业知识，需要按需加载领域技能。
"""
import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"你是一个工作在目录{WORKDIR}下的代码智能助手,你可以借助任务工具拆分调研工作与子任务。"
# subagent 有独立的 system prompt，引导它完成任务后输出摘要（作为返回给 parent 的结果）
SUBAGENT_SYSTEM = f"你是一个工作在目录{WORKDIR}下的代码子助手，完整分发给你的指定任务后，汇总所得结果。"

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# ── 工具定义分层 ──────────────────────────────────────────────────────────────
# CHILD_TOOLS：subagent 可用的工具（只有基础文件操作，没有 task）
# PARENT_TOOLS：parent 在 CHILD_TOOLS 基础上额外拥有 task 工具，可以派发子任务
CHILD_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

PARENT_TOOLS = CHILD_TOOLS + [
    {
        "name": "task",
        "description": "新建一个独立上下文的子代理，共享文件系统，但不共用会话记录。",
        "input_schema": {
            "type":"object",
            "properties": {
                "prompt":{"type":"string"},
                "description":{"type":"string", "description":"任务简短描述"},
            },
            "required":["prompt"]
        },
    }
]

# ── Subagent 执行器 ───────────────────────────────────────────────────────────

def run_subagent(prompt: str) -> str:
    # 关键：全新的 messages[]，与 parent 完全隔离，执行细节不会污染 parent 上下文
    sub_message = [{"role": "user", "content": prompt}]

    # range(30) 而非 while True：给 subagent 设上限，防止失控消耗 token
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_message,
            tools=CHILD_TOOLS, max_tokens=8000,
        )
        sub_message.append({"role": "assistant", "content": response.content})

        # 模型不再调用工具，任务结束，break 后提取摘要
        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"未知工具：{block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_message.append({"role": "user", "content": results})

    # 只把最后一轮的文本摘要返回给 parent，中间过程全部丢弃
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"

def agent_loop(messages:list):
    while True:
        response = client.messages.create(
            model = MODEL, system=SYSTEM, messages = messages,
            tools=PARENT_TOOLS,max_tokens=8000,
        )
        messages.append({"role":"assistant","content":response.content})
        if response.stop_reason != "tool_use":
            return 
        results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "task":
                    # task 工具：启动 subagent，返回的是摘要字符串
                    desc = block.input.get("description", "subtask")
                    prompt = block.input.get("prompt", "")
                    print(f"> task ({desc}): {prompt[:80]}")
                    output = run_subagent(prompt)
                else:
                    # 其他工具：走普通分发
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"未知工具：{block.name}"
                print(f"  {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})

if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()