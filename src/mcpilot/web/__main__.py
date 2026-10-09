"""``python -m mcpilot.web`` 入口。"""

from __future__ import annotations

import argparse
import sys

from .app import serve


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="McPilot Web 服务（真实 MCP 后端）")
    p.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅本机 127.0.0.1）")
    p.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    p.add_argument("--verbose", action="store_true", help="打印访问日志")
    args = p.parse_args(argv)
    serve(host=args.host, port=args.port, verbose=args.verbose)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
