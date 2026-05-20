# Day 6 — s06: Context Compact

> 格言：*"上下文总会满，要有办法腾地方"*
> 对应文件：`agents/s06_context_compact.py`
> 预计时间：2-3 小时

---

## 学习目标

- 理解为什么长对话必然导致上下文溢出
- 理解三层压缩策略各自解决什么问题
- 能看懂 `micro_compact`、`auto_compact` 函数
- 理解压缩后信息去了哪里（没有真正消失）

---

## 核心问题：上下文窗口是有限资源

读一个 1000 行的 Python 文件：约 4000 token
一次任务里读 30 个文件、跑 20 条命令：轻松突破 100,000 token

Claude 的上下文窗口大约是 200,000 token。听起来很大，但：
- 每次 LLM 调用都要发送**完整的 messages 数组**
- messages 越大，每次调用越慢、越贵
- 超过限制直接报错，Agent 崩溃

没有压缩策略，Agent 只能处理小任务。

---

## 三层压缩策略

### 第一层：micro_compact（每轮静默执行）

**问题**：旧的 tool_result 占着空间，但大部分已经没用了。

**策略**：把 3 轮以前的 tool_result 替换为简短占位符。

```
之前：
messages[3] = tool_result("文件内容：\nimport os\nimport sys\n...1000行...")

之后（3轮后）：
messages[3] = tool_result("[Previous: used read_file]")
```

信息压缩了，但对话结构完整，LLM 知道"我之前读过这个文件"。

**触发时机**：每次 LLM 调用之前，静默执行，不通知 LLM。

### 第二层：auto_compact（token 超阈值时触发）

**问题**：第一层压缩还不够，对话历史本身就很长。

**策略**：
1. 把完整对话历史保存到磁盘（`.transcripts/` 目录）
2. 用 LLM 生成一段对话摘要（"目前做了什么，状态是什么"）
3. 用这段摘要替换全部 messages，从新的起点继续

```
之前的 messages：[100条消息，50000 token]
         ↓ auto_compact
之后的 messages：[1条消息，2000 token]
  {"role": "user", "content": "[Compressed]\n目前已完成步骤1-3，
   下一步是步骤4...当前文件状态..."}
```

信息没有丢失（在 `.transcripts/` 里），只是移出了活跃上下文。

### 第三层：manual compact（Agent 主动触发）

**问题**：Agent 自己感知到上下文快满了，想主动压缩。

**策略**：与第二层相同，但由 Agent 调用 `compact` 工具主动触发。

---

## 代码结构解析

### micro_compact

```python
KEEP_RECENT = 3  # 保留最近 3 个 tool_result

def micro_compact(messages: list) -> list:
    # 收集所有 tool_result 的位置
    tool_results = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for j, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    # 找出这个 tool_result 对应的工具名
                    tool_name = _find_tool_name(messages, part.get("tool_use_id", ""))
                    tool_results.append((i, j, part, tool_name))
    
    # 如果 tool_result 总数 <= KEEP_RECENT，不压缩
    if len(tool_results) <= KEEP_RECENT:
        return messages
    
    # 把 KEEP_RECENT 之前的 tool_result 替换为占位符
    for i, j, part, tool_name in tool_results[:-KEEP_RECENT]:
        if len(part.get("content", "")) > 100:  # 只压缩有内容的
            part["content"] = f"[Previous: used {tool_name}]"
    
    return messages
```

注意：这是**原地修改** messages 数组，不是创建新的。

### auto_compact

```python
TRANSCRIPT_DIR = Path(".transcripts")
TOKEN_THRESHOLD = 50000

def estimate_tokens(messages: list) -> int:
    # 简单估算：字符数 / 4 ≈ token 数
    return len(json.dumps(messages, default=str)) // 4

def auto_compact(messages: list) -> list:
    # 1. 保存完整历史到磁盘
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    
    # 2. 让 LLM 生成摘要
    response = client.messages.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": "Summarize this conversation for continuity. "
                       "Include: completed tasks, current state, next steps.\n\n"
                       + json.dumps(messages, default=str)[:80000]
        }],
        max_tokens=2000,
    )
    summary = response.content[0].text
    
    # 3. 用摘要替换全部 messages
    return [
        {"role": "user", "content": f"[Compressed conversation]\n\n{summary}"}
    ]
```

### 循环集成

```python
def agent_loop(messages: list):
    while True:
        # 第一层：每轮执行
        micro_compact(messages)
        
        # 第二层：token 超限时触发
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("[auto_compact] 上下文过长，压缩中...")
            messages[:] = auto_compact(messages)
        
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        
        if response.stop_reason != "tool_use":
            return
        
        results = []
        manual_compact_triggered = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    manual_compact_triggered = True
                    results.append({...})
                else:
                    output = TOOL_HANDLERS[block.name](**block.input)
                    results.append({...})
        
        messages.append({"role": "user", "content": results})
        
        # 第三层：Agent 主动触发
        if manual_compact_triggered:
            messages[:] = auto_compact(messages)
```

---

## 动手实验

### 实验 1：触发 micro_compact

```sh
python agents/s06_context_compact.py
```

输入：
```
Read every Python file in the agents/ directory one by one
```

让 Agent 一个一个读文件。读到第 4 个文件时，在控制台观察：早期的 tool_result 是否已经被替换为 `[Previous: used read_file]`。

可以在 `micro_compact` 函数里加 print 来确认：

```python
if len(part.get("content", "")) > 100:
    print(f"[micro_compact] 压缩 tool_result: {part['content'][:50]}...")
    part["content"] = f"[Previous: used {tool_name}]"
```

### 实验 2：触发 auto_compact

把 token 阈值改小：

```python
TOKEN_THRESHOLD = 5000  # 改成 5000（原来是 50000）
```

然后给任务：
```
Read 10 Python files from the agents directory
```

观察 auto_compact 触发，看 `.transcripts/` 目录里生成的文件。

**重要**：打开生成的 transcript 文件，验证完整历史确实保存在磁盘上。

### 实验 3：改代码练习（必做）

**练习：改进 auto_compact 的摘要提示词**

当前 auto_compact 使用简单的提示词让 LLM 生成摘要。改成更结构化的提示词：

```python
COMPACT_PROMPT = """You are summarizing a coding agent conversation for continuity.

Analyze the conversation and produce a structured summary with these sections:

## Completed Work
List each completed task with its outcome.

## Current State
Describe the current state of the codebase/files.

## In Progress
If any task was interrupted, describe where it left off.

## Next Steps
List what should happen next.

Keep the summary under 800 words.

Conversation to summarize:
"""

def auto_compact(messages: list) -> list:
    ...
    response = client.messages.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": COMPACT_PROMPT + json.dumps(messages, default=str)[:80000]
        }],
        max_tokens=2000,
    )
    ...
```

触发 auto_compact，对比改前改后的摘要质量。

---

## 思考题

1. `messages[:] = auto_compact(messages)` 里用 `messages[:]` 而不是 `messages =`，区别是什么？（提示：函数参数和列表引用）

2. 如果 auto_compact 的摘要本身丢失了某个重要信息，Agent 会怎样？这是 auto_compact 的固有风险吗？

3. micro_compact 会把**最近** 3 个 tool_result 也压缩掉吗？为什么要保留最近几个？

4. auto_compact 把历史保存到 `.transcripts/`，但如果以后需要"从 transcript 恢复"，应该怎么做？（当前代码支持吗？）

---

## 检查点

完成本课后，你应该能回答：

- [ ] 三层压缩各自的触发时机是什么？
- [ ] micro_compact 之后，旧的 tool_result 内容去了哪里？
- [ ] auto_compact 之后，完整历史去了哪里？不会丢失吗？
- [ ] 压缩后 messages 数组里有几条消息？

---

## 与 Hermes 的连接

Hermes 使用 FTS5 全文搜索索引来管理对话历史，可以精确检索过去的对话内容而不是简单丢弃。s06 的 transcript 机制是这种"历史不丢失，但移出活跃窗口"设计的简化版。理解了 s06，你就理解了 Hermes 上下文管理的核心思路。
