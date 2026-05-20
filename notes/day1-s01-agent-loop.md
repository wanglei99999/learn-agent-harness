# Day 1 — s01: Agent Loop

> 格言：*"One loop & Bash is all you need"*
> 对应文件：`agents/s01_agent_loop.py`
> 预计时间：2-3 小时

---

## 学习目标

- 理解 Agent 的最小工作单元是什么
- 能看懂并手写 `agent_loop()` 函数
- 知道 `stop_reason` 是什么，为什么它是循环退出的条件
- 理解 `messages[]` 数组是怎么随着对话增长的

---

## 核心问题：没有循环，你自己就是那个循环

想象这样一个场景：你让 GPT 帮你写代码，它给了你代码，你粘到编辑器里，发现报错，再把报错粘回给 GPT，它再给你修复方案……

你在手动做的这个"粘贴-反馈-再粘贴"的过程，就是 Agent Loop。它只是把这件事自动化了。

---

## 代码结构解析

打开 `agents/s01_agent_loop.py`，从上往下读。

### 第一部分：工具定义（第 54-62 行）

```python
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]
```

这是告诉 LLM："你有一个叫 `bash` 的工具，可以运行 shell 命令，需要传一个 `command` 字符串参数。"

LLM 看到这个定义后，当它需要执行某个操作时，会返回一个结构化的 tool_use 请求，而不是自己瞎编命令结果。

### 第二部分：工具执行（第 65-77 行）

```python
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
```

注意几个设计决策：
- **危险命令拦截**：黑名单检查，这是最简单的安全边界
- **输出截断到 50000 字符**：防止一个命令的输出把上下文撑爆
- **超时 120 秒**：防止命令卡死整个 Agent

### 第三部分：核心循环（第 81-101 行）

```python
def agent_loop(messages: list):
    while True:
        # 1. 把当前所有消息 + 工具定义发给 LLM
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 2. 把 LLM 的回复追加进消息列表
        messages.append({"role": "assistant", "content": response.content})

        # 3. 如果 LLM 没有调用工具，结束循环
        if response.stop_reason != "tool_use":
            return

        # 4. 执行 LLM 要求调用的工具，收集结果
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                print(output[:200])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output
                })

        # 5. 把工具结果作为 user 消息追加，回到第 1 步
        messages.append({"role": "user", "content": results})
```

**关键：messages 数组是整个对话的完整历史**

每一轮循环结束后，`messages` 里多了两条：
- LLM 的回复（assistant）
- 工具执行结果（user）

下一轮 LLM 调用时，它能看到所有历史，知道自己做过什么、结果是什么，然后决定下一步。

---

## messages 数组的增长过程

用一个具体例子追踪：

```
用户输入："Create a file called hello.py"

初始 messages:
[
  {"role": "user", "content": "Create a file called hello.py"}
]

第 1 轮循环后：
[
  {"role": "user",      "content": "Create a file called hello.py"},
  {"role": "assistant", "content": [tool_use(bash, "echo '' > hello.py")]},
  {"role": "user",      "content": [tool_result("(no output)")]},
]

第 2 轮循环（LLM 验证文件创建成功）：
[
  ...上面的 3 条...
  {"role": "assistant", "content": [tool_use(bash, "cat hello.py")]},
  {"role": "user",      "content": [tool_result("")]},
]

第 3 轮循环（LLM 认为任务完成，返回文本）：
[
  ...上面的 5 条...
  {"role": "assistant", "content": [TextBlock("File hello.py created.")]},
]
→ stop_reason != "tool_use"，循环退出
```

---

## 动手实验

### 实验 1：跑起来

```sh
cd learn-claude-code
python agents/s01_agent_loop.py
```

依次输入这些任务，观察 Agent 的行为：

```
Create a file called hello.py that prints "Hello, World!"
List all Python files in this directory
What is the current git branch?
Create a directory called test_output and write 3 files in it
```

观察控制台输出：黄色的是 Agent 执行的 bash 命令，命令下面是输出结果。

### 实验 2：观察 messages 增长

在 `agent_loop` 函数的 while 循环开头加一行：

```python
def agent_loop(messages: list):
    while True:
        print(f"[DEBUG] 本轮循环，messages 共 {len(messages)} 条")  # 加这行
        response = client.messages.create(...)
```

然后给一个需要 5 步以上的任务，观察每轮循环 messages 数量的增长。

### 实验 3：改代码练习（必做）

**练习**：给 `run_bash` 的危险命令黑名单加两条你认为危险的命令。

```python
dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/",
             "你加的命令1", "你加的命令2"]  # 加在这里
```

然后测试：在 Agent 里输入一个包含你加的命令的任务，确认被拦截，并看到 "Error: Dangerous command blocked" 返回给 LLM 后，LLM 会怎么反应。

**练习 2**：把超时时间从 120 秒改成 5 秒，然后让 Agent 执行 `sleep 10`，观察超时错误怎么处理。

---

## 思考题

1. `stop_reason` 有哪些可能的值？为什么退出条件用 `!= "tool_use"` 而不是 `== "end_turn"`？

2. 如果 LLM 在一次回复里同时调用了 3 个工具（tool_use），循环会执行几次？（提示：看 `for block in response.content` 这个循环）

3. `tool_use_id` 是什么？为什么 tool_result 里要带这个字段？

4. 如果 `run_bash` 抛出了一个没有被捕获的异常，整个 Agent 会怎样？应该怎么改？

---

## 检查点

完成本课后，你应该能回答：

- [ ] Agent Loop 的退出条件是什么？
- [ ] `messages` 数组里 assistant 消息和 user 消息分别是什么？
- [ ] 工具结果是以什么角色（role）追加到消息列表的？
- [ ] 不改循环，能加新工具吗？（s02 会回答这个问题）

---

## 与 Hermes 的连接

Hermes Agent 的 `agent/` 目录里有同样的 while 循环结构。当你以后读 Hermes 代码时，先找这个循环，找到 `stop_reason` 的判断，你就找到了 Hermes 的心脏。
