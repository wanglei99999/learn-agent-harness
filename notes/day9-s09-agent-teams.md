# Day 9 — s09: Agent Teams

> 格言：*"任务太大一个人干不完，要能分给队友"*
> 对应文件：`agents/s09_agent_teams.py`
> 预计时间：3 小时

---

## 学习目标

- 彻底理解 Subagent（s04）和 Teammate（s09）的区别
- 理解基于文件的 JSONL mailbox 通信机制
- 能看懂 `TeammateManager` 和线程如何协作
- 理解为什么用文件而不是内存队列做通信

---

## Subagent vs Teammate：根本差异

在学 s09 之前，先彻底搞清楚这个区分：

| | Subagent（s04）| Teammate（s09）|
|---|---|---|
| **生命周期** | 被调用时生成，完成后消亡 | 启动后一直存在，等待任务 |
| **状态** | 无跨次状态 | 有自己的消息历史，保持上下文 |
| **通信方式** | 直接函数调用，同步 | 通过 mailbox 文件，异步 |
| **适用场景** | 隔离上下文的一次性子任务 | 需要多轮交互的持久角色 |
| **类比** | 请一个临时工做一件事 | 雇一个长期员工，持续协作 |

---

## 核心问题：Subagent 做不到的事

场景：你要同时做 3 个不相关的重构：
- Agent Alice：重构认证模块
- Agent Bob：重构缓存系统
- Agent Carol：重构数据库层

这 3 件事可以完全并行，但彼此之间需要偶尔同步（"Bob，你的缓存接口改了吗？我的数据库层要用它"）。

用 Subagent（s04）：
- Alice、Bob、Carol 各自是一次性调用，没有持久身份
- 没有办法让 Bob 在工作中途和 Carol 通信
- 父 Agent 必须协调所有信息传递，成为瓶颈

用 Teammate（s09）：
- Alice、Bob、Carol 是持久运行的 Agent，有各自的线程
- 可以互相发消息（通过 mailbox）
- 父 Agent（lead）只需要分配任务，不需要管中间细节

---

## 通信机制：JSONL Mailbox

```
.team/
  config.json          ← 团队名册（谁是团队成员，各自状态）
  inbox/
    alice.jsonl        ← Alice 的收件箱（append-only，读完清空）
    bob.jsonl          ← Bob 的收件箱
    lead.jsonl         ← 领导的收件箱
```

**发消息**（发送方）：

```python
def send_message(recipient: str, sender: str, content: str, msg_type: str = "message"):
    inbox_path = INBOX_DIR / f"{recipient}.jsonl"
    msg = {
        "id": str(uuid.uuid4())[:8],
        "from": sender,
        "to": recipient,
        "type": msg_type,
        "content": content,
        "timestamp": time.time(),
    }
    with open(inbox_path, "a") as f:
        f.write(json.dumps(msg) + "\n")
```

**读消息**（接收方）：

```python
def read_inbox(name: str) -> list[dict]:
    inbox_path = INBOX_DIR / f"{name}.jsonl"
    if not inbox_path.exists():
        return []
    lines = inbox_path.read_text().strip().splitlines()
    msgs = [json.loads(line) for line in lines if line.strip()]
    # 读完即清空（drain on read）
    inbox_path.write_text("")
    return msgs
```

**为什么用文件而不是内存队列？**

- 持久性：程序重启后消息还在
- 跨进程：将来可以让 Agent 跑在不同进程甚至不同机器上
- 可观察：直接打开 `.team/inbox/` 目录就能看通信内容，调试方便
- 简单：不需要任何网络基础设施，一个文件就是一个队列

---

## 代码结构解析

### TeammateManager

```python
class TeammateManager:
    def __init__(self):
        self.config_path = TEAM_DIR / "config.json"
        self.teammates: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._load_config()
    
    def spawn(self, name: str, role: str, system_prompt: str) -> str:
        if name in self.teammates:
            return f"Teammate {name} already exists"
        
        self.teammates[name] = {
            "name": name,
            "role": role,
            "status": "idle",
            "system_prompt": system_prompt,
        }
        self._save_config()
        
        # 为这个 Teammate 创建收件箱文件
        inbox_path = INBOX_DIR / f"{name}.jsonl"
        inbox_path.touch()
        
        # 启动线程
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name,),
            daemon=True
        )
        self._threads[name] = thread
        thread.start()
        
        return f"Teammate {name} spawned as {role}"
```

### Teammate 的内部循环

每个 Teammate 在自己的线程里运行一个 Agent 循环，持续检查收件箱：

```python
def _teammate_loop(self, name: str):
    teammate = self.teammates[name]
    messages = []  # 每个 Teammate 有自己的消息历史
    
    while teammate.get("status") != "shutdown":
        # 检查收件箱
        inbox_msgs = read_inbox(name)
        
        if not inbox_msgs:
            time.sleep(1)  # 没有消息，等待 1 秒再检查
            continue
        
        # 把收到的消息追加到 Teammate 的 messages 里
        for msg in inbox_msgs:
            messages.append({
                "role": "user",
                "content": f"[Message from {msg['from']}]: {msg['content']}"
            })
        
        # 更新状态
        teammate["status"] = "working"
        self._save_config()
        
        # 运行 Agent Loop（和 s01 一样的循环）
        while True:
            response = client.messages.create(
                model=MODEL,
                system=teammate["system_prompt"],
                messages=messages,
                tools=CHILD_TOOLS,
                max_tokens=8000,
            )
            messages.append({"role": "assistant", "content": response.content})
            
            if response.stop_reason != "tool_use":
                break
            
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    output = TOOL_HANDLERS[block.name](**block.input)
                    results.append({...})
            messages.append({"role": "user", "content": results})
        
        # 工作完成，回到 idle 状态，发通知
        teammate["status"] = "idle"
        send_message("lead", name, "Task completed. Ready for next assignment.")
```

---

## 动手实验

### 实验 1：创建团队并观察 mailbox

```sh
python agents/s09_agent_teams.py
```

输入：
```
Spawn a teammate named alice with the role of coder
```

然后：
```
Send alice a message: "Please create a file called hello.py with a hello() function"
```

在另一个终端窗口（或文件管理器），打开 `.team/inbox/` 目录，实时观察 `alice.jsonl` 文件的变化：
- 发消息后，文件里出现一行 JSON
- Alice 处理完后，文件被清空
- `lead.jsonl` 里出现 Alice 的回复

### 实验 2：观察 config.json 的状态变化

```sh
# 在另一个终端持续监控
# Windows:
Get-Content .team/config.json -Wait
```

观察 Alice 的 `status` 字段在 `idle` → `working` → `idle` 之间的变化。

### 实验 3：改代码练习（必做）

**练习：添加广播功能**

实现一个 `broadcast` 工具，让 Lead 可以同时给所有 Teammate 发消息：

```python
def broadcast_message(sender: str, content: str) -> str:
    recipients = [name for name in TEAM_MANAGER.teammates.keys() if name != sender]
    if not recipients:
        return "No teammates to broadcast to."
    
    for recipient in recipients:
        send_message(recipient, sender, content, msg_type="broadcast")
    
    return f"Broadcast sent to: {', '.join(recipients)}"
```

注册工具，验证：Spawn 两个 Teammate，然后 Lead 广播一条任务，两个 Teammate 都应该收到并处理。

---

## 思考题

1. 如果 Alice 的收件箱里同时有 5 条未处理消息，`_teammate_loop` 会怎么处理？是一条一条处理，还是一次全处理？

2. 两个 Teammate 能直接互相发消息吗？（看 `send_message` 函数，它对 sender/recipient 有限制吗？）

3. Teammate 的 `messages` 数组也会越来越大。s09 的代码处理这个问题了吗？如果没有，应该怎么处理？

4. 如果 Teammate Alice 的线程崩溃（Python 异常），Lead 会知道吗？如何让异常不会静默失败？

---

## 检查点

完成本课后，你应该能回答：

- [ ] Subagent 和 Teammate 的最核心区别是什么？
- [ ] JSONL mailbox 用什么操作发消息、读消息？
- [ ] 为什么用文件而不是内存队列做团队通信？
- [ ] Teammate 的主循环在哪个线程里运行？

---

## 与 Hermes 的连接

Hermes 的多平台消息网关（gateway/）就是一个高级版的 mailbox 系统。来自 Telegram、Discord、Slack 的消息，都先进入各自的消息队列，再被分发给 Agent 处理。你现在理解的 JSONL mailbox 模式，就是 Hermes gateway 的核心思想。
