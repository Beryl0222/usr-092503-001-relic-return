"""服务启动入口：python -m src.relic_case.server --db ./ledger.db --port 8080"""

from __future__ import annotations

import argparse

from .httpapi import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="流失文物返还协作台账服务")
    parser.add_argument("--db", default="relic_ledger.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    httpd = create_server(args.db, host=args.host, port=args.port)
    print(f"台账服务已启动: http://{args.host}:{args.port}  数据库: {args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        httpd.engine.store.close()


if __name__ == "__main__":
    main()
