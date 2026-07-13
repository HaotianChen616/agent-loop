"""Trusted deterministic checks for the hello-loop scenario."""

import json
from pathlib import Path


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
print(json.dumps({"schema_version": 1, "results": results}, ensure_ascii=False))
raise SystemExit(0 if all(item["verdict"] == "pass" for item in results) else 1)
