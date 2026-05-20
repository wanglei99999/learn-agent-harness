# Day 11 — s12: Worktree Isolation + 总复习

> 格言：*"各干各的目录，互不干扰"*
> 对应文件：`agents/s12_worktree_task_isolation.py`、`agents/s_full.py`
> 预计时间：3-4 小时

---

## 学习目标

- 理解为什么多 Agent 并行工作需要目录隔离
- 理解 Task ID 和 git worktree 如何绑定
- 跑 `s_full.py` 感受所有机制整合后的完整体验
- 做总复习：从 s01 到 s12，每个 harness 机制一句话总结

---

## s12 核心问题：文件系统竞争

到 s11，多个 Agent 可以并行工作了。但它们都在同一个目录里操作文件。

场景：Agent Alice 和 Agent Bob 都在重构不同模块，但：

```
Alice 修改了 utils.py（某一行）
Bob 同时也修改了 utils.py（另一行）
→ 后一个写入覆盖前一个
→ 两人的工作都部分丢失
```

更糟的是：未提交的修改互相污染。Alice 运行测试时，看到的是 Bob 未完成的改动，测试莫名其妙失败。

**根本矛盾**：任务板管"做什么"（逻辑层），但不管"在哪做"（物理层）。

---

## 解决方案：Task ID 绑定 git Worktree

```
控制平面（.tasks/）              执行平面（.worktrees/）
+--------------------+           +------------------------+
| task_1.json        |           | auth-refactor/         |
|   status: active   | <──────>  |   branch: wt/auth      |
|   worktree: "auth" |           |   task_id: 1           |
+--------------------+           +------------------------+
| task_2.json        |           | ui-login/              |
|   status: active   | <──────>  |   branch: wt/ui-login  |
|   worktree: "ui"   |           |   task_id: 2           |
+--------------------+           +------------------------+
```

每个任务分配一个独立的 git worktree 目录，用任务 ID 把两者绑定。

**什么是 git worktree？**

git worktree 允许同一个 git 仓库在**多个目录**里同时被 checkout，每个目录对应不同的分支。

```sh
git worktree add .worktrees/alice-work wt/alice-branch
# 现在 .worktrees/alice-work/ 是完整的工作目录
# 和主目录完全独立，改了一边不影响另一边
```

Alice 在 `.worktrees/alice-work/` 里干活，Bob 在 `.worktrees/bob-work/` 里干活，互不干扰。干完了，各自提交，最后 merge。

---

## 状态机的扩展

s12 给 Worktree 引入了独立的状态机，和 Task 状态机配合：

```
Task 状态机：      pending → in_progress → completed
Worktree 状态机：  absent  → active      → removed | kept
```

两个状态机通过任务 ID 绑定，转换协调进行：
- Task 进入 in_progress → 自动创建 Worktree（absent → active）
- Task 完成 → Worktree 可以 removed 或 kept（用于 review）

---

## 代码结构解析（重点看设计，不需要逐行）

### WorktreeManager

```python
class WorktreeManager:
    def __init__(self, worktrees_dir: Path):
        self.dir = worktrees_dir
        self.index_path = self.dir / "index.json"
        self.events_path = self.dir / "events.jsonl"
        self.dir.mkdir(exist_ok=True)
        self._load_index()
    
    def create(self, name: str, task_id: int) -> str:
        branch = f"wt/{name}"
        path = self.dir / name
        
        # 创建 git worktree
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(path)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return f"Error: {result.stderr}"
        
        # 更新索引
        self.index[name] = {
            "name": name,
            "task_id": task_id,
            "branch": branch,
            "path": str(path),
            "status": "active",
        }
        self._save_index()
        
        # 记录生命周期事件
        self._log_event("created", name, task_id)
        
        return f"Worktree '{name}' created at {path} on branch {branch}"
    
    def remove(self, name: str, keep: bool = False) -> str:
        if keep:
            # 保留目录，只更新状态（用于 code review）
            self.index[name]["status"] = "kept"
            self._save_index()
            return f"Worktree '{name}' kept for review"
        
        # 彻底删除
        subprocess.run(["git", "worktree", "remove", "--force", name])
        del self.index[name]
        self._save_index()
        return f"Worktree '{name}' removed"
```

### 生命周期事件日志

```python
def _log_event(self, event_type: str, name: str, task_id: int):
    event = {
        "timestamp": time.time(),
        "event": event_type,   # created / removed / kept
        "worktree": name,
        "task_id": task_id,
    }
    with open(self.events_path, "a") as f:
        f.write(json.dumps(event) + "\n")
```

这是一个 append-only 的事件日志，记录 worktree 的完整生命周期历史。这个模式（事件流）是分布式系统的核心设计模式之一。

---

## 动手实验

### 实验 1：跑 s12，观察 worktree 创建

```sh
python agents/s12_worktree_task_isolation.py
```

创建 2 个任务，让它们都进入 in_progress 状态，观察：
1. `.worktrees/` 目录里出现了 2 个独立的工作目录
2. `git worktree list` 命令显示多个 worktree
3. 两个目录里的文件是完全独立的

### 实验 2：跑 s_full.py（最重要）

```sh
python agents/s_full.py
```

这是所有 12 个机制合一的版本。给它一个复杂的多步任务：

```
Create a Python library with:
1. A main module with 3 utility functions
2. A test file for each function
3. A README documenting the library
Use multiple agents to work on different parts in parallel
```

观察它如何综合运用：TodoWrite、子 Agent、上下文压缩、任务图、后台任务、Agent 团队。

---

## 总复习：12 个机制，每个一句话

把下面的表格填完，这是今天最重要的练习：

| 课程 | 格言 | 加了什么机制 | 解决了什么问题 |
|------|------|------------|--------------|
| s01 | One loop & Bash is all you need | Agent Loop（while + stop_reason） | 没有循环，你就是那个循环 |
| s02 | 加一个工具，只加一个 handler | dispatch map | 加工具不改循环 |
| s03 | 没有计划的 Agent 走哪算哪 | TodoManager + nag reminder | 多步任务中 Agent 偏航 |
| s04 | 大任务拆小，每个小任务干净的上下文 | Subagent（独立 messages[]）| 上下文污染 |
| s05 | 用到什么知识，临时加载什么知识 | Skill 两层注入 | 知识全塞 system prompt 太贵 |
| s06 | 上下文总会满，要有办法腾地方 | 三层压缩（micro/auto/manual）| 长对话上下文溢出 |
| s07 | 大目标要拆成小任务，排好序，记在磁盘上 | 持久化任务图（DAG）| 扁平清单不够，状态不持久 |
| s08 | 慢操作丢后台，Agent 继续想下一步 | 守护线程 + 通知队列 | 阻塞等待浪费时间 |
| s09 | 任务太大一个人干不完，要能分给队友 | JSONL mailbox + 持久 Teammate | 单 Agent 无法并行协作 |
| s10 | 队友之间要有统一的沟通规矩 | 结构化协议 + FSM | 自由文本通信脆弱 |
| s11 | 队友自己看看板，有活就认领 | 空闲轮询 + 自主认领 | Lead 成为分配瓶颈 |
| s12 | 各干各的目录，互不干扰 | git worktree + Task ID 绑定 | 并行 Agent 文件竞争 |

---

## 进入 Hermes 的路线图

完成 s01-s12 后，按这个顺序阅读 Hermes 代码：

```
第一步：找到心脏
  hermes-agent/agent/
  → 找 agent loop（while + stop_reason）
  → 对应 s01

第二步：找工具系统
  hermes-agent/tools/
  → 找 dispatch map 或类似结构
  → 对应 s02

第三步：找技能系统
  hermes-agent/skills/
  hermes-agent/optional-skills/
  → 看 skill 怎么加载，怎么注入
  → 对应 s05

第四步：找定时/后台
  hermes-agent/cron/
  → 找心跳机制和任务调度
  → 对应 s08 + s11

第五步：找消息网关
  hermes-agent/gateway/
  → 找各平台消息队列
  → 对应 s09（mailbox 的生产版）

第六步：找上下文管理
  hermes-agent/agent/ 里找上下文处理
  → 对应 s06
```

每找到一个对应点，你就多了一份理解。不熟悉的部分（self-learning loop、FTS5 搜索、Soul 人格系统），在这个基础上去看文档，不会再感到完全陌生。

---

## 最后的思考

12 节课走完，循环本身从来没变过：

```python
while True:
    response = llm(messages, tools)
    if response.stop_reason != "tool_use":
        return
    for tool_call in response.content:
        results.append(execute(tool_call))
    messages.append(results)
```

这 10 行代码是每一个 Agent 的骨架。

你加的所有机制——规划、隔离、压缩、持久化、并发、协作——都是在这个骨架**外面**搭建的。它们是 harness。骨架本身，是模型。

**造好 Harness。Agent 会完成剩下的。**
