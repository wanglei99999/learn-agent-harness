# Day 2 — s02: Tool Use

> 格言：*"加一个工具，只加一个 handler"*
> 对应文件：`agents/s02_tool_use.py`
> 预计时间：2-3 小时

---

## 学习目标

- 理解 dispatch map 是什么，以及为什么比 if/elif 链好
- 能独立给 Agent 注册新工具（全流程：handler + schema + TOOLS 列表）
- 理解路径沙箱（`safe_path`）是什么，为什么需要它
- 确认循环本身在 s01 和 s02 之间完全没有变化

---

## 核心问题：只有 bash，一切都靠 shell

s01 只有一个 `bash` 工具，这意味着读文件要用 `cat`，写文件要用 `echo >` 或 `tee`，编辑文件要用 `sed`。

问题：
- `cat` 大文件会把整个内容倒进上下文，无法限制行数
- `sed` 遇到特殊字符经常出错
- 每次 bash 调用都是一个不受约束的安全面，LLM 可以执行任意命令

s02 的解法：给常用操作加专用工具，在工具层做约束，而不是靠 bash 的黑名单拦截。

---

## 代码结构解析

打开 `agents/s02_tool_use.py`。

### 路径沙箱（第一个关键概念）

```python
WORKDIR = Path(os.getcwd()).resolve()

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
```

**为什么需要这个？**

如果没有沙箱，LLM 可以传 `path = "../../etc/passwd"` 给 `read_file`，然后读到系统敏感文件。

`safe_path` 的逻辑：
1. 把用户传来的路径拼接到工作目录下
2. 用 `.resolve()` 解析所有 `..` 和符号链接，得到真实绝对路径
3. 检查这个真实路径是否仍然在工作目录内
4. 如果不在，抛出异常

```python
# 攻击尝试
safe_path("../../etc/passwd")
# → (WORKDIR / "../../etc/passwd").resolve() → /etc/passwd
# → /etc/passwd.is_relative_to(WORKDIR) → False
# → raise ValueError
```

### 专用工具函数

```python
def run_read(path: str, limit: int = None) -> str:
    text = safe_path(path).read_text()
    lines = text.splitlines()
    if limit and limit < len(lines):
        lines = lines[:limit]
    return "\n".join(lines)[:50000]

def run_write(path: str, content: str) -> str:
    target = safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Written {len(content)} chars to {path}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    target = safe_path(path)
    content = target.read_text()
    if old_text not in content:
        return f"Error: old_text not found in {path}"
    target.write_text(content.replace(old_text, new_text, 1))
    return "Edit applied"
```

每个函数都职责单一，都经过 `safe_path` 沙箱。

### dispatch map（第二个关键概念）

```python
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}
```

这是一个普通的 Python 字典，把工具名映射到对应的处理函数。

循环里的调用：

```python
for block in response.content:
    if block.type == "tool_use":
        handler = TOOL_HANDLERS.get(block.name)
        output = handler(**block.input) if handler \
            else f"Unknown tool: {block.name}"
```

对比 s01 的循环——**循环体完全一样**，唯一的区别是把硬编码的 `run_bash()` 调用换成了 `TOOL_HANDLERS.get(block.name)` 这一行字典查找。

**为什么 dispatch map 比 if/elif 好？**

```python
# 坏的写法（if/elif）：每加一个工具都要改循环
if block.name == "bash":
    output = run_bash(...)
elif block.name == "read_file":
    output = run_read(...)
elif block.name == "write_file":
    output = run_write(...)
# 加工具 = 改循环 = 有风险

# 好的写法（dispatch map）：加工具不碰循环
TOOL_HANDLERS["new_tool"] = lambda **kw: run_new_tool(...)
# 加工具 = 加字典条目 = 循环不变
```

这是**开放-封闭原则**的实践：对扩展开放，对修改封闭。

---

## TOOLS 列表的作用

`TOOLS` 列表里的每一项是给 LLM 看的工具说明书：

```python
TOOLS = [
    {"name": "bash", "description": "...", "input_schema": {...}},
    {"name": "read_file", "description": "Read a file.", "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path"},
            "limit": {"type": "integer", "description": "Max lines"},
        },
        "required": ["path"],
    }},
    ...
]
```

LLM 读这个列表，知道有哪些工具可用，每个工具需要什么参数。当 LLM 决定调用 `read_file` 时，它会生成：

```json
{
  "type": "tool_use",
  "name": "read_file",
  "input": {"path": "requirements.txt", "limit": 50}
}
```

然后你的 dispatch map 里的 handler 接收这个 `input` 字典，执行实际操作。

**两个列表必须保持同步**：
- `TOOLS` 里有的工具 → `TOOL_HANDLERS` 里必须有对应 handler
- `TOOL_HANDLERS` 里有的 handler → 建议 `TOOLS` 里也声明（否则 LLM 不知道能用）

---

## 动手实验

### 实验 1：跑起来，感受专用工具的差异

```sh
python agents/s02_tool_use.py
```

输入：
```
Read the file requirements.txt
Create a file called greet.py with a greet(name) function
Edit greet.py to add a docstring to the function
Read greet.py to verify the edit worked
```

注意：这次 Agent 不会用 `cat`、`echo` 这些 bash 命令，而是直接调用 `read_file`、`write_file`、`edit_file`。

### 实验 2：测试路径沙箱

在 `agent_loop` 里加一行调试：

```python
def run_read(path: str, limit: int = None) -> str:
    target = safe_path(path)  # 如果路径逃逸，这里会抛出异常
    print(f"[DEBUG] 读取路径：{target}")  # 加这行
    text = target.read_text()
    ...
```

然后输入：
```
Read the file ../../../etc/hosts
```

观察 `safe_path` 抛出的异常是怎么被处理的（注意：你需要在循环里加 try/except，否则会崩溃——这本身就是一个发现问题的好机会）。

### 实验 3：改代码练习（必做）

**练习：添加第 5 个工具 `list_dir`**

完整走一遍"加工具"的流程：

**第一步**：写 handler 函数

```python
def run_list_dir(path: str = ".") -> str:
    target = safe_path(path)
    if not target.is_dir():
        return f"Error: {path} is not a directory"
    items = []
    for item in sorted(target.iterdir()):
        kind = "dir" if item.is_dir() else "file"
        items.append(f"[{kind}] {item.name}")
    return "\n".join(items) if items else "(empty directory)"
```

**第二步**：加入 dispatch map

```python
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "list_dir":   lambda **kw: run_list_dir(kw.get("path", ".")),  # 加这行
}
```

**第三步**：加入 TOOLS 列表

```python
{
    "name": "list_dir",
    "description": "List files and directories in a path.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path (default: current)"},
        },
        "required": [],
    },
},
```

**第四步**：验证

循环没有改动。跑起来输入：
```
List all files in the agents directory
```

看 Agent 是否调用了 `list_dir` 工具。

---

## 思考题

1. `TOOL_HANDLERS` 里用 `lambda **kw:` 而不是直接写 `run_read`，区别是什么？为什么要展开 `**kw`？

2. 如果 LLM 调用了 `TOOLS` 里没有声明的工具（比如 `delete_file`），会发生什么？（看 dispatch map 里的 `else` 分支）

3. `run_edit` 里用 `replace(old_text, new_text, 1)`，最后的 `1` 是什么意思？如果去掉会怎样？

4. 为什么 `run_write` 里有 `target.parent.mkdir(parents=True, exist_ok=True)`？不加会有什么问题？

---

## 检查点

完成本课后，你应该能回答：

- [ ] dispatch map 是什么数据结构？工具名和什么映射？
- [ ] 加一个新工具需要改哪几个地方？循环需要改吗？
- [ ] `safe_path` 做了什么？能防御什么类型的攻击？
- [ ] `TOOLS` 列表和 `TOOL_HANDLERS` 字典的作用各是什么？

---

## 与 Hermes 的连接

Hermes 的 `tools/` 目录里有大量专用工具（文件操作、网络、终端等）。每个工具都是一个独立模块，注册进统一的 dispatch 系统。你现在理解的这个模式，就是 Hermes 工具系统的核心设计。
