# Day 4 — s04: Subagent

> 格言：*"大任务拆小，每个小任务干净的上下文"*
> 对应文件：`agents/s04_subagent.py`
> 预计时间：2-3 小时

---

## 学习目标

- 理解为什么子 Agent 需要独立的 `messages[]` 数组
- 能看懂 `run_subagent()` 函数，理解它和父循环的关系
- 理解为什么禁止子 Agent 再生成子 Agent（递归禁止）
- 能区分 Subagent（s04）和 Teammate（s09）的根本差异

---

## 核心问题：上下文污染

Agent 工作越久，`messages` 数组越大。举个例子：

任务：分析整个项目，找出所有 TODO 注释

Agent 会读 20 个文件，每个文件读完都会把文件内容放进 messages。20 个文件读完，messages 里可能有 50,000+ token 的文件内容。然后 Agent 继续工作，这些内容一直留在内存里，占用昂贵的上下文空间，而且可能干扰后续推理。

更糟的是：父 Agent 可能只需要最终结论"项目里有 37 个 TODO"，完全不需要看所有中间步骤。

---

## 解决方案：子任务用独立的 messages[]

```
父 Agent                         子 Agent
+------------------+             +------------------+
| messages=[...]   |             | messages=[]      | ← 全新空白
|                  |  委派任务   |                  |
| tool: task       | ──────────> | while tool_use:  |
|   prompt="..."   |             |   读文件、执行   |
|                  |  只返回摘要 |   ...30+次工具   |
|   result = "..." | <────────── | return 最终文本  |
+------------------+             +------------------+
                                 ↑
                          子 Agent 的 messages
                          在返回后直接丢弃
```

父 Agent 的 messages 保持干净，只多了一条 tool_result（子 Agent 的摘要）。

---

## 代码结构解析

### 父 Agent 的 task 工具

```python
PARENT_TOOLS = CHILD_TOOLS + [
    {
        "name": "task",
        "description": "Spawn a subagent with fresh context to handle a subtask.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task for the subagent to complete"
                }
            },
            "required": ["prompt"],
        },
    }
]
```

父 Agent 有 `task` 工具，子 Agent 没有。这是递归禁止的实现：

```python
CHILD_TOOLS = [bash, read_file, write_file, edit_file]  # 没有 task
PARENT_TOOLS = CHILD_TOOLS + [task]                      # 有 task
```

子 Agent 无法再生成子 Agent，否则会有无限递归的风险。

### run_subagent() 函数

```python
def run_subagent(prompt: str) -> str:
    # 子 Agent 有独立的、全新的 messages 数组
    sub_messages = [{"role": "user", "content": prompt}]
    
    for _ in range(30):  # 安全上限：最多 30 轮
        response = client.messages.create(
            model=MODEL,
            system=SUBAGENT_SYSTEM,
            messages=sub_messages,
            tools=CHILD_TOOLS,          # 用子 Agent 的工具集
            max_tokens=8000,
        )
        sub_messages.append({
            "role": "assistant",
            "content": response.content
        })
        
        if response.stop_reason != "tool_use":
            break  # 子 Agent 决定停止
        
        # 执行工具
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)[:50000]
                })
        sub_messages.append({"role": "user", "content": results})
    
    # 提取子 Agent 最后一条消息的文本内容作为摘要
    return "".join(
        b.text for b in response.content if hasattr(b, "text")
    ) or "(no summary)"
    # sub_messages 在函数返回后被 Python 垃圾回收，整个历史消失
```

关键点：
1. `sub_messages = []` 从空开始，不继承父 Agent 的任何历史
2. `for _ in range(30)` 是安全上限，防止子 Agent 陷入无限循环
3. 函数返回时只返回最后一条文本，整个 `sub_messages` 数组被丢弃
4. 父 Agent 收到的只是一段摘要字符串，作为普通 `tool_result`

### dispatch map 里的注册

```python
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "task":       lambda **kw: run_subagent(kw["prompt"]),  # task 也只是一个工具
}
```

`task` 工具从循环的角度看和其他工具没有任何区别——它也是 dispatch map 里的一个条目，返回一个字符串。循环不知道也不关心里面发生了什么。

---

## 上下文隔离的意义

想象父 Agent 需要做 3 件事：
1. 分析项目结构（需要读 20 个文件）
2. 写一个新功能（需要读 3 个文件、写 2 个文件）
3. 写测试（需要读 5 个文件）

没有 Subagent（s01-s03）：
```
父 messages = [user] + [20个文件读取] + [写功能] + [5个文件读取] + [写测试]
→ 最终 messages 里有大量"已完成任务的历史垃圾"
```

用 Subagent（s04）：
```
父 messages = [user] + [subtask摘要1] + [subtask摘要2] + [subtask摘要3]
→ 父 messages 保持极度干净
```

---

## 动手实验

### 实验 1：观察上下文差异

```sh
python agents/s04_subagent.py
```

输入：
```
Use a subtask to find what testing framework this project uses
```

然后输入：
```
Delegate: read all .py files and summarize what each one does
```

注意父 Agent 的 messages 有多少条，对比一下如果不用子 Agent、直接读所有文件会有多少条。

### 实验 2：触发安全上限

把子 Agent 的循环上限改成 3 次：

```python
for _ in range(3):  # 改成 3（原来是 30）
```

然后给一个需要超过 3 步工具调用的子任务，观察当子 Agent 被截断时，父 Agent 收到的摘要是什么。

### 实验 3：改代码练习（必做）

**练习：给子 Agent 加执行日志**

当子 Agent 每次调用工具时，在父 Agent 的控制台打印一行日志：

```python
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    step = 0  # 加这行
    
    for _ in range(30):
        response = client.messages.create(...)
        sub_messages.append(...)
        
        if response.stop_reason != "tool_use":
            break
        
        results = []
        for block in response.content:
            if block.type == "tool_use":
                step += 1
                # 加这行：打印子 Agent 的每一步
                print(f"  [subagent step {step}] {block.name}: {str(block.input)[:60]}")
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input)
                results.append(...)
        
        sub_messages.append(...)
    
    print(f"  [subagent done] {step} steps, returning summary")  # 加这行
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"
```

验证：给一个委派任务，观察控制台里子 Agent 的执行轨迹，对比父 Agent 的 messages 里只有一条 tool_result。

---

## 思考题

1. 子 Agent 的 `sub_messages` 数组在 `run_subagent()` 函数返回后会发生什么？

2. 如果允许子 Agent 也能生成子 Agent，会有什么风险？

3. 父 Agent 能"看到"子 Agent 中间做了什么吗？只能看到什么？

4. 子 Agent 返回的摘要质量取决于什么？如果 LLM 在最后没有做文字总结（直接用工具停止）会发生什么？（提示：看 `or "(no summary)"` 这个处理）

---

## 关键区分：Subagent vs Teammate

| | Subagent（s04）| Teammate（s09）|
|---|---|---|
| 生命周期 | 一次性，用完即毁 | 持久存在，可以复用 |
| 记忆 | 没有跨次记忆 | 有自己的消息历史 |
| 通信 | 直接调用，同步返回 | 通过 mailbox 异步通信 |
| 用途 | 隔离上下文，干净完成子任务 | 持久角色，长期协作 |

现在（s04）用 Subagent，等学到 s09 再回头看这个表格。

---

## 检查点

完成本课后，你应该能回答：

- [ ] 子 Agent 的 messages 数组是从哪里开始的？继承父 Agent 的历史吗？
- [ ] 为什么子 Agent 不能使用 `task` 工具？
- [ ] 父 Agent 最终看到的子 Agent 产出是什么格式？
- [ ] `range(30)` 这个上限是保护什么的？

---

## 与 Hermes 的连接

Hermes 里生成"并行子 Agent"的机制和 s04 的模式一样：每个子 Agent 有独立的上下文，干完活返回摘要。区别是 Hermes 的子 Agent 可以在独立的 worktree 里工作，不会互相污染文件系统（这是 s12 的内容）。
