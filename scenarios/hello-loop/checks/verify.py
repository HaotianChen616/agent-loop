"""hello-loop Scenario 的可信、确定性验收脚本。

脚本在 Verifier 创建的一次性 Workspace 快照中运行，只输出约定的 JSON 协议。
它不读取 Agent 的声明，而是直接检查磁盘事实。
"""

import json
from pathlib import Path


# 两项标准分开报告：既要结果正确，也要证明 Agent 没有篡改题目要求。
requirements = Path("requirements.txt").read_text(encoding="utf-8")
implementation = Path("implementation.txt").read_text(encoding="utf-8")
results = [
    {
        "criterion_id": "requirements-file-unchanged",
        "verdict": "pass"
        if requirements == "implementation.txt 必须只包含一行：Hello, loop!\n"
        else "fail",
        "message": "requirements.txt 保持初始内容"
        if requirements == "implementation.txt 必须只包含一行：Hello, loop!\n"
        else "requirements.txt 被修改",
        "evidence": [],
    },
    {
        "criterion_id": "implementation-matches",
        "verdict": "pass" if implementation == "Hello, loop!\n" else "fail",
        "message": "implementation.txt 内容正确"
        if implementation == "Hello, loop!\n"
        else f"期望 'Hello, loop!'，实际为 {implementation.strip()!r}",
        "evidence": [],
    },
]
# stdout 必须只有协议 JSON；整体 pass/fail 还要与进程退出码 0/1 一致。
print(json.dumps({"schema_version": 1, "results": results}, ensure_ascii=False))
raise SystemExit(0 if all(item["verdict"] == "pass" for item in results) else 1)
