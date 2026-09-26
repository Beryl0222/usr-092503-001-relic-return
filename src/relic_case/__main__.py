"""启动 HTTP 服务：python3 -m src.relic_case --db relic_case.db --port 8080"""

from __future__ import annotations

import argparse

from .api import make_server
from .service import CaseService
from .store import EventStore


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="relic_case", description="流失文物返还协作台账服务"
    )
    parser.add_argument("--db", default="relic_case.db", help="SQLite 数据文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    service = CaseService(EventStore(args.db))
    server = make_server(service, args.host, args.port)
    print(f"流失文物返还协作台账服务已启动: http://{args.host}:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
