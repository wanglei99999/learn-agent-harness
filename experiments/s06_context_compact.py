"""
s06_context_compact.py — 三层上下文压缩

s06 在 s05 基础上新增一个机制：
  长对话 token 膨胀时，通过三层压缩策略维持上下文可用。

三层压缩策略：

  Layer 1 — micro_compact（每轮自动）：
    把历史 tool_result 替换为短占位符 "[Previous: used bash]"
    只保留最近 KEEP_RECENT 条完整结果
    轻量、无 API 调用、原地修改

  Layer 2 — auto_compact（token 超阈值时）：
    调用 LLM 生成对话摘要
    用一条摘要消息替换整个 messages[]
    messages[:] = ... 原地替换，调用方持有的引用不失效

  Layer 3 — manual_compact（LLM 主动调用 compact 工具）：
    LLM 自己判断"上下文快满了"，主动触发压缩
    执行完立即 return，下轮对话从摘要开始

关键实现细节：
  messages[:] = auto_compact(messages)
  ↑ 切片赋值：原地替换列表内容，而非重新绑定变量
    如果写 messages = auto_compact(messages)，函数外的引用不会更新

  PRESERVE_RESULT_TOOLS = {"read_file"}
  ↑ 文件内容可能后续还在用，不压缩

s06 → s07 的演进：上下文能维持了，但任务管理仍靠 LLM 记忆，需要持久化任务系统。
"""
import json
import os
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

# ── 压缩参数 ──────────────────────────────────────────────────────────────────
KEEP_RECENT = 3                          # micro_compact 保留最近几条 tool_result
PRESERVE_RESULT_TOOLS = {"read_file"}    # 这些工具的结果不压缩（内容可能还在用）
TRANSCRIPT_DIR = WORKDIR / ".transcripts"  # auto_compact 保存原始记录的目录
THRESHOLD = 50000                        # 估算 token 超过此值触发 auto_compact

def estimate_tokens(messages:str)->int:
    """粗略计算token，按照4个字符一个token计算"""
    return len(str(messages))//4

# ── Layer 1: micro_compact ────────────────────────────────────────────────────
# 轻量压缩：保留最近 KEEP_RECENT 条 tool_result，把更早的替换为短占位符
# 原地修改 messages（字典是引用类型），不重建列表

def micro_compact(messages: list) -> list:
    # 第一步：收集所有 tool_result 的位置和内容
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))

    # 总数不超过保留数，无需压缩
    if len(tool_results) <= KEEP_RECENT:
        return messages

    # 第二步：建立 tool_use_id → 工具名 的映射
    # tool_result 只有 id，工具名在 assistant 消息的 tool_use block 里
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name

    # 第三步：把最近 KEEP_RECENT 条之前的结果替换为占位符
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        # 内容太短，压缩没意义
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            continue
        tool_id = result.get("tool_use_id")
        tool_name = tool_name_map.get(tool_id, "unknown")
        # read_file 结果保留，文件内容后续可能还有用
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"   # 原地替换
    return messages

# ── Layer 2: auto_compact ─────────────────────────────────────────────────────
# 重度压缩：token 超过阈值时触发
# 1. 把完整对话存到 .transcripts/ 防止丢失
# 2. 调用 LLM 生成摘要
# 3. 用一条摘要消息替换整个 messages[]，大幅释放上下文

def auto_compact(messages: list) -> list:
    # 保存原始记录到本地，防止压缩后丢失细节
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")

    # 取最后 80000 字符送给 LLM 总结（避免摘要请求本身超出限制）
    conversation_text = json.dumps(messages, default=str)[-80000:]

    # 不传 tools，模型只能输出文本，确保 response.content 里只有 TextBlock
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "总结本次对话以保持上下文连贯，内容包括：\n"
            "1. 已完成的事项。\n"
            "2. 当前的进展状态。\n"
            "3. 已做出的关键决策。\n"
            "行文简洁，但要保留核心细节。\n\n" + conversation_text
        }],
        max_tokens=2000,
    )
    summary = next((block.text for block in response.content if hasattr(block, "text")), "")
    if not summary:
        summary = "No summary generated."

    # 返回单条消息替换整个 messages[]，上下文大幅缩短
    return [{
        "role": "user",
        "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"
    }]

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
    "compact":    lambda **kw: "手动压缩请求"   # 假 handler，真正的压缩逻辑在 agent_loop 里用 block.name 判断触发
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {
        "name":"compact",
        "description":"触发手动压缩请求",
        "input_schema":{
            "type":"object",
            "properties":{
                "focus":{
                    "type":"string","description":"总结中需要保留哪些内容"
                }
            }
        },
    },
]

# ── Agent 主循环（三层压缩策略）─────────────────────────────────────────────────
# 每轮开始时：
#   Layer 1 micro_compact  → 轻量压缩，替换老旧 tool_result
#   Layer 2 auto_compact   → token 超阈值时，LLM 生成摘要替换整个 messages
# 模型主动调用 compact 工具时：
#   Layer 3 manual_compact → 立即触发 auto_compact，然后退出本轮循环

def agent_loop(messages: list):
    while True:
        # Layer 1：每轮自动执行，原地压缩老旧工具结果
        micro_compact(messages)

        # Layer 2：token 超阈值，用 LLM 摘要替换整个 messages
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)   # [:] 原地替换列表内容

        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        manual_compact = False   # 标记本轮模型是否主动调用了 compact 工具

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    # Layer 3：模型主动请求压缩，先收集结果，循环结束后执行
                    manual_compact = True
                    output = "压缩中..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"未知工具：{block.name}"
                    except Exception as e:
                        output = f"错误：{e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

        messages.append({"role": "user", "content": results})

        # Layer 3：模型主动压缩，执行后退出循环（下次对话从摘要开始）
        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages)
            return

if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
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