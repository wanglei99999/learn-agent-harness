# Learn Claude Code 学习教案

## 学习方法

每节课固定三步：**读文档 → 跑代码 → 改代码**

改代码是最重要的一步。只读不动手，理解停留在表面。每节课都有明确的"改代码练习"，必须完成。

## 环境准备

```sh
git clone https://github.com/shareAI-lab/learn-claude-code
cd learn-claude-code
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 ANTHROPIC_API_KEY 和 MODEL_ID
```

## 课程地图

```
第一阶段：循环基础（2天）
  Day 1 — s01: Agent Loop        ← 从这里开始，理解最小循环
  Day 2 — s02: Tool Use          ← dispatch map，加工具不改循环

第二阶段：规划与记忆（4天）
  Day 3 — s03: TodoWrite         ← 让 Agent 有计划地干活
  Day 4 — s04: Subagent          ← 上下文隔离，子任务不污染主对话
  Day 5 — s05: Skill Loading     ← 按需注入知识
  Day 6 — s06: Context Compact   ← 三层压缩，对话可以无限长

第三阶段：持久化（2天）
  Day 7 — s07: Task System       ← 任务图，状态活过上下文压缩
  Day 8 — s08: Background Tasks  ← 慢操作丢后台，Agent 继续干别的

第四阶段：多 Agent 协作（3天）
  Day 9  — s09: Agent Teams      ← 持久队友 + 文件邮箱通信
  Day 10 — s10/s11: Protocols + Autonomous ← 协议 + 自主认领
  Day 11 — s12: Worktree + 总复习 ← 隔离执行 + 全部机制合一
```

## 各课文件

| 文件 | 对应课程 |
|------|---------|
| [day1-s01-agent-loop.md](./day1-s01-agent-loop.md) | s01 Agent Loop |
| [day2-s02-tool-use.md](./day2-s02-tool-use.md) | s02 Tool Use |
| [day3-s03-todo-write.md](./day3-s03-todo-write.md) | s03 TodoWrite |
| [day4-s04-subagent.md](./day4-s04-subagent.md) | s04 Subagent |
| [day5-s05-skill-loading.md](./day5-s05-skill-loading.md) | s05 Skill Loading |
| [day6-s06-context-compact.md](./day6-s06-context-compact.md) | s06 Context Compact |
| [day7-s07-task-system.md](./day7-s07-task-system.md) | s07 Task System |
| [day8-s08-background-tasks.md](./day8-s08-background-tasks.md) | s08 Background Tasks |
| [day9-s09-agent-teams.md](./day9-s09-agent-teams.md) | s09 Agent Teams |
| [day10-s10-s11-protocols-autonomous.md](./day10-s10-s11-protocols-autonomous.md) | s10 + s11 |
| [day11-s12-worktree-review.md](./day11-s12-worktree-review.md) | s12 + 总复习 |

## 学完后能做什么

| 阶段 | 你能做什么 |
|------|-----------|
| 阶段一结束 | 从零写出一个能用的 Agent，100 行以内 |
| 阶段二结束 | Agent 能规划复杂任务，上下文不会爆 |
| 阶段三结束 | Agent 状态持久化，重启不丢进度 |
| 阶段四结束 | 理解多 Agent 协作架构，能读 Hermes 核心代码 |
