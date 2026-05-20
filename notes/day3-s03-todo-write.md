# Day 3 — s03: TodoWrite

> 格言：*"没有计划的 Agent 走哪算哪"*
> 对应文件：`agents/s03_todo_write.py`
> 预计时间：2-3 小时

---

## 学习目标

- 理解为什么多步任务中 Agent 会偏航
- 理解 TodoManager 的约束设计（同时只有一个 in_progress）
- 知道 nag reminder 是什么、注入到哪里、为什么注入在那里
- 能判断什么情况下用 Todo，什么情况下不需要

---

## 核心问题：模型在长对话里会失忆

你让 Agent 做一个 10 步的重构任务：

```
1. 分析代码结构
2. 提取公共函数
3. 重命名变量
4. 添加类型注解
5. 写单元测试
6. 更新文档
7. 检查导入
8. 格式化代码
9. 运行测试
10. 提交 git
```

Agent 开始干了。做完前 3 步后，它执行的 3 次工具调用、3 次返回结果，都进了 `messages` 数组。现在 `messages` 里已经有不少内容了。

**问题**：系统提示说"做 10 步"，但每一轮新的 LLM 调用，系统提示的权重被越来越多的工具调用历史稀释了。LLM 可能做完步骤 3 之后，直接去做它认为最重要的事，而不是按顺序做步骤 4。

**更深层的问题**：LLM 没有一个持续更新的、可见的"进度板"——它只能从消息历史里推断自己做了什么，这是不可靠的。

---

## 解决方案：给 Agent 一块白板

TodoManager 就是一块白板，贴在 Agent 每次思考的时候都能看到的地方。

白板规则：
- 任务分三种状态：`pending`（未开始）、`in_progress`（进行中）、`completed`（已完成）
- **同时只能有一个 `in_progress`**（这是强制约束，不是建议）
- 每做完一步，Agent 必须更新白板，否则系统会催它

---

## 代码结构解析

### TodoManager（第 36-65 行附近）

```python
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        validated, in_progress_count = [], 0
        for item in items:
            status = item.get("status", "pending")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({
                "id": item["id"],
                "text": item["text"],
                "status": status
            })
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            icon = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(
                item["status"], "[ ]"
            )
            lines.append(f"{icon} {item['id']}: {item['text']}")
        return "\n".join(lines)
```

`update()` 接收整个任务列表（而不是单条更新），每次都完整替换。这样 LLM 不容易犯"忘记更新某一条"的错误。

**"同时只能有一个 in_progress"** 这个约束：
- 防止 LLM 幻觉性地把多个任务都标记为进行中
- 强制 LLM 聚焦在一件事上
- 让状态板保持清晰可读

### Nag Reminder（催促机制）

```python
rounds_since_todo = 0  # 全局计数器

def agent_loop(messages: list):
    global rounds_since_todo
    while True:
        # 如果 Agent 连续 3 轮没更新 todo，注入提醒
        if rounds_since_todo >= 3 and messages:
            last = messages[-1]
            if last["role"] == "user" and isinstance(last.get("content"), list):
                last["content"].insert(0, {
                    "type": "text",
                    "text": "<reminder>Update your todos.</reminder>",
                })

        response = client.messages.create(...)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        called_todo = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "todo":
                    called_todo = True
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({...})

        # 更新计数器
        if called_todo:
            rounds_since_todo = 0
        else:
            rounds_since_todo += 1

        messages.append({"role": "user", "content": results})
```

**注入位置**：`<reminder>` 被插入到最新一条 user 消息（工具结果）的最前面。

为什么是这个位置？
- system prompt 在每次调用都会发送，加在这里会占用 token，且对所有轮都生效
- 插入到最近的 user 消息里，只在当前轮生效，且位于 LLM 注意力最集中的位置（最新消息）
- 这是一个"即时注入"，不污染对话历史的其他部分

---

## 完整流程示意

```
用户："重构 hello.py"
    ↓
LLM 调用 todo(创建 5 个步骤，步骤1标为 in_progress)
    ↓
LLM 调用 bash(执行步骤1)
    ↓ rounds_since_todo = 1
LLM 调用 bash(再做一些工作)
    ↓ rounds_since_todo = 2
LLM 调用 bash(又做了些工作)
    ↓ rounds_since_todo = 3
→ 下一轮循环开始前，<reminder> 注入到消息里
LLM 看到提醒，调用 todo(步骤1标为 completed，步骤2标为 in_progress)
    ↓ rounds_since_todo = 0（重置）
...继续
```

---

## 动手实验

### 实验 1：跑起来，给复杂任务

```sh
python agents/s03_todo_write.py
```

输入一个需要多步的任务：
```
Refactor the file hello.py: add type hints, docstrings, and a main guard
```

或者：
```
Create a Python package with __init__.py, utils.py, and tests/test_utils.py
```

观察：
- Agent 是否在开始工作前先列出了 todo
- `[>]` 标记有没有在任务之间移动
- `[x]` 最终标记了哪些任务

### 实验 2：观察 nag 触发

把 nag 阈值从 3 改成 1：

```python
if rounds_since_todo >= 1 and messages:  # 改成 1
```

再给 Agent 同样的任务，观察每次工具调用后都会看到 `<reminder>`。

然后改成 10，感受 Agent 不被催促时会怎样漂移。

### 实验 3：改代码练习（必做）

**练习：让 todo 工具的输出更丰富**

当前 `render()` 只显示状态图标和文字，改成同时显示"完成进度"：

```python
def render(self) -> str:
    if not self.items:
        return "No todos."
    
    total = len(self.items)
    done = sum(1 for item in self.items if item["status"] == "completed")
    
    lines = [f"Progress: {done}/{total}"]  # 加这一行
    lines.append("-" * 20)
    
    for item in self.items:
        icon = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(
            item["status"], "[ ]"
        )
        lines.append(f"{icon} {item['id']}: {item['text']}")
    return "\n".join(lines)
```

验证修改有效：给任务并让 Agent 开始做，todo 工具的返回值里现在应该有进度信息。

---

## 思考题

1. 为什么是"同时只能有一个 `in_progress`"而不是"同时可以有多个"？联系 s04 的 subagent 概念思考：什么时候需要并行？

2. `<reminder>` 标签注入在 user 消息里，LLM 会把它误解为真实用户说的话吗？这个设计有什么风险？

3. 如果 Agent 完成任务后没有把所有 todo 标记为 `completed`，会有什么问题？

4. s03 的 TodoManager 只活在内存里，上下文压缩（s06）或程序重启后会消失。这个问题在哪一课被解决？（提示：看 s07）

---

## 检查点

完成本课后，你应该能回答：

- [ ] nag reminder 是被注入到 system prompt 还是 user 消息里？
- [ ] "同时只能有一个 in_progress" 这个约束是在哪里强制执行的？
- [ ] `rounds_since_todo` 在什么情况下重置为 0？
- [ ] TodoManager 的状态是持久化的吗？程序重启后还在吗？

---

## 与 Hermes 的连接

Hermes 的 skill 系统里有类似的"工作进度追踪"机制，但实现更复杂，持久化到了数据库。你理解了 s03 的 nag 注入模式，就理解了 Hermes 里"如何在对话中途插入系统信息"的设计思路。
