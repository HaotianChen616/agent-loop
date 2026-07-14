"""让 `python -m agent_loop` 与安装后的 `agent-loop` 命令行为一致。"""

from .cli import main


raise SystemExit(main())
