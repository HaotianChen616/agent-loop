# 方案穿刺指南

本项目适合用来快速验证 Agent Loop 设计，但每次实验应只改变一个主要变量。否则结果无法说明提升来自模型、Context、工具、验证还是预算。

## 1. 实验卡片

开始编码前，先复制并填写：

```markdown
# <实验名称>

## 假设
如果把 <组件 A> 从 <基线> 换成 <候选方案>，那么 <指标> 会改善，因为 <原因>。

## 固定项
- Scenario 与 digest：
- fixture / acceptance criteria / verifier：
- Agent 或 ScriptedAgent 动作：
- 工具和 Policy：
- Budget：

## 唯一变量
- 替换组件：
- 基线实现：
- 候选实现：

## 判定条件
- 主要指标：
- 可接受退化：
- 提前停止条件：

## 结果
- Run IDs：
- 观察：
- 结论：保留 / 放弃 / 继续实验
```

## 2. 推荐工作流

1. 使用 ScriptedAgent 跑通 Scenario，证明控制循环和 Verifier 本身稳定；
2. 保存基线 Run 的 manifest、最终 state 和 events；
3. 只替换一个组件或参数；
4. 使用相同成功标准和硬预算重复运行；
5. 对比事实数据，不用 Agent 自评作为指标；
6. 把结论和失败样本写入实验记录，再决定是否进入基线。

当前最便宜的实验变量是 Scenario/Skill、ScriptedAgent 动作、LLM Agent 和预算。Tool 需要修改注册表，Context/Verifier/Workspace/Policy 需要先增加注入点；具体成本见 [ARCHITECTURE.md](ARCHITECTURE.md#12-如何扩展而不破坏闭环)。

## 3. 当前可直接读取的指标

| 指标 | 数据来源 |
|---|---|
| 最终状态、停止原因 | `state.json` |
| iteration、Agent/Tool/Verification 次数 | `state.json.budget_usage` |
| 每轮事件类型、摘要、授权、验证和停止决定 | `events.jsonl` |
| Agent token usage（Provider 返回时） | Agent 相关 event 的 `usage` |
| Verifier 耗时和原始证据 | event `duration_ms`、`artifacts/verification/` |
| 重复失败次数与最终反馈 | `state.json` |
| Apply 预览、确认、结果和部分写入 | `applications/*.json` |

当前不会自动记录 git commit、Python/依赖版本、组件版本、累计费用或跨 resume 总墙钟，也不保留全部历史动作的结构化参数；events 只有摘要，state 只有最近动作/结果。需要这些信息时，应由实验脚本在 Run 外单独保存。

## 4. 常见实验与控制变量

| 问题 | 改变 | 应固定 | 重点指标 |
|---|---|---|---|
| 模型 A 是否优于 B | Agent/model | Scenario、Context、预算 | 成功率、调用数、Token |
| Skill 是否有效 | `skill.md` | Agent、fixture、Verifier | 规则违反、迭代数 |
| Plan-first 是否有效 | AgentAdapter | 工具、成功标准、预算 | 无效动作、首次通过率 |
| 重试策略是否更好 | StopPolicy | 失败样本、Agent | 无进展轮数、误停率 |
| 新 Verifier 是否增益 | Verifier | workspace 结果集 | 假阳性、假阴性、耗时 |
| 容器是否值得 | Workspace | Scenario、Agent、Verifier | 隔离强度、启动耗时 |

LLM 实验具有非确定性。至少保存每次 Run ID，进行多次重复，并同时报告失败样本；不要只展示最好的一次。

## 5. 进入基线的门槛

候选方案只有在以下条件都满足时才适合合入：

- 没有削弱“Verifier 才能完成”的核心不变量；
- 新副作用仍经过 allowlist、Policy、预算和 in-flight checkpoint；
- 暂停、失败与恢复语义明确，不会无限重试；
- 新状态和证据可持久化、检查和比较；
- Happy、Feedback、Budget 以及相关安全测试通过；
- 收益足以覆盖新增抽象和教学复杂度。
