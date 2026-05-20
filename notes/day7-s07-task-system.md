# Day 7 — s07: Task System

> 格言：*"大目标要拆成小任务，排好序，记在磁盘上"*
> 对应文件：`agents/s07_task_system.py`
> 预计时间：3 小时

---

## 学习目标

- 理解 DAG（有向无环图）任务图和 s03 扁平清单的本质区别
- 能看懂 `TaskManager` 的 CRUD 实现和依赖解除逻辑
- 理解任务持久化到磁盘的意义（活过 auto_compact）
- 理解 s07 的任务图为什么是后续多 Agent 协作的骨架

---

## 核心问题：扁平清单不够用

s03 的 TodoManager 是一个内存里的扁平列表：

```
[ ] 步骤1
[ ] 步骤2
[ ] 步骤3
```

**问题 1：没有依赖关系**

"步骤2 必须在步骤1完成后才能开始"——列表表达不了这个。Agent 可能会乱序执行。

**问题 2：没有并行信息**

"步骤2 和步骤3 可以同时做"——列表也表达不了这个。只能串行。

**问题 3：只活在内存里**

s06 的 auto_compact 一跑，对话历史被替换成摘要，TodoManager 的状态消失了。

**问题 4：多个 Agent 无法共享**

s09-s12 要让多个 Agent 协作，它们需要一个共享的任务板来协调"谁在做什么"。内存里的列表做不到。

---

## 解决方案：持久化任务图（DAG）

```
.tasks/
  task_1.json  → {"id": 1, "status": "completed", "blockedBy": []}
  task_2.json  → {"id": 2, "status": "pending",   "blockedBy": [1]}
  task_3.json  → {"id": 3, "status": "pending",   "blockedBy": [1]}
  task_4.json  → {"id": 4, "status": "pending",   "blockedBy": [2, 3]}

任务图：
         +--------+
    +--> | task 2 | --+
    |    | pending|   |
+-------+             +--> +--------+
| task 1|                  | task 4 |
| done  |                  | blocked|
+-------+             +--> +--------+
    |    +--------+   |
    +--> | task 3 | --+
         | pending|
         +--------+

顺序：task 1 必须先完成
并行：task 2 和 task 3 可以同时开始（都只等 task 1）
等待：task 4 要等 2 和 3 都完成
```

**关键**：每个任务都是磁盘上的一个 JSON 文件。程序重启、上下文压缩、不同 Agent 实例——只要磁盘在，任务图就在。

---

## 代码结构解析

### TaskManager

```python
class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",    # 初始状态
            "blockedBy": [],        # 初始无依赖
            "owner": "",            # s09 会用到这个字段
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2)

    def _save(self, task: dict):
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2))

    def _load(self, task_id: int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        return json.loads(path.read_text())
```

### 依赖解除（自动传播）

```python
def _clear_dependency(self, completed_id: int):
    # 遍历所有任务，把已完成任务的 ID 从 blockedBy 里移除
    for f in self.dir.glob("task_*.json"):
        task = json.loads(f.read_text())
        if completed_id in task.get("blockedBy", []):
            task["blockedBy"].remove(completed_id)
            self._save(task)
            # 如果移除后 blockedBy 变空，该任务自动解锁

def update(self, task_id: int, status: str = None,
           add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
    task = self._load(task_id)
    
    if status:
        task["status"] = status
        if status == "completed":
            self._clear_dependency(task_id)  # 完成时自动解除下游依赖
    
    if add_blocked_by:
        task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
    
    if remove_blocked_by:
        task["blockedBy"] = [x for x in task["blockedBy"]
                             if x not in remove_blocked_by]
    
    self._save(task)
    return json.dumps(task, indent=2)
```

### task_list：让 Agent 看全局

```python
def list_all(self) -> str:
    tasks = []
    for f in sorted(self.dir.glob("task_*.json")):
        tasks.append(json.loads(f.read_text()))
    
    if not tasks:
        return "No tasks."
    
    lines = []
    for t in sorted(tasks, key=lambda x: x["id"]):
        blocked = f" [blocked by: {t['blockedBy']}]" if t["blockedBy"] else ""
        owner = f" [owner: {t['owner']}]" if t.get("owner") else ""
        lines.append(
            f"[{t['status'][:4]}] #{t['id']}: {t['subject']}{blocked}{owner}"
        )
    return "\n".join(lines)
```

Agent 调用 `task_list` 得到的输出类似：

```
[comp] #1: Setup project
[pend] #2: Write code [blocked by: [1]]
[pend] #3: Write tests [blocked by: [1]]
[pend] #4: Deploy [blocked by: [2, 3]]
```

一眼看出哪些可以开始，哪些在等待。

---

## 动手实验

### 实验 1：创建并操作任务图

```sh
python agents/s07_task_system.py
```

依次输入：

```
Create 3 tasks: "Setup project", "Write code", "Write tests". Make "Write code" and "Write tests" both depend on "Setup project".
```

然后：
```
List all tasks and show the dependency graph
```

再：
```
Complete task 1 and then list tasks to see tasks 2 and 3 unblocked
```

然后**直接打开** `.tasks/` 目录，看 JSON 文件的内容是否和 Agent 报告的一致。

### 实验 2：观察依赖传播

创建这样的任务图：
```
parse → transform
parse → emit
transform + emit → test
```

然后完成 parse，观察 transform 和 emit 的 `blockedBy` 字段是否自动变空。

在 `_clear_dependency` 里加 print 来追踪：

```python
def _clear_dependency(self, completed_id: int):
    for f in self.dir.glob("task_*.json"):
        task = json.loads(f.read_text())
        if completed_id in task.get("blockedBy", []):
            print(f"[dep] task {task['id']} 解除对 task {completed_id} 的依赖")
            task["blockedBy"].remove(completed_id)
            self._save(task)
```

### 实验 3：改代码练习（必做）

**练习：添加 `task_available` 工具**

这个工具返回所有"当前可以开始的任务"（status=pending 且 blockedBy=[]）：

```python
def available(self) -> str:
    tasks = []
    for f in sorted(self.dir.glob("task_*.json")):
        task = json.loads(f.read_text())
        if task["status"] == "pending" and not task.get("blockedBy"):
            tasks.append(task)
    
    if not tasks:
        return "No available tasks (all pending tasks are blocked or in progress)."
    
    lines = [f"Available tasks ({len(tasks)} total):"]
    for t in tasks:
        lines.append(f"  #{t['id']}: {t['subject']}")
    return "\n".join(lines)
```

注册到 TOOL_HANDLERS 和 TOOLS 列表，验证工作正常。

这个工具在 s11 的"自主认领"机制里非常重要——Agent 通过这个工具知道"哪些活没人干"。

---

## 思考题

1. `_clear_dependency` 在每次任务完成时遍历**所有**任务文件。如果有 1000 个任务，性能会怎样？有更好的数据结构吗？

2. 如果两个 Agent 同时读、改同一个 task JSON 文件会怎样（竞争条件）？s07 处理了这个问题吗？（这在 s12 里解决）

3. `owner` 字段在 s07 里是空字符串，它在哪里被用到？（看 s09 的 Agent Teams）

4. 如果任务 A blockedBy 任务 B，任务 B blockedBy 任务 A（循环依赖），会发生什么？TaskManager 防御了这种情况吗？

---

## 检查点

完成本课后，你应该能回答：

- [ ] 任务图存在哪里？程序重启后还在吗？
- [ ] 完成一个任务后，依赖它的下游任务会发生什么变化？
- [ ] `blockedBy: []` 意味着什么？
- [ ] s07 的任务图为什么是 s09-s12 多 Agent 协作的基础？

---

## 与 Hermes 的连接

Hermes 的 cron 调度系统和任务队列底层都用了类似的持久化 JSON + 状态机设计。任务图的 DAG 思路在 Hermes 里体现为"技能依赖技能"的链式执行。你现在理解的这个任务状态机（pending → in_progress → completed），在 Hermes 里被进化成了更复杂的多状态机。
