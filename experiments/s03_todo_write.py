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

SYSTEM = f"""你是一个在 {WORKDIR} 工作目录下的代码智能助手。
使用待办工具规划多步骤任务。开始任务前标记为进行中，完成后标记为已完成。
优先使用工具操作，而非文字描述。"""


# ── TodoManager：LLM 可写入的结构化进度状态 ──────────────────────────────────

#todoManager只做状态维护，实际是否完成是交给模型实现的，这里实现两个东西，状态更新和结果字符化 
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        """全量替换待办列表，验证合法性后保存并返回渲染结果。"""
        if len(items) > 20:
            raise ValueError("最多允许创建 20 条待办事项")

        validated = []
        in_progress_count = 0

        for i, item in enumerate(items):
            text     = str(item.get("text", "")).strip()
            status   = str(item.get("status", "pending")).lower()
            item_id  = str(item.get("id", str(i + 1)))

            if not text:
                raise ValueError(f"Item {item_id}: 需要文本")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: 无效状态 '{status}'")
            if status == "in_progress":
                in_progress_count += 1

            validated.append({"id": item_id, "text": text, "status": status})

        # 同一时刻只允许一个任务处于进行中，保证模型专注单任务
        if in_progress_count > 1:
            raise ValueError("同一时间只允许一个任务处于进行中状态")

        # 全量替换：直接覆盖旧列表，状态完全由 LLM 本次传入的数据决定
        # LLM 自己根据工具执行结果判断哪条完成了，然后传过来，harness 只负责存
        self.items = validated

        #这个很关键，返回给LLM做信息
        return self.render()

    def render(self) -> str:
        """将待办列表渲染为可读字符串，返回给 LLM 作为工具结果。"""
        if not self.items:
            return "没有待办项目"

        # 用字典映射状态 → 符号，比 if/elif 更简洁
        MARKERS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}

        lines = [
            f"{MARKERS[item['status']]} #{item['id']}: {item['text']}"
            for item in self.items
        ]

        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


TODO = TodoManager()


# ── 工具实现 ──────────────────────────────────────────────────────────────────

def safe_path(p: str) -> Path:
    """将相对路径解析为绝对路径，并确保不会逃出工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径越界: {p}")
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


# ── 工具分发表：工具名 → 处理函数 ────────────────────────────────────────────

TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}


# ── 工具定义（JSON Schema）：告诉 LLM 每个工具接受什么参数 ────────────────────
# 结构：name / description / input_schema
#   input_schema.properties  定义有哪些参数及类型
#   input_schema.required    列出 LLM 必须传的参数（不在此列表的为可选）

TOOLS = [
    {
        "name": "bash",
        "description": "执行 shell 命令。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取文件内容。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":  {"type": "string"},
                "limit": {"type": "integer"},   # 可选：最多读取的行数
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "将内容写入文件。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "替换文件中的指定文本（精确匹配）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":     {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "todo",
        "description": "更新待办列表，跟踪多步骤任务的进度。",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {              # 参数名，我们自己起的，叫 tasks 也行
                    "type": "array",
                    "items": {          # JSON Schema 关键字（固定），意思是"数组里每个元素长这样"
                        "type": "object",
                        "properties": {
                            "id":     {"type": "string"},
                            "text":   {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["id", "text", "status"],
                    },
                },
            },
            "required": ["items"],   # ← required 必须在 input_schema 内部
        },
    },
]

# ── Agent 主循环 ──────────────────────────────────────────────────────────────

def agent_loop(messages: list):
    rounds_since_todo = 0   # 记录距离上次调用 todo 工具已经过了几轮

    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # 模型没有调用工具，说明任务结束，退出循环
        if response.stop_reason != "tool_use":
            return

        results = []
        used_todo = False   # 标记本轮是否用了 todo 工具

        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"未知工具：{block.name}"
                except Exception as e:
                    output = f"错误：{e}"

                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

                if block.name == "todo":
                    used_todo = True

        # 用了 todo 就归零，否则计数 +1
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1

        # 连续 3 轮没更新待办，向 tool_result 里注入提醒，推动模型更新进度
        #nag reminder机制
        if rounds_since_todo >= 3:
            results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})

        messages.append({"role": "user", "content": results})

if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
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