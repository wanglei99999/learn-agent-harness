import os
import subprocess

from anthropic import Anthropic
from dotenv import load_dotenv

# 从 .env 文件加载环境变量（ANTHROPIC_API_KEY、MODEL_ID、ANTHROPIC_BASE_URL）
# override=True 表示 .env 里的值会覆盖系统已有的同名环境变量
load_dotenv(override=True)

# 使用第三方兼容服务（如 DeepSeek）时，需要去掉 Anthropic 自己的 token
# 否则 SDK 会优先用 ANTHROPIC_AUTH_TOKEN，导致请求失败
# os.environ.pop(key, None)：从环境变量字典中删除该键，不存在时返回 None 不报错，因为我们有key了，所以不用token，防止sdk优先请求token
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 创建 Anthropic 客户端
# base_url 为 None 时默认连 Anthropic 官方，填了则连第三方兼容服务
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# 从环境变量读取模型 ID，例如 "deepseek-v4-flash" 或 "claude-sonnet-4-6"
MODEL = os.environ["MODEL_ID"]

# 系统提示词：告诉模型它是谁、在哪、该怎么做
# os.getcwd() 动态注入当前目录，让模型知道自己在哪个位置工作
SYSTEM = f"你是一个编程Agent, 当前的工作目录是{os.getcwd()}。使用bash工具去完成任务，直接行动，不要解释。"

# 工具定义：告诉模型有哪些工具可以用
# 这里只有一个工具：bash，让模型可以执行任意 shell 命令
# - name: 工具名，模型调用时用这个名字
# - description: 告诉模型这个工具是干什么的，模型靠这个决定要不要用它
# - input_schema: 定义参数格式（JSON Schema），这里只有一个必填参数 command
TOOLS = [{
    "name": "bash",
    "description": "run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


def run_bash(command: str) -> str:
    # 简单的安全过滤：包含危险字符串的命令直接拦截，不执行
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误:已阻止危险指令"
    try:
        # 执行 shell 命令
        # shell=True：通过 shell 解释器运行，支持管道、通配符等
        # capture_output=True：捕获 stdout 和 stderr
        # text=True：输出结果是字符串而不是 bytes
        # timeout=120：最多执行 120 秒，防止命令卡死
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 返回执行结果，最多 50000 字符，防止结果太长撑爆上下文
        # 无论成功还是失败，都返回字符串，因为这个结果要发回给模型
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "错误：超时(120S)"
    except (FileNotFoundError, OSError) as e:
        return f"错误：{e}"


def agent_loop(messages: list):
    # 这是整个 Agent 的核心：一个 while 循环
    # 循环会一直运行，直到模型不再调用工具为止
    while True:
        # 把消息历史 + 工具定义发给模型，拿到响应
        # response 是一个 Message 对象，主要字段：
        #   response.id           - 本次请求的唯一 ID
        #   response.model        - 实际使用的模型名
        #   response.stop_reason  - 模型停止的原因："tool_use" 或 "end_turn"
        #   response.content      - 模型输出内容，是一个列表，每个元素是：
        #                           TextBlock(type="text", text="...")        模型的文字回复
        #                           ToolUseBlock(type="tool_use", id, name, input)  工具调用
        #   response.usage        - token 用量统计（input_tokens / output_tokens）
        # 我们只关心 stop_reason 和 content，其他字段这里用不到
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        # 把模型的回复追加进消息历史
        # 注意：不管模型说了什么，都要先存进去，保持对话历史完整
        messages.append({
            "role": "assistant", "content": response.content
        })

        # stop_reason 是模型告诉我们"为什么停下来"
        # "tool_use" 表示模型想调用工具，需要继续循环
        # 其他值（如 "end_turn"）表示模型认为任务完成了，直接退出
        # 注意：控制权在模型手里，代码只是听从模型的决定
        if response.stop_reason != "tool_use":
            return

        # 执行模型请求的工具调用，收集结果
        # 关键理解：tool_use 是 agency 和 harness 的分界点
        #   - 模型决定调哪个工具、传什么参数 → 这是模型的 agency（训练出来的）
        #   - 我们检测到 tool_use 后去真正执行命令 → 这是 harness 的工作
        #   - 模型做决策，harness 执行，两者职责严格分离
        results = []
        for block in response.content:
            # 文字回复的 block type 是 text
            # 工具调用的 block type 是 tool_use
            if block.type == "tool_use":
                # block.input["command"] 是模型生成的具体命令，例如 "ls -la"
                output = run_bash(block.input["command"])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,  # 对应模型那次调用的唯一 ID
                    "content": output         # 执行结果发回给模型
                })

        # 把工具执行结果以 user 角色追加进消息历史
        # 回到 while True 顶部，模型看到结果后决定下一步
        messages.append({"role": "user", "content": results})

if __name__ == '__main__':
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