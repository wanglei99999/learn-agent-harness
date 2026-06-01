"""
s11_autonomous_agents.py — 自主 Agent 团队

s11 在 s10 基础上新增两个机制：
  1. Idle 循环：teammate 干完活不退出，主动轮询任务板找新活
  2. Identity re-injection：上下文压缩后重新注入身份，防止 LLM 失忆

teammate 生命周期：

    spawn（新线程启动）
        │
        ▼
    ┌─────────────────────────────┐
    │  WORK PHASE（最多 50 轮）    │  ← 标准 agent loop，和 s09/s10 相同
    │  读收件箱 → 调LLM → 执行工具 │
    │  stop_reason!=tool_use      │
    │  或 LLM 调 idle → 跳出      │
    └──────────────┬──────────────┘
                   │
    ┌──────────────▼──────────────┐
    │  IDLE PHASE（最多 60 秒）    │  ← s11 新增，teammate 不退出，等活
    │  每 5 秒轮询一次：           │
    │    有收件箱消息 → resume     │
    │    有无主任务   → claim      │
    │    都没有       → 继续等     │
    └──────────────┬──────────────┘
                   │
           resume? ─── No → shutdown（线程退出）
                   │
                  Yes → 回到 WORK PHASE

lead 角色简化：发布任务、处理 inbox，不主动分配——teammate 自己找活干。
"""
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    # 使用第三方兼容 API 时，移除 Anthropic 的默认鉴权 token
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 团队状态和收件箱存放目录
TEAM_DIR = WORKDIR / ".team"       # .team/config.json 记录成员状态
INBOX_DIR = TEAM_DIR / "inbox"     # .team/inbox/<name>.jsonl 每人一个收件箱
TASKS_DIR = WORKDIR / ".tasks"     # .tasks/task_*.json 任务板文件

# ── s11 新增的两个关键常量 ────────────────────────────────────────────────────
# POLL_INTERVAL：IDLE PHASE 每次等待的秒数
# IDLE_TIMEOUT ：IDLE PHASE 最长等待时间，超时说明真的没活，线程退出
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

# lead 系统提示明确说明：teammate 自主，lead 无需逐一分配
SYSTEM = f"You are a team lead at {WORKDIR}. Teammates are autonomous -- they find work themselves."

# 消息类型白名单，send() 会拒绝不在此集合内的类型
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# ── 并发状态追踪（从 s10 继承）────────────────────────────────────────────────
# shutdown_requests / plan_requests：lead 侧字典，key=req_id，记录请求状态
# 多线程同时读写这两个字典时需要加锁
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()

# _claim_lock 专门用于任务认领：
# 多个 teammate 线程可能同时发现同一个无主任务并尝试 claim，
# 加锁保证只有一个人能成功写入 owner
_claim_lock = threading.Lock()


# ── MessageBus：基于文件的异步消息总线（从 s09 继承，未改动）────────────────

class MessageBus:
    """
    每个成员对应 inbox/<name>.jsonl 一个文件。
    send() 追加写入（append），read_inbox() 读完即清空（drain）。
    这种设计让消息传递完全不依赖共享内存，天然支持多线程。
    """

    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            # extra 用于携带协议字段，如 request_id、approve 等
            msg.update(extra)
        # JSONL 格式：每行一条消息，追加写入不会破坏已有消息
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        """
        读取并清空收件箱（drain 语义）。
        每次读完就清空文件，避免重复处理同一条消息。
        返回空列表表示收件箱为空。
        """
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")  # 清空文件，下次读到的是新消息
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        """向所有 teammate 发送同一条消息，跳过自己。"""
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


BUS = MessageBus(INBOX_DIR)


# ── s11 新增：任务板扫描和认领 ────────────────────────────────────────────────

def scan_unclaimed_tasks() -> list:
    """
    扫描 .tasks/ 目录，找出所有可认领的任务。
    条件：status=pending 且没有 owner 且没有 blockedBy（无依赖阻塞）。
    返回列表按文件名排序，即按任务 ID 从小到大。
    """
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    """
    认领任务：将 status 改为 in_progress，写入 owner。

    关键：用 _claim_lock 保证原子性。
    场景：alice 和 bob 同时发现 task_3 无主，同时调用 claim_task(3, ...)，
    没有锁的话两个线程都会读到 owner=None，都以为自己抢到了。
    加锁后只有一个能进入 with 块，另一个等待并发现 owner 已被设置。
    """
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found"
        task = json.loads(path.read_text())
        if task.get("owner"):
            existing_owner = task.get("owner") or "someone else"
            return f"Error: Task {task_id} has already been claimed by {existing_owner}"
        if task.get("status") != "pending":
            status = task.get("status")
            return f"Error: Task {task_id} cannot be claimed because its status is '{status}'"
        if task.get("blockedBy"):
            return f"Error: Task {task_id} is blocked by other task(s) and cannot be claimed yet"
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2))
    return f"Claimed task #{task_id} for {owner}"


def make_identity_block(name: str, role: str, team_name: str) -> dict:
    """
    构造身份注入消息块（s11 新增机制）。

    问题背景：
      长时间运行后，LLM 的上下文会被压缩（compaction），
      messages 列表变短，LLM 可能忘记自己是谁、在哪个团队。

    解决方案：
      在 messages 过短时（<= 3 条），在列表开头插入一条身份说明，
      让 LLM 重新确认自己的角色和名字，然后继续工作。

    注意：插入的是 role="user" 的消息，模拟"系统提醒"，
    紧接着插入一条 role="assistant" 的确认回复，
    保证 user/assistant 交替的消息格式不被破坏。
    """
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


# ── TeammateManager：管理 teammate 线程和状态 ─────────────────────────────────

class TeammateManager:
    """
    管理所有 teammate 的生命周期。
    配置持久化到 .team/config.json，线程对象存在内存的 self.threads 字典中。
    """

    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}  # name → Thread 对象（内存中，重启后丢失）

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        """每次状态变更后立刻写盘，保证重启后能看到正确状态。"""
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        """在 members 列表中按名字查找成员，不存在返回 None。"""
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        """更新成员状态并立刻持久化，让外部能通过 /team 实时看到状态变化。"""
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """
        启动一个 teammate 线程。

        daemon=True 的含义：
          主进程（lead）退出时，所有 daemon 线程自动结束，
          不需要手动 join，避免程序卡住等待子线程。

        重复 spawn 同名 teammate 时：
          如果状态是 idle 或 shutdown（说明上次线程已结束），
          允许重新启动；否则拒绝（线程还在跑）。
        """
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)

        self._save_config()
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _loop(self, name: str, role: str, prompt: str):
        """
        teammate 的核心循环，在独立线程中运行。

        结构：while True 包裹两个阶段
          - WORK PHASE（内层 for 循环）：执行任务
          - IDLE PHASE（轮询循环）：等待新任务

        两个阶段交替运行，直到：
          - IDLE PHASE 超时（resume=False）→ 线程退出
          - 任意阶段收到 shutdown_request  → 线程退出
        """
        team_name = self.config["team_name"]
        # sys_prompt 告知 LLM 两件事：
        #   1. 自己是谁（name/role/team）
        #   2. 没活时调 idle 工具，会自动认领新任务
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle tool when you have no more work. You will auto-claim new tasks."
        )
        # messages 在整个线程生命周期内持续积累，跨 WORK/IDLE 保持连续上下文
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()

        while True:
            # ════════════════════════════════════════════════════════════════
            # WORK PHASE：标准 agent loop，最多 50 轮
            # 每轮：读收件箱 → 调 LLM → 执行工具 → 循环
            # 退出条件：① LLM 停止调工具  ② LLM 主动调 idle
            # ════════════════════════════════════════════════════════════════
            for _ in range(50):
                # 每轮开头先读收件箱，有消息立即处理
                # shutdown_request 不经 LLM，直接退出（s11 简化了 s10 的审批流程）
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return  # 线程结束
                    # 其他消息（broadcast、普通消息等）作为 user 消息塞进对话
                    messages.append({"role": "user", "content": json.dumps(msg)})

                try:
                    response = client.messages.create(
                        model=MODEL,
                        system=sys_prompt,
                        messages=messages,
                        tools=tools,
                        max_tokens=8000,
                    )
                except Exception:
                    # API 调用失败（网络问题等），进入 idle 等待而不是直接崩溃
                    self._set_status(name, "idle")
                    return

                messages.append({"role": "assistant", "content": response.content})

                # stop_reason != "tool_use" 说明 LLM 认为本轮工作结束
                # 跳出 for 循环，进入 IDLE PHASE
                if response.stop_reason != "tool_use":
                    break

                results = []
                idle_requested = False  # 标记本轮是否有 idle 工具调用
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            # idle 是信号工具，不做实际操作
                            # 只设标记，让外层 if 判断后 break 出 WORK PHASE
                            idle_requested = True
                            output = "Entering idle phase. Will poll for new tasks."
                        else:
                            # 普通工具：bash、read_file、send_message 等
                            output = self._exec(name, block.name, block.input)
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                # 把所有工具结果一次性返回给 LLM（下一轮的 user 消息）
                messages.append({"role": "user", "content": results})

                if idle_requested:
                    break  # LLM 主动 idle → 退出 WORK PHASE

            # ════════════════════════════════════════════════════════════════
            # IDLE PHASE：轮询等待新任务（s11 核心新增）
            # 每 POLL_INTERVAL 秒检查一次，最多等 IDLE_TIMEOUT 秒
            # 找到活 → resume=True → 回到 WORK PHASE
            # 超时    → resume=False → 线程退出
            # ════════════════════════════════════════════════════════════════
            self._set_status(name, "idle")
            resume = False
            # polls = 总等待时间 / 每次间隔 = 最多轮询次数
            # 例：60 // 5 = 12 次，每次 5 秒，共等 60 秒
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)

            for _ in range(polls):
                time.sleep(POLL_INTERVAL)  # 等一个间隔再检查

                # 优先级 1：有人给我发消息 → 立刻恢复工作
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break  # 跳出轮询，回到 WORK PHASE

                # 优先级 2：任务板有无主任务 → 主动认领
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    task = unclaimed[0]  # 取第一个（ID 最小的）
                    result = claim_task(task["id"], name)
                    if result.startswith("Error:"):
                        # 被其他线程抢先，继续等待下一轮
                        continue

                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )

                    # ── Identity re-injection ────────────────────────────
                    # 判断依据：messages 条数 <= 3 说明对话历史很短，
                    # 可能是上下文压缩（compaction）后只剩摘要，
                    # LLM 可能已经不知道自己是谁了。
                    # 解决：在 messages 最前面插入身份说明 + 一条 assistant 确认，
                    # 保证 user/assistant 交替格式合法。
                    if len(messages) <= 3:
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})

                    # 把认领的任务追加为新的对话轮次，让 LLM 接着处理
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break  # 跳出轮询，回到 WORK PHASE

                # 优先级 3：什么都没有 → 继续等下一轮

            # 轮询结束，判断是否找到了新活
            if not resume:
                # IDLE_TIMEOUT 秒内没有任何消息和任务，自动退出
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")
            # while True 继续，回到 WORK PHASE 顶部

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        """
        工具分发器（teammate 专用）。
        sender 参数用于路由消息：发消息时知道"我是谁"，读收件箱时知道读谁的。
        用 if/else 而非字典是因为 sender 是动态的，每次调用都不同。
        """
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            # sender 作为发件人，让收件方知道是谁发的
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            # 读自己的收件箱，sender 就是自己的名字
            return json.dumps(BUS.read_inbox(sender), indent=2)
        if tool_name == "shutdown_response":
            # teammate 响应 lead 的 shutdown 请求：
            #   1. 更新内存状态表（让 lead 调 _check_shutdown_status 时能看到）
            #   2. 发消息到 lead 收件箱（让 lead 的 LLM 能读到）
            req_id = args["request_id"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if args["approve"] else "rejected"
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": args["approve"]},
            )
            return f"Shutdown {'approved' if args['approve'] else 'rejected'}"
        if tool_name == "plan_approval":
            # teammate 提交计划给 lead 审批：
            #   生成 req_id → 记录到 plan_requests → 发到 lead 收件箱
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for approval."
        if tool_name == "claim_task":
            # LLM 也可以主动调工具认领任务（与 IDLE PHASE 自动认领是两种路径）
            return claim_task(args["task_id"], sender)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        """
        teammate 可用工具列表。
        与 lead 相比，多了：
          - idle：主动进入等待状态
          - claim_task：主动从任务板认领任务
        少了：
          - spawn_teammate、list_teammates、broadcast（管理权限归 lead）
        """
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "shutdown_response", "description": "Respond to a shutdown request.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "Submit a plan for lead approval.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "Claim a task from the task board by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# ── 基础工具实现（从 s02 继承，未改动）──────────────────────────────────────────

def _safe_path(p: str) -> Path:
    """
    将相对路径解析为绝对路径，并验证不超出 WORKDIR。
    防止 LLM 传入 "../../etc/passwd" 之类的路径逃逸攻击。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str) -> str:
    """执行 shell 命令，屏蔽危险操作，输出截断到 50000 字符。"""
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


# ── lead 侧协议函数（从 s10 继承）────────────────────────────────────────────

def handle_shutdown_request(teammate: str) -> str:
    """
    lead 向 teammate 发送关闭请求。
    同时记录到 shutdown_requests，让 lead 可以后续查询状态。
    注意：s11 的 teammate 收到 shutdown_request 后直接退出，不需要等 approve。
    """
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """
    lead 审批 teammate 提交的计划。
    更新内存状态表，并把审批结果发回 teammate 收件箱。
    """
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    """lead 查询某个 shutdown 请求的当前状态（pending/approved/rejected）。"""
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


# ── lead 工具分发表 ──────────────────────────────────────────────────────────
# 用字典 + lambda 而非 if/else，因为 lead 身份固定（sender 永远是 "lead"）
TOOL_HANDLERS = {
    "bash":              lambda **kw: _run_bash(kw["command"]),
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":        lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":         lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":    lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":    lambda **kw: TEAM.list_all(),
    "send_message":      lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":        lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":         lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":              lambda **kw: "Lead does not idle.",  # lead 不 idle，只是为了工具列表统一
    "claim_task":        lambda **kw: claim_task(kw["task_id"], "lead"),
}

# lead 可用工具定义（JSON Schema 格式，传给 Anthropic API）
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check shutdown request status.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "Enter idle state (for lead -- rarely used).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "Claim a task from the board by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


def agent_loop(messages: list):
    """
    lead 的主循环（与 s09/s10 基本相同，新增了每轮注入 inbox）。

    s11 对 lead 的改动只有一处：
      每轮 LLM 调用前先读 lead 自己的收件箱，
      把 teammate 发来的消息（plan_approval、shutdown_response 等）
      包装成 <inbox> 标签注入对话，lead LLM 可以据此做决策。
    """
    while True:
        # 注入收件箱：让 lead LLM 看到 teammate 的回报
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return  # LLM 认为本轮对话结束，返回给用户
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


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 内置调试指令，不经过 LLM，直接查看状态
        if query.strip() == "/team":
            print(TEAM.list_all())   # 查看所有 teammate 的状态
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))  # 查看 lead 收件箱
            continue
        if query.strip() == "/tasks":
            # 以 [ ]/[>]/[x] 可视化任务状态，@owner 显示认领人
            TASKS_DIR.mkdir(exist_ok=True)
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
                owner = f" @{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
