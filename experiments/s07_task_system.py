"""
s07_task_system.py — 文件持久化任务系统

s07 在 s06 基础上新增一个机制：
  任务状态保存到 .tasks/*.json 文件，不依赖 messages[] 上下文。

核心设计：任务存文件，不存消息

  之前（s01-s06）：任务状态靠 LLM 记忆 / todo 列表维护，对话压缩后可能丢失
  s07：每个任务是一个独立 JSON 文件，重启、压缩后依然存在

任务文件结构（.tasks/task_1.json）：
  {
    "id": 1,
    "subject": "实现登录功能",
    "status": "pending",          ← pending / in_progress / completed
    "blockedBy": [2, 3],          ← 依赖的任务 ID 列表（未完成则无法开始）
    "created_at": 1234567890.0
  }

依赖关系（blockedBy）：
  类比拓扑排序：blockedBy 是入度，_clear_dependency 是"移除已完成节点"。
  任务完成后，扫描所有文件，从其他任务的 blockedBy 里删掉这个 ID。
  当 blockedBy 变为空列表，任务变为可执行状态。

LLM 通过 6 个工具操作任务：
  task_create / task_list / task_get / task_update / task_add_blocked_by / task_remove_blocked_by

s07 → s08 的演进：任务系统够用了，但任务只能串行，需要后台并发执行。
"""
import json
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

TASKS_DIR = WORKDIR / ".tasks"

SYSTEM = f"You are a coding agent at {WORKDIR}. Use task tools to plan and track work."

# ── TaskManager：持久化任务图，以 JSON 文件存储在 .tasks/ 目录 ─────────────────
# s07 核心设计：任务状态保存到文件系统，而不是 messages[]
# 好处：对话压缩或重启后任务依然存在，不依赖上下文
class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        # 启动时扫描已有任务，确定下一个可用 ID
        self._next_id = self._max_id() + 1

    #关于pathlib的一些函数用法：
    #f.name :完整文件名（名+扩展名）：a.txt
    #f.stem: 去掉扩展名的文件名:a
    #f.suffix:扩展名:.txt
    def _max_id(self) -> int:
        # 从文件名 task_N.json 中提取 N，取最大值
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict:
        # 从磁盘读取单个任务文件，返回 Python dict
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())

    def _save(self, task: dict):
        # 全量写入：把整个 task dict 序列化后覆盖文件，JSON 不支持原地修改字段
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def create(self, subject: str, description: str = "") -> str:
        # 新任务默认 pending，blockedBy 空列表（无依赖）
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending",
            "blockedBy": [], "owner": "",
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def update(
            self, task_id: int, status: str = None, add_blocked_by: list = None,
            remove_blocked_by: list = None) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            if status == "completed":
                # 任务完成时，自动从所有其他任务的 blockedBy 中移除自己
                # 相当于拓扑排序里"删除入度为0的节点后更新其他节点的入度"
                self._clear_dependency(task_id)

        # add/remove_blocked_by 独立于 status，允许单独修改依赖关系
        if add_blocked_by:
            # 用 set 去重，防止同一依赖被重复添加
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if remove_blocked_by:
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def _clear_dependency(self, completed_id: int):
        # 扫描全部任务文件，把 completed_id 从 blockedBy 里移除
        # 读 → 改内存 → 写回，覆盖原文件
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        tasks = []
        # 按 task_N.json 里的 N 排序，保证输出顺序与创建顺序一致
        files = sorted(
            self.dir.glob("task_*.json"),
            key=lambda f: int(f.stem.split("_")[1])
        )
        for f in files:
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        return "\n".join(lines)


TASKS = TaskManager(TASKS_DIR)


# ── 工具实现（与之前 session 相同）────────────────────────────────────────────
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
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

# ── 工具分发表（s07 新增四个 task_* 工具）────────────────────────────────────
# addBlockedBy / removeBlockedBy 用 .get() 取，因为是可选参数，模型可以不传
TOOL_HANDLERS = {
    "bash":        lambda **kw: run_bash(kw["command"]),
    "read_file":   lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":  lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":   lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("addBlockedBy"), kw.get("removeBlockedBy")),
    "task_list":   lambda **kw: TASKS.list_all(),
    "task_get":    lambda **kw: TASKS.get(kw["task_id"]),
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
    # ── s07 新增：任务 CRUD 工具 ──────────────────────────────────────────────
    {
        "name": "task_create",
        "description": "新建一个任务",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject":     {"type": "string"},
                "description": {"type": "string"},  # 可选，详细说明
            },
            "required": ["subject"],
        }
    },
    {
        "name": "task_update",
        "description": "更新任务状态或依赖关系",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status":  {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                # 增量修改依赖：只传想加/想删的 ID，不需要传完整列表
                "addBlockedBy":    {"type": "array", "items": {"type": "integer"}},
                "removeBlockedBy": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["task_id"]   # 其余字段全部可选，模型按需传
        }
    },
    {
        "name": "task_list",
        "description": "展示所有任务和状态总结",
        "input_schema": {"type": "object", "properties": {}}  # 无参数
    },
    {
        "name": "task_get",
        "description": "通过 id 获取任务完整详情",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"]
        }
    }
]

def agent_loop(messages:list):
    while True:
        response = client.messages.create(
            model=MODEL,system = SYSTEM,messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role":"assistant","content":response.content})
        if response.stop_reason != "tool_use":
            return 
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"未知工具：{block.name}"
                except Exception as e:
                    output = f"错误:{e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms07 >> \033[0m")
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