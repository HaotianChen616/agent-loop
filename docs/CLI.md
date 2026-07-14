# CLI 使用手册

CLI 是 v0 的 Trigger、教学 Trace 和人工关卡。本文记录当前实现的命令、退出码和恢复约束。

## 安装与版本

需要 Python 3.12 或更高版本：

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 12), sys.version'
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

默认 ScriptedAgent 无需 API Key 或额外依赖。

## 命令总览

```text
agent-loop run <scenario> [--run-id ID] [--runs-dir DIR]
               [--agent scripted|llm] [--provider PROVIDER]
               [--model MODEL] [--step]

agent-loop resume <run-id> [--scenario FILE] [--runs-dir DIR]
                  [--agent scripted|llm] [--provider PROVIDER] [--model MODEL]
                  [--approve | --reject] [--step]

agent-loop inspect <run-id> [--runs-dir DIR]

agent-loop apply <run-id> <target-dir> [--runs-dir DIR] [--yes]
```

所有命令的 `--runs-dir` 默认都是 `.agent-loop/runs`。如果 Run 创建时使用了其他目录，后续 `inspect/resume/apply` 必须传入同一个目录。

## `run`：创建并驱动 Run

```bash
agent-loop run scenarios/hello-loop/scenario.toml --run-id quickstart
```

`--run-id` 可省略，由系统生成。手工 ID 只能包含小写字母、数字和连字符，并且不能与已有 Run 重复。

`--agent` 可以覆盖 Scenario 中的 Agent 类型。使用 `--agent llm` 时，Provider 可选 `openai` 或 `zhipu-coding-plan`，模型必须通过 `--model` 或 Scenario 的 `agent.model` 显式提供：

```bash
python -m pip install -e '.[openai]'
export OPENAI_API_KEY='...'
agent-loop run scenarios/hello-loop/scenario.toml \
  --agent llm --provider openai --model your-model-id
```

智谱 Coding Plan 使用 `ZAI_API_KEY` 和专属场景 `scenarios/hello-loop/zhipu.toml`，完整说明见 [PROVIDERS.md](PROVIDERS.md)。

`--step` 会在每个持久事件后等待回车，适合逐步讲解，不适合无交互 CI。

## `resume`：恢复同一个事实包

未显式传 `--scenario` 时，CLI 使用 manifest 保存的 Scenario 路径。恢复前会重新计算 Scenario digest，并要求 Agent、Provider 和 model 与原 Run 一致；定义发生变化时应创建新 Run，而不是继续旧实验。

审批场景：

```bash
agent-loop run scenarios/approval-loop/scenario.toml --run-id approval-demo
# 上一步以退出码 2 暂停是预期行为

agent-loop resume approval-demo --approve
# 或
agent-loop resume approval-demo --reject
```

批准只执行 state 中保存的同一个 `action_id`、工具和参数。审批期间 workspace 变化会使批准失效。拒绝会进入 `cancelled`。

当前 `resume --approve/--reject` 只处理带 `pending_approval` 的审批暂停。Verifier 不确定、事件损坏或未知 in-flight 动作产生的通用 `needs_review` 没有自动解除命令。

## `inspect`：读取事实时间线

```bash
agent-loop inspect quickstart
```

它按顺序渲染 `events.jsonl`，最后打印当前状态。`inspect` 不调用 Agent，也不会用模型重新解释历史。

若需要原始数据，可直接查看：

```bash
python3 -m json.tool .agent-loop/runs/quickstart/state.json
sed -n '1,20p' .agent-loop/runs/quickstart/events.jsonl
```

## `apply`：显式发布已验证 workspace

目标目录必须预先存在：

```bash
mkdir -p ./result
agent-loop apply quickstart ./result
```

CLI 会展示最终 workspace 相对目标目录的新增和修改文件，然后请求确认。`--yes` 表示对该次已展示预览显式确认，主要用于自动化环境。

Apply 的关键限制：

- 只接受 `completed` 且最后验证为 `pass` 的 Run；
- 最终 workspace 的全部普通文件都会参与比较，包括未被 Agent 修改的 fixture 文件；
- 最多 1000 个文件、总计 10 MB；
- 不传播文件删除；
- 单个文件原子替换，但多文件 apply 不是整体事务；
- 失败记录会保存已写入的路径；
- workspace、目标根目录身份或待覆盖目标文件在确认期间变化时拒绝执行；无关目标文件变化不影响本次预览；
- 只复制内容，不保留权限或可执行位；写入文件以 `0600` 创建（仍受 umask 约束）；
- 依赖 POSIX 安全打开能力，支持 macOS 和 Linux。

成功生成预览后的每次确认或应用尝试会单独记录；完成状态、workspace 摘要或目标安全等前置检查失败时不会生成记录：

```text
.agent-loop/runs/<run-id>/applications/<application-id>.json
```

它不会改写已完成 Run 的状态或事件。

## 退出码

| 退出码 | 含义 | 常见状态 |
|---:|---|---|
| `0` | 成功完成命令 | `completed`，或 apply 成功 |
| `2` | Run 正在等待人工复核 | `needs_review` |
| `1` | 其他非成功结果或用户错误 | `failed`、`blocked`、`budget_exhausted`、`cancelled`、apply 被拒绝 |

教学场景的预期结果：

| 场景 | 最终/暂停状态 | 退出码 |
|---|---|---:|
| `hello-loop/happy.toml` | `completed` | 0 |
| `hello-loop/scenario.toml` | `completed` | 0 |
| `hello-loop/budget.toml` | `failed` | 1 |
| `approval-loop/scenario.toml` 首次运行 | `needs_review` | 2 |
| Approval 批准恢复 | `completed` | 0 |

在启用 `set -e` 的脚本中运行 Approval 或 Budget 教程时，应显式处理这些预期的非零退出码。
