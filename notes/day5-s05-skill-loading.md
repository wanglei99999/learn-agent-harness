# Day 5 — s05: Skill Loading

> 格言：*"用到什么知识，临时加载什么知识"*
> 对应文件：`agents/s05_skill_loading.py`
> 预计时间：2-3 小时

---

## 学习目标

- 理解为什么不能把所有领域知识塞进 system prompt
- 理解两层注入机制：system prompt（第一层）vs tool_result（第二层）
- 能独立写一个新的 Skill 文件并让 Agent 使用它
- 理解 SKILL.md 的文件格式（frontmatter + body）

---

## 核心问题：system prompt 不是免费的

你想让 Agent 遵循特定工作流：
- 写代码时遵循团队的代码风格规范（2000 token）
- 做 git 操作时遵循分支命名约定（1500 token）
- 写测试时遵循 TDD 流程（1800 token）
- 做 code review 时用标准清单（2500 token）
- 处理 PDF 时用特定库（1200 token）

如果把这 5 份规范全塞进 system prompt：
- 每次 LLM 调用都要发这 9000 token
- 80% 的调用其实只用到 1-2 份规范
- 浪费了约 7000 token（成本 + 速度）
- 大量无关内容可能降低关键规范的权重

---

## 解决方案：两层注入

```
第一层（system prompt，始终存在，低成本）：
"你是一个编程 Agent。可用的 Skills：
  - git: Git 工作流辅助（用 load_skill 加载）
  - test: 测试最佳实践（用 load_skill 加载）
  - code-review: 代码审查清单（用 load_skill 加载）"
~100 token

↓ Agent 决定需要某个 Skill 时

第二层（tool_result，按需出现，完整内容）：
"<skill name="git">
  完整的 git 工作流规范...
  第一步：...
  第二步：...
  共 2000 token 的详细说明
</skill>"
```

Agent 先知道有什么（便宜），需要的时候再取（贵）。就像知道图书馆有某本书，需要时再去借，而不是把整个图书馆搬到家里。

---

## 文件格式：SKILL.md

每个 Skill 是一个目录，里面有 `SKILL.md`：

```
skills/
  git/
    SKILL.md
  code-review/
    SKILL.md
  mcp-builder/
    SKILL.md
    references/
      some-reference.md
```

`SKILL.md` 的格式：

```markdown
---
name: git
description: Git workflow helpers for commits, branches, and PRs
---

# Git Workflow

## Commit Message Format
...完整的工作流说明...
```

- `---` 包裹的是 YAML frontmatter（元数据）
- `name`: Skill 的标识符，用于 `load_skill("git")` 调用
- `description`: 一行简短描述，会显示在 system prompt 里（第一层）
- frontmatter 下面的内容是 Skill 的完整正文（第二层按需加载）

---

## 代码结构解析

### SkillLoader 类

```python
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills = {}
        # 递归扫描所有 SKILL.md 文件
        for f in sorted(skills_dir.rglob("SKILL.md")):
            text = f.read_text()
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)  # 用 name 字段，或目录名作为后备
            self.skills[name] = {"meta": meta, "body": body}
    
    def get_descriptions(self) -> str:
        # 只返回 name 和 description（第一层）
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "")
            lines.append(f"  - {name}: {desc}")
        return "\n".join(lines)
    
    def get_content(self, name: str) -> str:
        # 返回完整 body 内容（第二层）
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'."
        return f'<skill name="{name}">\n{skill["body"]}\n</skill>'
```

### 注册进系统

```python
# 第一层：system prompt 里有 Skill 列表
SKILL_LOADER = SkillLoader(Path("skills"))
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Skills available (use load_skill to load):
{SKILL_LOADER.get_descriptions()}"""

# 第二层：load_skill 也是 dispatch map 里的一个普通工具
TOOL_HANDLERS = {
    # ...base tools...
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
}
```

循环还是一样。Skill 加载不需要任何特殊处理，它就是一个工具调用，返回一段文本。

---

## 动手实验

### 实验 1：看现有的 Skill

先看看项目里有什么 Skill：

```sh
ls skills/
```

打开 `skills/code-review/SKILL.md`，读一遍，理解 frontmatter 结构。

然后运行：

```sh
python agents/s05_skill_loading.py
```

输入：
```
What skills are available?
```

观察 Agent 如何描述可用的 Skills。

然后：
```
I need to do a code review -- load the relevant skill first
```

观察 Agent 先调用 `load_skill("code-review")`，然后依照加载的 Skill 内容进行审查。

### 实验 2：看 tool_result 里的内容

在 dispatch map 的 `load_skill` handler 里加一行日志：

```python
def load_skill_handler(name: str) -> str:
    content = SKILL_LOADER.get_content(name)
    print(f"[DEBUG] 加载 skill '{name}'，内容长度：{len(content)} 字符")
    return content

TOOL_HANDLERS = {
    ...
    "load_skill": lambda **kw: load_skill_handler(kw["name"]),
}
```

再次运行，观察加载的 Skill 内容有多少字符。对比 system prompt 里只有 description 时的简短程度。

### 实验 3：改代码练习（必做）

**练习：写一个新的 Skill**

创建文件 `skills/python-style/SKILL.md`：

```markdown
---
name: python-style
description: Python 代码风格规范，包含命名、格式、类型注解
---

# Python 代码风格规范

## 命名约定
- 函数和变量使用 snake_case：`def calculate_total()`
- 类使用 PascalCase：`class UserAccount`
- 常量使用全大写：`MAX_RETRY_COUNT = 3`
- 私有方法加下划线前缀：`def _validate_input()`

## 类型注解
所有函数必须有类型注解：
```python
def greet(name: str, times: int = 1) -> str:
    return f"Hello, {name}! " * times
```

## 函数长度
- 单个函数不超过 30 行
- 超过时拆分为更小的辅助函数

## 错误处理
```python
# 好的写法
try:
    result = risky_operation()
except ValueError as e:
    logger.error(f"Invalid input: {e}")
    raise

# 不好的写法
try:
    result = risky_operation()
except:  # 不要裸 except
    pass  # 不要吞掉异常
```
```

然后重新运行 Agent（SkillLoader 在启动时扫描，改完需重启），输入：

```
Load the python-style skill and review greet.py using it
```

观察 Agent 是否能加载你写的 Skill 并使用它。

---

## 思考题

1. 如果 Agent 需要同时用到 2 个 Skill（比如先 git 再 test），它会调用 `load_skill` 几次？每次调用的 `tool_result` 都在 messages 里吗？

2. Skill 加载后，内容放在 messages 的什么位置？它会随着上下文压缩（s06）被压缩掉吗？

3. 如果你把 Skill 的完整内容放进 system prompt，和"按需加载"相比，有什么优劣？什么场景适合直接放进 system prompt？

4. `SkillLoader` 是在程序启动时扫描一次文件系统，还是每次 `load_skill` 调用时都重新扫描？如果你在程序运行中修改了 SKILL.md 文件，Agent 能立刻感知到吗？

---

## 检查点

完成本课后，你应该能回答：

- [ ] Skill 的两层注入分别是什么？放在哪里？
- [ ] SKILL.md 里 frontmatter 和 body 各是什么作用？
- [ ] 新增一个 Skill 需要改代码吗？
- [ ] `load_skill` 工具从循环的角度看和其他工具有什么区别？

---

## 与 Hermes 的连接

Hermes 的 `skills/` 和 `optional-skills/` 目录就是这个机制的生产版本。Hermes 的 Agent 可以根据任务类型动态加载相应的 Skill，并在技能使用后通过学习循环改进 Skill 内容本身——这是 s05 机制的进化版。
