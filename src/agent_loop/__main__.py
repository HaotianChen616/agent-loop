"""Allow `python -m agent_loop` to behave like the console script."""

from .cli import main


raise SystemExit(main())
