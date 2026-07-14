# MaaS Provider 指南

`MaaSAgent` 负责把模型输出校验为 `AgentDecision`，`providers/` 只负责不同模型服务协议。这样 Provider 不接触 Tool、Workspace、Policy、RunState 或停止条件。

## 组件关系

```text
AgentContext
  → MaaSAgent
  → MaaSProvider.complete(instructions, prompt, schema)
  → MaaSResponse(output_text, normalized_usage)
  → AgentDecision.from_mapping
  → LoopEngine / Policy / Tool / Verifier
```

共享契约位于 `src/agent_loop/providers/base.py`：

- `MaaSProvider`：Provider 名称、模型 ID 和一次结构化补全；
- `MaaSResponse`：JSON 文本与可选的标准化 Token 用量；
- 标准用量字段：`input_tokens`、`output_tokens`、`total_tokens`。

模型返回的 JSON、`arguments` 和未知字段仍由 `MaaSAgent` 与 `AgentDecision` 二次校验。Provider 的 JSON 模式不能替代本地验证。

## OpenAI Responses Provider

```bash
python -m pip install -e '.[openai]'
export OPENAI_API_KEY='...'
agent-loop run scenarios/hello-loop/scenario.toml \
  --agent llm --provider openai --model your-model-id
```

它使用严格 JSON Schema、`store=False`、请求超时和输出上限。SDK 重试被关闭，错误由有预算和审计的外层 Loop 处理。

## 智谱 GLM Coding Plan Provider

安装共享的 OpenAI 协议客户端并配置 Key：

```bash
python -m pip install -e '.[zhipu]'
export ZAI_API_KEY='...'
agent-loop run scenarios/hello-loop/zhipu.toml
```

也可以覆盖 Scenario：

```bash
agent-loop run scenarios/hello-loop/scenario.toml \
  --agent llm \
  --provider zhipu-coding-plan \
  --model glm-5.1
```

实现使用智谱 Coding Plan 的 OpenAI Chat Completions 兼容端点：

```text
https://open.bigmodel.cn/api/coding/paas/v4
```

请求使用 Bearer API Key、`response_format={"type":"json_object"}`、非流式输出、显式超时和 `max_tokens`。智谱的 `prompt_tokens/completion_tokens` 会归一化为公共字段。

Coding Plan 是有使用范围的订阅服务。接入前应确认当前账号、套餐条款和工具授权允许本项目；若不适用，应使用智谱通用 API 套餐，而不是复用 Coding Plan 额度。官方参考：

- [Coding Plan 快速开始](https://docs.bigmodel.cn/cn/coding-plan/quick-start)
- [其他编码工具接入](https://docs.bigmodel.cn/cn/coding-plan/tool/others)
- [HTTP API 与 Coding 专属端点](https://docs.bigmodel.cn/cn/guide/develop/http/introduction)
- [结构化输出](https://docs.bigmodel.cn/cn/guide/capabilities/struct-output)

## 新增 Provider

1. 在 `providers/` 中实现 `MaaSProvider`；
2. 把协议特有状态、拒绝/截断判断和 Token 字段转换封装在 Provider 内；
3. 在 `providers.PROVIDER_NAMES` 和 `create_provider()` 注册；
4. 在 `config.py`/CLI 暴露稳定名称；
5. 使用 fake client 测试请求形态、错误路径和用量归一化，禁止单元测试访问真实服务；
6. 确保 SDK 自动重试关闭，避免绕过 LoopEngine 预算；
7. 在 manifest 中冻结 Provider 与 model，resume 时拒绝切换。

Provider 实现是与 Loop 同进程运行的可信代码，拥有宿主权限；只有 Provider 返回的模型数据被视为不可信。不要记录 API Key，也不要把包含秘密的 workspace 文件发送给外部 Provider。
