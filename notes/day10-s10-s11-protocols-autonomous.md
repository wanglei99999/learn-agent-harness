# Day 10 — s10 + s11: Protocols & Autonomous Agents

> s10 格言：*"队友之间要有统一的沟通规矩"*
> s11 格言：*"队友自己看看板，有活就认领"*
> 对应文件：`agents/s10_team_protocols.py`、`agents/s11_autonomous_agents.py`
> 预计时间：2-3 小时

---

## 学习目标

- 理解为什么需要结构化协议（不能只发自由文本）
- 理解 FSM（有限状态机）在协议里的作用
- 理解"自主认领"模式：Agent 主动看任务板，不需要人分配
- 能把这两节课的概念对应到 Hermes 里的相应机制

---

## s10：团队协议

### 核心问题：自由文本通信的脆弱性

s09 的 Teammate 通过 mailbox 发消息，但消息内容是自由文本。问题：

```
Lead 发给 Alice：
"I need you to approve the shutdown so we can stop"

Alice 可能回复：
"OK sure"
"Yeah that's fine"
"Approved"
"Yes I agree with the shutdown plan"
```

这 4 种回复语义相同，但代码要怎么判断 Alice 是否批准了？用字符串匹配？太脆弱。

解决方案：定义结构化消息类型，每种类型有固定的字段格式。

### 5 种消息类型

```python
MSG_TYPES = {
    "message":                 # 普通文本消息
        {"content": "str"},
    
    "broadcast":               # 广播（发给所有人）
        {"content": "str"},
    
    "shutdown_request":        # 请求关机
        {"reason": "str"},
    
    "shutdown_response":       # 关机确认/拒绝
        {"approved": "bool", "reason": "str"},
    
    "plan_approval_response":  # 计划审批确认/拒绝
        {"approved": "bool", "feedback": "str"},
}
```

Agent 收到 `shutdown_response` 时，只需检查 `msg["approved"]` 字段，不用猜测文本含义。

### FSM：关机流程

关机不是"发一条消息就关"。需要多方确认：

```
状态机：
                    send shutdown_request
                           ↓
RUNNING ────────────> SHUTDOWN_PENDING
                           ↓
                   所有 Teammate 回复 shutdown_response
                           ↓
                      检查所有 approved == True？
                      ↙              ↘
                   Yes               No（有人反对）
                    ↓                  ↓
                SHUTDOWN           RUNNING（取消关机）
```

这个状态机防止了"一个 Agent 在关键任务进行中被强制关机"的问题。

```python
class ShutdownFSM:
    def __init__(self, team_members: list[str]):
        self.state = "running"
        self.pending_approvals = set(team_members)
        self.responses = {}
    
    def request_shutdown(self, reason: str):
        if self.state != "running":
            return "Shutdown already in progress"
        self.state = "shutdown_pending"
        # 向所有成员发送 shutdown_request
        for member in self.pending_approvals:
            send_message(member, "lead", reason, msg_type="shutdown_request")
        return f"Shutdown requested, waiting for {len(self.pending_approvals)} approvals"
    
    def receive_response(self, from_agent: str, approved: bool, reason: str):
        self.responses[from_agent] = {"approved": approved, "reason": reason}
        self.pending_approvals.discard(from_agent)
        
        if not self.pending_approvals:  # 所有人都回复了
            if all(r["approved"] for r in self.responses.values()):
                self.state = "shutdown"
                return "All approved. Shutting down."
            else:
                self.state = "running"
                rejected_by = [k for k, v in self.responses.items() if not v["approved"]]
                return f"Shutdown rejected by: {rejected_by}"
        
        return f"Waiting for {len(self.pending_approvals)} more responses"
```

### 学习重点

你不需要把 s10 的代码改得多复杂。**重点是理解 FSM 思想**：
- 系统有有限个状态（running, shutdown_pending, shutdown）
- 消息触发状态转换
- 每个状态有合法的转换路径

这个模式在 Hermes 里到处都有。

---

## s11：自主 Agent

### 核心问题：领导成为瓶颈

s09-s10 的团队模型里，任务分配需要 Lead 主动发消息给每个 Teammate。

如果有 10 个 Teammate 和 100 个任务，Lead 需要：
1. 查任务板，找未分配的任务
2. 评估哪个 Teammate 适合做这个任务
3. 给那个 Teammate 发消息
4. 等待确认
5. 重复...

Lead 成了分配瓶颈。而且 Lead 在分配时，Teammate 们在干等。

### 解决方案：自主认领

换一个模式：**任务板是公开的，Teammate 自己看，自己认领。**

```
任务板（.tasks/ 目录）：
  #1: 重构认证模块 [pending]
  #2: 优化缓存系统 [pending]
  #3: 写数据库迁移 [pending]

Alice（空闲）：
  1. 查 task_available → 看到 #1, #2, #3
  2. 认领 #1（更新 task owner 为 alice，status 为 in_progress）
  3. 开始工作

Bob（空闲，同时）：
  1. 查 task_available → 看到 #2, #3（#1 已被 Alice 认领）
  2. 认领 #2
  3. 开始工作
```

不需要 Lead 协调，Agent 们自组织。

### 空闲轮询：Teammate 的工作节律

每个 Teammate 在 idle 状态下，定期检查"有没有活可干"：

```python
def _teammate_loop(self, name: str):
    teammate = self.teammates[name]
    messages = []
    
    while teammate.get("status") != "shutdown":
        # 1. 先检查收件箱
        inbox_msgs = read_inbox(name)
        
        if not inbox_msgs:
            # 2. 没有直接消息，看看有没有可以认领的任务
            available_tasks = TASK_MANAGER.get_available_unowned()
            
            if available_tasks:
                # 3. 认领第一个可用任务
                task = available_tasks[0]
                TASK_MANAGER.update(
                    task["id"],
                    status="in_progress",
                    owner=name
                )
                # 把任务加入 messages，开始工作
                messages.append({
                    "role": "user",
                    "content": f"New task assigned: #{task['id']} - {task['subject']}\n{task.get('description', '')}"
                })
            else:
                # 4. 没有任务，等待
                time.sleep(2)
                continue
        else:
            # 有直接消息，处理消息
            for msg in inbox_msgs:
                messages.append({...})
        
        # 5. 运行 Agent Loop
        teammate["status"] = "working"
        # ...执行工具调用...
        teammate["status"] = "idle"
```

### 学习重点

- **`get_available_unowned()`**：返回 status=pending 且 owner="" 的任务——这就是 Day 7 练习里你加的 `task_available` 工具的作用
- **竞争条件**：两个 Agent 同时看到同一个任务，同时认领，会怎样？（s11 的教学实现里没有完全处理这个问题，s12 用 worktree 从另一角度解决了文件竞争）
- **自组织**：不需要中央协调，任务自然地被空闲的 Agent 认领

---

## 动手实验

### 实验 1：跑 s10，看关机协议

```sh
python agents/s10_team_protocols.py
```

Spawn 2 个 Teammate，然后：
```
Request a shutdown
```

观察 FSM 的状态变化，和 Teammate 的批准/拒绝响应。

### 实验 2：跑 s11，观察自主认领

```sh
python agents/s11_autonomous_agents.py
```

用 task 工具创建 5 个任务（不指定 owner），然后 Spawn 3 个 Teammate。

观察：3 个 Teammate 是否自动分配到不同的任务，而不是都扑向同一个任务。

### 思考实验（不需要跑代码）

**对应到 Hermes**：

| s10/s11 概念 | Hermes 对应机制 |
|---|---|
| 结构化消息类型 | Hermes 的消息协议（各平台消息统一格式化） |
| 关机 FSM | Hermes 的 Agent 生命周期管理 |
| 自主认领任务 | Hermes 的 heartbeat：每 30 秒醒来，检查是否有事要做 |
| 空闲轮询 | Hermes 的 cron 调度器 |

---

## 两节课的核心收获

s10 教你：**通信需要协议，协议需要状态机**

s11 教你：**好的系统设计让参与者自组织，不需要中央调度**

这两个设计原则不只是 Agent 系统的专利，它们是分布式系统的通用原则。

---

## 检查点

完成本课后，你应该能回答：

- [ ] 为什么自由文本通信在多 Agent 系统里不够？结构化消息解决了什么问题？
- [ ] FSM 里的"状态"和"转换"分别是什么？关机流程的 3 个状态是？
- [ ] 自主认领模式下，Lead 的职责变成什么了？
- [ ] "竞争条件"在自主认领里指的是什么情况？

---

## 与 Hermes 的连接

Hermes 的心跳机制是 s11 自主轮询的生产级实现：每 30 秒，Agent 被唤醒，执行"检查有没有事情要做"的逻辑。如果有，立即处理；没有，继续睡。这是一个完整的、在生产环境中运行的自主 Agent 模式。你在 s11 里看到的 `time.sleep(2)` 和 `get_available_unowned()` 的组合，就是 Hermes heartbeat 的教学版。
