import json
import os
import subprocess
import threading
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

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

SYSTEM = f"You are a team lead at {WORKDIR}. Spawn teammates and communicate via inboxes."

# s09 支持的消息类型，send 时会校验，不在列表里的直接报错
VALID_MSG_TYPES={
    "message",              # 普通消息
    "broadcast",            # 广播给所有人
    "shutdown_request",     # 请求关闭（s10 用）
    "shutdown_response",    # 关闭确认（s10 用）
    "plan_approval_response", # 计划审批（s10 用）
}

# ── MessageBus：文件中转站，每个 agent 一个 JSONL inbox ──────────────────────
# s09 核心设计：agent 之间不直接调用，而是通过文件传消息
# 发消息 = append 一行到对方的 .jsonl 文件
# 收消息 = 读取自己的 .jsonl 文件，然后清空（drain）
# 好处：异步，发送方不需要等接收方在线
class MessageBus:
    def __init__(self, inbox_dir:Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender:str, to:str, content:str,
             msg_type:str="message", extra:dict=None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}, valid:{VALID_MSG_TYPES}'"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time()
        }
        if extra:
            msg.update(extra)
        # append 模式：每条消息一行，不覆盖历史消息
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name:str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        # 读完后清空文件（drain），避免消息被重复处理
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender:str, content:str, teammates:list) -> str:
        count = 0
        for name in teammates:
            if name != sender:  # 不给自己发
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"

BUS = MessageBus(INBOX_DIR)

# ── TeammateManager：管理持久化 teammate 的生命周期 ──────────────────────────
# s04 subagent：用完即销毁，parent 拿到返回值就结束
# s09 teammate：持续存活，空闲时等消息，收到消息后继续工作
# 状态流转：working → idle（任务完成）→ working（收到新任务）→ shutdown（关闭）
class TeammateManager:
    def __init__(self, team_dir:Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        # 团队配置持久化到文件，重启后成员信息不丢失
        self.config = self._load_config()
        self.threads = {}   # name → Thread，只在内存里，重启后线程需重新 spawn

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name:str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name:str, role:str, prompt:str) -> str:
        member = self._find_member(name)
        if member:
            # 已存在的成员：只有 idle/shutdown 状态才能重新激活
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        # daemon=True：主进程退出时线程自动终止
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"spawned '{name}' (role: {role})"

    def _teammate_loop(self, name:str, role:str, prompt:str):
        # 每个 teammate 有自己独立的 system prompt，与 lead 的不同
        sys_prompt = (
            f"你是{name}，你的角色是{role}，你只能工作在{WORKDIR}及其子目录。"
            f"使用send_message去与其他人交流，完成你的任务。"
        )
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()

        # range(50) 设上限防止失控，和 s04 subagent 一样的安全设计
        for _ in range(50):
            # 每轮先读自己的 inbox，把新消息注入到 messages 里
            # 这是 teammate 感知外部世界（其他人发来的消息）的唯一方式
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                messages.append({"role": "user", "content": json.dumps(msg)})
            try:
                response = client.messages.create(
                    model=MODEL, system=sys_prompt, messages=messages,
                    tools=tools, max_tokens=8000,
                )
            except Exception:
                break
            messages.append({"role": "assistant", "content": response.content})
            # 模型不再调用工具，说明当前任务完成，退出循环进入 idle
            if response.stop_reason != "tool_use":
                break
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    output = self._exec(name, block.name, block.input)
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    })
            messages.append({"role": "user", "content": results})

        # 循环结束 → 标记为 idle，等待下一次 spawn 激活
        member = self._find_member(name)
        if member and member["status"] != "shutdown":
            member["status"] = "idle"
            self._save_config()

    def _exec(self, sender:str, tool_name:str, args:dict) -> str:
        # teammate 的工具分发：send_message/read_inbox 以 sender 身份操作 BUS
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            # sender 传入，确保消息的 from 字段是 teammate 自己的名字
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2)
        return f"未知工具：{tool_name}"
    
    def _teammate_tools(self)->list:
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name":"send_message", "description":"发送消息给同事。",
             "input_schema":{"type":"object","properties":{"to":{"type":"string"},"content":{"type":"string"},"msg_type":{"type":"string","enum":list(VALID_MSG_TYPES)}},"required":["to","content"]}
             },
             {
                 "name":"read_inbox",
                 "description":"读取和取出你的信箱。",
                 "input_schema":{
                     "type":"object",
                     "properties":{}
                 }
             },
        ]
    def list_all(self)->str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"{m['name']}({m['role']}):{m['status']}")
        return "\n".join(lines)
    
    def member_names(self)->list:
        return [m["name"] for m in self.config["members"]]
    
TEAM = TeammateManager(TEAM_DIR)

def _safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def _run_read(path: str, limit: int = None) -> str:
    try:
        lines = _safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def _run_write(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"
    

TOOL_HANDLERS = {
    "bash":            lambda **kw: _run_bash(kw["command"]),
    "read_file":       lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":      lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":       lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":  lambda **kw: TEAM.spawn(kw['name'],kw['role'],kw['prompt']),
    "list_teammates":  lambda **kw: TEAM.list_all(),
    "send_message":    lambda **kw: BUS.send("lead",kw["to"],kw["content"],kw.get("msg_type","message")),
    "read_inbox":      lambda **kw:json.dumps(BUS.read_inbox("lead"),indent=2),
    "broadcast":       lambda **kw:BUS.broadcast("lead",kw['content'],TEAM.member_names()),
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
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate that runs in its own thread.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates with name, role, status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
]

# ── Agent 主循环（lead）──────────────────────────────────────────────────────
# lead 和 teammate 的循环结构几乎一样，区别是：
#   - lead 每轮读自己的 inbox，把消息注入后再调用 API
#   - teammate 在 _teammate_loop 里做同样的事，但跑在独立线程
def agent_loop(messages:list):
    while True:
        # 每轮先 drain lead 的 inbox，把 teammate 发来的消息注入给模型
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"
            })
        response = client.messages.create(
            model= MODEL,
            system = SYSTEM,
            messages= messages,
            tools = TOOLS,
            max_tokens=8000,
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
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        messages.append({"role": "user", "content": results})

if __name__=="__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() =="/team":
            print(TEAM.list_all())
            continue
        if query.strip() =="/inbox":
            print(json.dumps(BUS.read_inbox("lead"),indent=2))
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()