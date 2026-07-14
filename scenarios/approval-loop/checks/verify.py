"""approval-loop Scenario 的可信验收脚本。

外部写入工具只是审批教学替身；真正的完成条件仍由 Workspace 中的 answer.txt 决定。
"""

import json
from pathlib import Path


# Verifier 直接读取快照内容，Agent 或审批事件本身都不能替代文件证据。
passed = Path("answer.txt").read_text(encoding="utf-8") == "approved\n"
result = {
    "criterion_id": "answer-present",
    "verdict": "pass" if passed else "fail",
    "message": "answer.txt 已批准" if passed else "answer.txt 尚未完成",
    "evidence": [],
}
# 结果 ID 必须与 Scenario 的 acceptance_criteria 精确对应，退出码也必须匹配 verdict。
print(json.dumps({"schema_version": 1, "results": [result]}, ensure_ascii=False))
raise SystemExit(0 if passed else 1)
