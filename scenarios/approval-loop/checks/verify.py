import json
from pathlib import Path


passed = Path("answer.txt").read_text(encoding="utf-8") == "approved\n"
result = {
    "criterion_id": "answer-present",
    "verdict": "pass" if passed else "fail",
    "message": "answer.txt 已批准" if passed else "answer.txt 尚未完成",
    "evidence": [],
}
print(json.dumps({"schema_version": 1, "results": [result]}, ensure_ascii=False))
raise SystemExit(0 if passed else 1)
