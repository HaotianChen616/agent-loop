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

详细设计见 [docs/DESIGN.md](docs/DESIGN.md)。

## 最快开始

要求 Python 3.12 或更高版本。项目核心只使用标准库：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

运行默认的 Feedback Path：

```bash
agent-loop run scenarios/hello-loop/scenario.toml
```

控制台会逐项展示 Context 构建、Agent 提案、Policy 授权、Tool 执行、Verifier 证据和 Stop Decision。运行数据保存在 `.agent-loop/runs/<run-id>/`。

## 三条可重复教学路径

```bash
# Happy Path：一次修改后验证通过
agent-loop run scenarios/hello-loop/happy.toml

# Feedback Path：第一次失败，吸收证据后修正
agent-loop run scenarios/hello-loop/scenario.toml

# Budget Path：重复无效动作，被停止策略终止
agent-loop run scenarios/hello-loop/budget.toml
```

Budget Path 以非零状态退出是预期行为；它演示的是“失败也必须有界”。加入 `--step` 可在每个事件后暂停，适合课堂讲解。

## 审批、恢复和检查

审批场景使用一个不会访问真实外部系统的 mock 工具：

```bash
agent-loop run scenarios/approval-loop/scenario.toml --run-id approval-demo
agent-loop resume approval-demo --approve
# 或：agent-loop resume approval-demo --reject
```

批准与动作 ID、参数和工作区摘要绑定；恢复时只执行保存的同一个动作一次。查看任意 Run 的事实时间线：

```bash
agent-loop inspect approval-demo
```

`inspect` 只读取持久事件，不调用模型重新解释历史。

## 将验证结果应用到目标目录

Agent 始终修改隔离工作区。只有已完成且拥有通过证据的 Run 才能显式应用：

```bash
mkdir -p ./result
agent-loop apply <run-id> ./result
```

命令会先展示新增和修改文件，再请求确认。自动化环境可以传 `--yes`，它仍然属于显式确认。确认前后都会复核工作区摘要、目标文件前置状态和符号链接；每次操作单独记录在 `applications/<application-id>.json`，不会改写已完成 Run 的状态或事件。

v0 不传播文件删除，也不提供 `--force`。

## 使用真实模型

确定性 `ScriptedAgent` 用于教程和 CI；OpenAI Responses Adapter 用于观察非确定性行为：

```bash
python -m pip install -e '.[openai]'
export OPENAI_API_KEY='...'
agent-loop run scenarios/hello-loop/scenario.toml \
  --agent llm --model <model-id>
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

`LoopEngine` 是唯一生命周期所有者。Provider、Scenario、工具和 Verifier 都通过边界隔离，方便后续一次替换一个组件做对比实验。

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

测试覆盖 Happy、Feedback、Budget、Approval、apply、严格模型输出、预算、路径逃逸、Verifier 协议、崩溃恢复和并发隔离等关键不变量。

## v0 边界

- 不执行任意 shell 命令，不连接真实业务系统；
- 不自动合并、部署、发消息或删除目标文件；
- 不实现多 Agent 编排、分布式队列或容器级强隔离；
- Verifier 是场景作者提供的可信控制面代码；
- OpenAI SDK 是可选依赖，默认教程不需要 API Key。

这些限制是有意保留的扩展缝：后续方案穿刺应优先替换一个接口并复用同一 LoopEngine 和事件协议。
