import argparse
import json


def main():
    parser = argparse.ArgumentParser(description="秋窗客流分析账本")
    parser.add_argument("command", choices=["demo", "serve", "check"])
    parser.add_argument("--database", default="autumn-ledger.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "demo":
        from .demo import run_demo
        print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True))
    elif args.command == "check":
        from .application import Ledger
        result = Ledger(args.database).status()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if not result["ok"]:
            raise SystemExit(1)
    else:
        import uvicorn
        from .api.server import create_app
        uvicorn.run(create_app(args.database), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
