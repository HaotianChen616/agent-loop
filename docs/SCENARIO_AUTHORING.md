# Scenario 编写指南

Scenario 是教学与方案穿刺的最小可复现实验单元。它把自然语言目标、可执行成功标准、工作区材料、Agent、工具、预算和策略固定在同一个目录中。

## 1. 推荐目录

```text
scenarios/my-loop/
├── scenario.toml
├── skill.md
├── scripted_actions.json
├── fixture/
│   ├── requirements.txt
│   └── implementation.txt
└── checks/
    └── verify.py
```

Scenario 目录及子目录不能包含符号链接。除 `__pycache__` 和 `.pyc` 外，目录中的全部普通文件都会进入 Scenario digest；修改任意变体 TOML、Skill、fixture、动作脚本或检查器，都会让已有 Run 拒绝恢复。

## 2. 最小可运行配置

```toml
schema_version = 1
scenario_id = "my-loop"
title = "修正一个文本实现"
learning_objective = "观察失败证据如何进入下一轮"
goal = "让 implementation.txt 满足 requirements.txt"
acceptance_criteria = [
  "requirements-unchanged",
  "implementation-correct",
]

instructions = ["skill.md"]
allowed_tools = ["list_files", "read_file", "write_file"]

[workspace]
mode = "copy"
seed = "fixture"
read_only = ["requirements.txt"]

[agent]
kind = "scripted"
script = "scripted_actions.json"
request_timeout_seconds = 30
max_output_tokens = 1000

[verification]
kind = "python_script"
script = "checks/verify.py"
timeout_seconds = 10

[budget]
max_iterations = 6
max_agent_calls = 6
max_tool_calls = 10
max_verifications = 7
max_elapsed_seconds = 120
max_same_failure = 2

[policy]
auto_allow = ["read", "local_write"]
require_approval = ["external_write"]
deny = ["irreversible"]
```

`instructions`、`workspace.seed`、`agent.script` 和 `verification.script` 相对 `scenario.toml`；`workspace.read_only` 相对复制后的 workspace 根。它们都不能是绝对路径或包含 `..`。

## 3. 字段与约束

### 顶层字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `schema_version` | 是 | 当前只能是整数 `1` |
| `scenario_id` | 是 | 写入 manifest 和 RunState 的稳定标识 |
| `title` | 是 | 面向人的场景名称 |
| `learning_objective` | 是 | 这条教学路径要展示什么 |
| `goal` | 是 | 指导 Agent 的自然语言目标，不作为完成证据 |
| `acceptance_criteria` | 是 | 非空、唯一的 ID 列表，必须与 Verifier 精确对应 |
| `instructions` | 否 | 注入 Context 的 UTF-8 Markdown/文本文件 |
| `allowed_tools` | 是 | 非空、唯一的内置工具子集 |

当前工具名称只有：

- `list_files`：读取工作区文件列表，风险 `read`；
- `read_file`：读取有界 UTF-8 文本，风险 `read`；
- `write_file`：覆盖工作区文本，风险 `local_write`；
- `mock_external_write`：教学审批用 mock，风险 `external_write`，不会真实发送。

### `[context]`

| 字段 | 默认值 | 当前语义 |
|---|---:|---|
| `max_input_chars` | 30000 | 期望的 Context 上限；v0 只截断 Skill，固定块过大时可能超限 |
| `max_history_items` | 8 | 为后续历史摘要预留，v0 尚未使用 |
| `max_tool_output_chars` | 8000 | `read_file` 返回给 Agent 的最大字符数 |

### `[workspace]`

- `seed` 必填，必须指向 Scenario 内已有目录；
- `mode` 当前只能是 `copy`；
- `read_only` 是禁止 Agent 写入的相对文件或目录前缀。

### `[agent]`

- `kind` 是 `scripted` 或 `llm`，默认 `scripted`；
- ScriptedAgent 实际运行时必须提供 `script`；
- LLM 的 `provider` 可选 `openai` 或 `zhipu-coding-plan`，默认 `openai`；
- LLM Agent 必须通过 `model` 或 CLI `--model` 显式指定模型；
- `request_timeout_seconds` 默认 30；
- `max_output_tokens` 默认 1000。

Provider 配置、环境变量和新增实现步骤见 [PROVIDERS.md](PROVIDERS.md)。

### `[verification]`

- `script` 必填，指向 Scenario 控制区内的 Python 文件；
- `kind` 当前只能是 `python_script`；
- `timeout_seconds` 默认 10。

### `[budget]`

所有值都必须是正整数。默认值分别是 6 次 iteration、6 次 Agent、10 次 Tool、7 次 Verification、120 秒和 2 次相同失败。初始验证也消耗一次 Verification 预算。

Elapsed deadline 在每次 `start/resume` 时重新建立；它不是跨多次恢复累计的总墙钟。Provider 返回的 Token usage 会记录到事件但不参与 StopPolicy，费用不计算。

### `[policy]`

风险值只能是 `read`、`local_write`、`external_write`、`irreversible`。同一风险不能同时出现在多个组中。默认策略与上面的示例一致。

## 4. ScriptedAgent 动作

文件必须是 JSON 数组，每一项都是严格的 `AgentDecision`。写文件：

```json
{
  "kind": "tool_call",
  "summary": "根据反馈修正 implementation.txt",
  "tool": "write_file",
  "arguments": {
    "path": "implementation.txt",
    "content": "Hello, loop!\n"
  }
}
```

主动请求验证：

```json
{
  "kind": "request_verification",
  "summary": "当前结果应已满足标准"
}
```

声明阻塞：

```json
{
  "kind": "blocked",
  "summary": "缺少必需输入",
  "reason": "read_file 刚刚确认该文件不存在"
}
```

`blocked` 不是自行终止权。只有最近一轮存在受支持的文件缺失或权限工具证据时，StopPolicy 才进入 `blocked`；否则进入 `needs_review`。

## 5. Verifier 协议

检查脚本在 workspace 的一次性快照中运行：

- 当前工作目录是快照根目录；
- `AGENT_LOOP_WORKSPACE` 指向同一快照；
- 环境仅保留 `PATH`、必要的平台字段和验证临时目录；
- 脚本位于 Scenario 控制区，Agent 不能修改；
- 脚本是可信代码，不受 OS Sandbox 限制。

stdout 必须只输出一个 JSON 对象：

```json
{
  "schema_version": 1,
  "results": [
    {
      "criterion_id": "implementation-correct",
      "verdict": "fail",
      "message": "期望 Hello, loop!，实际为空",
      "evidence": ["implementation.txt"]
    }
  ]
}
```

规则：

- 每个 acceptance criterion 恰好出现一次；
- `verdict` 只能是 `pass`、`fail`、`inconclusive`；
- 总体 `pass/fail/inconclusive` 必须分别以退出码 `0/1/2` 结束；
- 非 JSON 日志应写 stderr；
- 超时、缺失 ID、重复 ID、未知 ID 或退出码冲突都会变成 `inconclusive`。

原始 stdout、stderr 和退出码会保存到 Run artifacts，`message` 会成为下一轮 Agent 的主要反馈，因此应说明观察事实和期望事实。

## 6. 从基线到方案穿刺

先用 ScriptedAgent 建立确定性基线：

```bash
agent-loop run scenarios/my-loop/scenario.toml --run-id my-baseline
agent-loop inspect my-baseline
```

至少准备：

1. Happy Path：最短动作后通过；
2. Feedback Path：第一次失败，第二次使用证据修正；
3. Budget Path：无进展时有界停止；
4. 若有高风险工具，再准备 Approval approve/reject 路径。

基线稳定后，只改变一个变量，例如 Agent、Skill、预算或失败停止策略。实验记录方法见 [EXPERIMENTS.md](EXPERIMENTS.md)。
