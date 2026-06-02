"""
s02_tool_use.py — 工具分发表 + 文件操作工具

s02 在 s01 基础上新增两个机制：
  1. TOOL_HANDLERS 字典：工具名 → lambda，agent_loop 通过名字查表分发，不再写 if/else
  2. 专用文件操作工具：read_file / write_file / edit_file，统一经过 safe_path 校验

核心改进：

  s01 的方式：
    if block.name == "bash": run_bash(...)   ← 每加一个工具就要改 agent_loop

  s02 的方式：
    TOOL_HANDLERS = {"bash": ..., "read_file": ..., ...}
    handler = TOOL_HANDLERS.get(block.name)  ← agent_loop 不用改，只加字典条目

安全边界：
  safe_path() 把所有文件路径限制在 WORKDIR 内，防止 LLM 访问系统文件。
  resolve() 展开 ../../ 这类路径后，is_relative_to() 判断是否越界。

s02 → s03 的演进：工具够用了，但模型没有"进度"概念，不知道自己完成了哪些步骤。
"""
import os
import subprocess
from pathlib import Path

# pathlib 把路径当对象处理，比字符串拼接更安全，支持 / 运算符拼路径
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"你是一个编程Agent, 当前的工作目录是{WORKDIR}。使用工具去完成任务，直接行动，不要解释。"

# harness 的权限边界：限制模型只能访问工作目录内的文件
# s01 用 bash 也能读写文件，但没有路径限制，模型理论上可以访问任意路径
# s02 把文件操作独立出来，统一过 safe_path，这是 harness 对 agency 的约束
def safe_path(p:str)->Path:
    path = (WORKDIR / p).resolve()
    # resolve() 会展开 ../../../etc/passwd 这类路径，is_relative_to 再判断是否越界
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径{p}不在工作空间内")
    return path

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误: 已阻止危险指令"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "错误：超时(120s)"
    except (FileNotFoundError, OSError) as e:
        return f"错误：{e}"


# s02 相比 s01 新增了三个文件操作工具：read/write/edit
# s01 只有 bash，模型只能通过 shell 命令间接操作文件（cat/echo 等）
# s02 把文件读写提升为独立工具，模型可以直接操作，更精确、更安全

# 读取文件内容，limit 可限制返回行数，避免大文件撑爆上下文
def run_read(path:str, limit:int=None)->str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        # 超出 limit 时截断并提示剩余行数，模型看到提示可决定是否继续读
        if limit and limit<len(lines):
            lines=lines[:limit]+[f"...({len(lines)-limit}) more lines"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误：{e}"

# 写入文件，覆盖已有内容；目录不存在时自动递归创建
def run_write(path:str, content:str)->str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"写了{len(content)}字符到路径{path}"
    except Exception as e:
        return f"错误：{e}"

# 局部替换：找到 old_text 并替换为 new_text（只替换第一处）
# 比 run_write 更精确，模型不需要重写整个文件
def run_edit(path:str, old_text:str,new_text:str)->str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"错误：路径{path}中无此内容"
        fp.write_text(content.replace(old_text,new_text,1))
        return f"已编辑{path}"
    except Exception as e:
        return f"错误：{e}"



# 分发表：工具名 → 处理函数，是 harness 的内部路由
# agent_loop 用 TOOL_HANDLERS.get(block.name) 找到对应函数并执行
# 新增工具只需加一行，agent_loop 本身不需要改动
# lambda **kw 统一接收模型传来的任意参数字典，再按需取值转发给具体函数
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

TOOLS = [
    {
        "name": "bash",
        "description": "执行 shell 命令。",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "阅读文件内容",
        "input_schema": {
            "type":"object",
            "properties":{
                "path":{"type":"string"},
                "limit": {"type":"integer"},  # 不在 required 里 = 可选参数，模型可以不传
            },
            "required":["path"],  # required 列表决定模型必须提供哪些参数
        },
    },
    {
        "name":"write_file",
        "description":"写内容到文件中",
        "input_schema":{
            "type":"object",
            "properties":{
                "path":{"type":"string"},
                "content":{"type":"string"},
            },
            "required": ["path","content"]
        },
    },
    {
        "name":"edit_file",
        "description":"替换文件中的内容",
        "input_schema":{
            "type": "object",
            "properties":{
                "path":{"type":"string"},
                "old_text":{"type":"string"},
                "new_text":{"type":"string"}
            },
            "required":["path","old_text","new_text"]
        },
    },
]


def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"未知工具: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input(">>")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
