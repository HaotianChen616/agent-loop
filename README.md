# Agent Loop

一个用于教学和快速方案穿刺的最小 Loop Engineering 实现。它刻意保持体量小，但包含完整闭环：触发、目标、Skill、隔离工作区、Agent、工具、策略、验证、状态、预算、停止规则、人工关卡和审计 Trace。

核心规则只有一句：**Agent 只能提出下一步动作，只有独立 Verifier 的证据可以让 Run 进入 `completed`。**

```text
Scenario → Workspace → Initial Verify
                        ↓ fail
Context → Agent → Policy → Tool → Verify → Stop Decision
   ↑                                      │
   └──────────── evidence feedback ───────┘
```

## 文档导航

| 想了解什么 | 从这里开始 |
|---|---|
| 5 分钟跑通并理解项目 | 本 README |
| 当前代码如何协作、状态如何流转 | [实现架构](docs/ARCHITECTURE.md) |
| 编写自己的教学或实验场景 | [Scenario 编写指南](docs/SCENARIO_AUTHORING.md) |
| 完整命令、退出码、恢复与 apply 语义 | [CLI 使用手册](docs/CLI.md) |
| 基于基线开展一次可比较的方案穿刺 | [方案穿刺指南](docs/EXPERIMENTS.md) |
| 设计动机、取舍和演进方向 | [原始设计文档](docs/DESIGN.md) |

建议新参与者依次阅读 README → ARCHITECTURE，然后运行 Feedback Path，并对照 `.agent-loop/runs/<run-id>/events.jsonl` 阅读 `LoopEngine`。

## 最快开始

要求 Python 3.12 或更高版本。项目核心只使用标准库：

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 12), sys.version'
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

运行默认的 Feedback Path：

```bash
agent-loop run scenarios/hello-loop/scenario.toml --run-id quickstart
```

控制台会逐项展示 Context 构建、Agent 提案、Policy 授权、Tool 执行、Verifier 证据和 Stop Decision。预期最终输出包含 `status=completed iterations=2`，运行数据保存在 `.agent-loop/runs/<run-id>/`。

## 三条可重复教学路径

```bash
# Happy Path：一次修改后验证通过
agent-loop run scenarios/hello-loop/happy.toml

# Feedback Path：第一次失败，证据进入下一轮；预置动作随后修正
agent-loop run scenarios/hello-loop/scenario.toml

# Budget Path：重复无效动作，被停止策略终止
agent-loop run scenarios/hello-loop/budget.toml
```

预期结果：Happy 和 Feedback 为 `completed`/退出码 0；Budget 为 `failed`/退出码 1。Budget 的非零退出是教学内容的一部分，它演示“失败也必须有界”。加入 `--step` 可在每个事件后暂停，适合课堂讲解。

## 审批、恢复和检查

审批场景使用一个不会访问真实外部系统的 mock 工具：

```bash
agent-loop run scenarios/approval-loop/scenario.toml --run-id approval-demo
agent-loop resume approval-demo --approve
# 或：agent-loop resume approval-demo --reject
```

第一条命令以 `needs_review`/退出码 2 暂停是预期行为。批准与动作 ID、参数和工作区摘要绑定；恢复时只执行保存的同一个动作一次。查看任意 Run 的事实时间线：

```bash
agent-loop inspect approval-demo
```

`inspect` 只读取持久事件，不调用模型重新解释历史。

## 将验证结果应用到目标目录

Agent 始终修改隔离工作区。只有已完成且拥有通过证据的 Run 才能显式应用：

```bash
mkdir -p ./result
agent-loop apply quickstart ./result
```

目标目录必须预先存在。命令会比较最终 workspace 的全部文件与目标目录，展示新增和修改后再请求确认；这也包括未被 Agent 修改的 fixture 文件。自动化环境可以传 `--yes`，它仍然属于显式确认。确认前后都会复核工作区摘要、目标文件前置状态和符号链接；成功生成预览后的确认与应用会记录在 `applications/<application-id>.json`，前置拒绝不落记录，也不会改写已完成 Run 的状态或事件。

v0 不传播文件删除，也不提供 `--force`；最多处理 1000 个文件、总计 10 MB，多文件 apply 不是整体事务，并且只复制内容、不保留权限或可执行位。

## 使用真实模型

确定性 `ScriptedAgent` 用于教程和 CI；OpenAI Responses Adapter 用于观察非确定性行为：

```bash
python -m pip install -e '.[openai]'
export OPENAI_API_KEY='...'
agent-loop run scenarios/hello-loop/scenario.toml \
  --agent llm --model your-model-id
```

模型必须显式指定，不使用会随时间变化的默认值。Adapter 使用严格 JSON Schema、`store=False`、请求超时和 `max_output_tokens`，并把实际 Agent/model 写入 manifest；恢复必须使用同一配置。参数形态依据 OpenAI 官方的 [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 和 [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create) 文档。

## 最小组件对应

| 组件 | v0 实现 |
|---|---|
| Trigger / Goal | CLI + TOML Scenario |
| Skills / Context | Markdown Skill + 有界 ContextBuilder |
| Workspace | 每个 Run 独立复制，路径与只读文件受约束 |
| Agent | ScriptedAgent + 可选 OpenAI Responses Adapter |
| Tools / Policy | 显式注册的文件工具、风险分级与审批 |
| Verifier | 在一次性快照中运行的可信 Python 检查器 |
| State / Memory | 原子 `state.json` + 追加式 `events.jsonl` |
| Budget / Stop | 调用、迭代、验证、时间与重复失败上限 |
| Human Gate | 高风险动作审批 + completed 后 `apply` |
| Trace / Audit | 控制台事件、manifest、artifact、application 记录 |

`LoopEngine` 是正常驱动路径中的唯一状态迁移控制器；初始状态创建和保守恢复修正由 `StateStore` 负责。当前 Agent、StateStore 和 clock 可直接注入；Scenario 可以独立增加。Tool、Context、Verifier、Workspace 和 Policy 边界已分离，但替换前仍需增加注册或构造注入点，详见[实现架构](docs/ARCHITECTURE.md#12-如何扩展而不破坏闭环)。

## Run 目录

```text
.agent-loop/runs/<run-id>/
├── manifest.json
├── state.json
├── events.jsonl
├── workspace/
├── artifacts/
└── applications/
```

`state.json` 是恢复事实源，事件是审计轨迹。若两者因崩溃不一致，恢复逻辑会补记恢复事件或进入人工检查，不会盲目重放未知副作用。

## 验证开发版本

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests scenarios
```

测试覆盖 Happy、Feedback、Budget、Approval、apply、严格模型输出、预算、路径逃逸、Verifier 协议、崩溃恢复和 Run 工作区隔离等关键不变量。

## v0 边界

- 不执行任意 shell 命令，不连接真实业务系统；
- 不自动合并、部署、发消息或删除目标文件；
- 不实现多 Agent 编排、分布式队列或容器级强隔离；
- Verifier 是场景作者提供的可信控制面代码；
- OpenAI SDK 是可选依赖，默认教程不需要 API Key。
- 安全 `apply` 依赖 POSIX 的 `O_NOFOLLOW`/`O_DIRECTORY`，v0 支持 macOS 和 Linux。

这些限制是有意保留的扩展缝：后续方案穿刺应优先替换一个接口并复用同一 LoopEngine 和事件协议。
