#!/usr/bin/env python
"""McPilot Web 便捷启动脚本（Phase 4）。

用法（Windows / macOS / Linux 通用）::

    python scripts/run_web.py                 # 默认 http://127.0.0.1:8765
    python scripts/run_web.py --port 9000
    python scripts/run_web.py --verbose       # 打印访问日志

该脚本会把 ``src/`` 加入 import 路径，因此无需手动设置 PYTHONPATH。
**不含任何凭据**——MCP 端点与令牌由 ``mcpilot.config`` 在运行时解析。
"""

from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from mcpilot.web.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
