# Day 8 — s08: Background Tasks

> 格言：*"慢操作丢后台，Agent 继续想下一步"*
> 对应文件：`agents/s08_background_tasks.py`
> 预计时间：2-3 小时

---

## 学习目标

- 理解为什么阻塞式循环在处理慢操作时浪费了 Agent 的时间
- 理解守护线程 + 通知队列的并发模型
- 知道通知在哪个时机被注入到 messages
- 理解"主循环单线程，I/O 并行"的设计意图

---

## 核心问题：Agent 在等待时什么都不能做

有些命令很慢：
- `npm install`：可能 2-5 分钟
- `docker build`：可能 5-10 分钟
- `pytest`（大型测试套件）：可能 3 分钟

在 s01-s07 的阻塞式循环里：

```
用户："安装依赖，然后写配置文件"

Agent:
  1. 调用 bash("npm install")
  2. 等待...等待...等待...（3分钟）
  3. 收到结果
  4. 调用 write_file("config.json", ...)
  5. 完成

总时间：3分钟 + 几秒
```

如果可以并行：

```
Agent:
  1. 调用 background_run("npm install")  → 立即返回，后台跑
  2. 调用 write_file("config.json", ...)  → 立即执行
  3. 做其他准备工作...
  4. 收到通知：npm install 完成
  5. 继续下一步

总时间：max(3分钟, 其他工作时间) ≈ 3分钟（重叠执行）
```

---

## 并发模型：主循环单线程

这是关键设计决策：**主 Agent 循环仍然是单线程的**。

并行发生在哪里？只在 I/O 层：子进程在独立的守护线程里跑，结果通过线程安全的队列回到主线程。

```
主线程（Agent Loop）          守护线程 1         守护线程 2
─────────────────────        ──────────────     ──────────────
spawn("npm install") ──────> 运行 npm install
spawn("sleep 5")    ──────>                     运行 sleep 5
做其他工作
...
每轮循环开始：
  drain_notifications()
  ← 得到 "sleep 5 完成"
  ← 没有 npm 结果（还没好）
继续下一轮循环
...
drain_notifications()
  ← 得到 "npm install 完成"
```

---

## 代码结构解析

### BackgroundManager

```python
class BackgroundManager:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self._notification_queue: list[dict] = []
        self._lock = threading.Lock()  # 保护 _notification_queue 的线程安全访问

    def run(self, command: str) -> str:
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {
            "status": "running",
            "command": command,
            "started_at": time.time(),
        }
        # 创建守护线程，daemon=True 确保主程序退出时线程也退出
        thread = threading.Thread(
            target=self._execute,
            args=(task_id, command),
            daemon=True
        )
        thread.start()
        return f"Background task {task_id} started: {command}"

    def _execute(self, task_id: str, command: str):
        # 这个函数在守护线程里执行
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed" if r.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "failed"
        
        # 更新任务状态
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["output"] = output
        
        # 把通知放入队列（线程安全）
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "result": output[:500]  # 通知里只放摘要
            })

    def drain_notifications(self) -> list[dict]:
        # 原子地取出并清空所有待处理通知
        with self._lock:
            notifs = self._notification_queue[:]
            self._notification_queue.clear()
        return notifs
```

### 循环集成：通知注入时机

```python
def agent_loop(messages: list):
    while True:
        # ← 每轮 LLM 调用之前，先排空通知队列
        notifs = BG.drain_notifications()
        if notifs:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}"
                for n in notifs
            )
            # 把通知作为一条 user 消息注入
            messages.append({
                "role": "user",
                "content": f"<background-results>\n{notif_text}\n</background-results>"
            })
        
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # ...工具执行...
```

**通知注入的位置**：在每次 LLM 调用**之前**检查队列。这意味着：

- 后台任务完成后，不会立即打断 Agent
- 只有等到 Agent 结束当前轮次、下一轮开始时，才能看到结果
- 这是合理的：不需要实时中断，批量处理通知更高效

---

## 动手实验

### 实验 1：观察并行执行

```sh
python agents/s08_background_tasks.py
```

输入：
```
Start 3 background tasks: "sleep 2", "sleep 4", "sleep 6". While they run, create a file called status.txt.
```

观察：
- Agent 立即开始写文件，不等待 sleep 命令
- 每隔一段时间，通知出现在 messages 里（按任务完成顺序，而不是启动顺序）

### 实验 2：追踪通知时机

在 `drain_notifications()` 调用处加日志：

```python
notifs = BG.drain_notifications()
if notifs:
    print(f"[DEBUG] 本轮收到 {len(notifs)} 个后台通知")
    for n in notifs:
        print(f"  - task {n['task_id']}: {n['status']}")
```

观察通知是在哪一轮循环里被 Agent 收到的（不一定是任务完成后的第一轮）。

### 实验 3：改代码练习（必做）

**练习：添加 background_status 改进**

当前 `check_background` 工具显示所有任务状态。改进它，让它显示"等待中的任务数"和"运行时间"：

```python
def check_background(self) -> str:
    if not self.tasks:
        return "No background tasks."
    
    lines = []
    now = time.time()
    running = sum(1 for t in self.tasks.values() if t["status"] == "running")
    
    lines.append(f"Background tasks ({running} running, {len(self.tasks)} total):")
    
    for task_id, info in self.tasks.items():
        elapsed = now - info.get("started_at", now)
        status = info["status"]
        
        if status == "running":
            lines.append(f"  [{task_id}] RUNNING ({elapsed:.0f}s elapsed): {info['command']}")
        elif status == "completed":
            lines.append(f"  [{task_id}] DONE: {info['command']}")
            lines.append(f"    Output: {info.get('output', '')[:100]}")
        else:
            lines.append(f"  [{task_id}] {status.upper()}: {info['command']}")
    
    return "\n".join(lines)
```

验证改进有效：启动几个耗时不同的任务，然后连续查询状态，观察运行时间的变化。

---

## 思考题

1. `daemon=True` 参数的作用是什么？如果不设置为 True，会发生什么？

2. 通知队列用 `with self._lock` 保护，但 `self.tasks` 字典的读写没有加锁。为什么通知队列需要加锁，而 tasks 字典不需要（或者说不需要特别处理）？

3. 如果一个后台任务花了 10 分钟，但 Agent 在这 10 分钟里一直在干别的事，一直循环，通知会积累在队列里吗？`drain_notifications` 调用时会一次性拿到 10 分钟里积累的所有通知吗？

4. 后台任务的输出在通知里只保留了 500 字符，完整输出存在哪里？（看 `self.tasks[task_id]["output"]`）Agent 如果需要完整输出怎么办？

---

## 检查点

完成本课后，你应该能回答：

- [ ] 主循环是单线程还是多线程的？
- [ ] 后台任务完成后，结果在什么时机被注入到 messages？
- [ ] `daemon=True` 的线程有什么特性？
- [ ] 为什么通知队列需要用锁保护？

---

## 与 Hermes 的连接

Hermes 的 cron 调度系统用了相同的思路：定时任务在后台运行，完成后通过通知机制告知主 Agent。Hermes 的心跳机制（每 30 秒唤醒 Agent 检查是否有任务）也是同样的"不阻塞主循环"设计。理解了 s08 的守护线程 + 通知队列，Hermes 的 cron 模块你一眼就能读懂。
