import os
import subprocess
import threading
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use background_run for long-running commands."

# ── BackgroundManager：后台任务管理器 ─────────────────────────────────────────
# s08 核心设计：把耗时命令（如 npm install、pytest）放到后台线程执行
# 模型不需要等待，立刻拿到 task_id 继续做其他事，任务完成后收到通知
#
# 对比 bash 工具（阻塞）：
#   bash       → 运行命令 → 等待完成 → 返回结果    （期间模型无法做其他事）
#   background → 启动线程 → 立刻返回 task_id → 任务在后台跑
class BackgroundManager:
    def __init__(self):
        # task_id → {status, result, command} 的内存字典
        self.tasks = {}
        # 完成通知队列：线程完成后把结果推入，agent_loop 每轮取出注入给模型
        self._notification_queue = []
        # Lock 保护队列的并发读写：主线程读、子线程写，必须加锁防止竞态
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        # uuid4 生成随机唯一 ID，取前8位够用且易读
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        # daemon=True：主进程退出时后台线程自动销毁，不会阻止程序退出
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        # 立刻返回，模型继续下一步，不等命令完成
        return f"后台任务 {task_id} 已启动：{command[:80]}"

    def _execute(self, task_id: str, command: str):
        """在子线程里执行命令，完成后把结果写入 tasks 并推送通知。"""
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "error: timeout(300s)"
            status = "timeout"
        except Exception as e:
            output = f"error: {e}"
            status = "error"

        # 写入任务状态（子线程写，主线程读，此处不加锁因为 dict 单键赋值在 CPython 是原子的）
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"

        # 推送通知到队列，加锁保护（列表 append 不是原子操作）
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(no result)")[:500],
            })

    def check(self, task_id: str = None) -> str:
        """查看单个任务详情，或不传 task_id 列出所有任务。"""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"错误：未识别的任务 {task_id}"
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> list:
        # 取出并清空所有待处理通知
        # agent_loop 每轮调用一次，把通知注入到 tool_result 里告知模型
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs
    
BG = BackgroundManager()

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
    
# ── 工具分发表（s08 新增两个后台工具）────────────────────────────────────────
# background_run / check_background 背后是 BG 实例，模型感知不到线程细节
TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "background_run":   lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id"))   # task_id 可选，不传则列出所有
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command (blocking).",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {
        "name":"background_run",
        "description":"运行后台线程，立刻返回task_id",
        "input_schema":{
            "type":"object",
            "properties":{"command":{"type":"string"}},
            "required":["command"]            
        }
    },
        {
        "name":"check_background",
        "description":"检查后台任务状态，不填task_id就会列出所有任务。",
        "input_schema":{
            "type":"object",
            "properties":{"task_id":{"type":"string"}},       
        }
    },
]

# ── Agent 主循环（s08 新增：每轮开始前注入后台任务通知）────────────────────────
def agent_loop(messages: list):
    while True:
        # s08 新增：每轮 API 调用前，先取出后台线程完成的通知
        # drain_notifications 会清空队列，避免同一条通知被重复注入
        notifs = BG.drain_notifications()
        if notifs and messages:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            # 以 user 消息形式注入，用 XML 标签标识来源，让模型能区分这是系统通知
            # messages 非空才注入，避免第一条消息是通知而不是用户问题
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})

        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})

if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
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


