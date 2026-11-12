"""The command line: ``python -m stockroom.cli --db PATH COMMAND ...``."""

import argparse
import json
import sys

from .store import Store, StoreError


def build_parser():
    parser = argparse.ArgumentParser(prog="stockroom")
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    add = commands.add_parser("add")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("quantity", type=int)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = Store(args.db)
    try:
        if args.command == "list":
            print(json.dumps({"items": store.list_items()}))
        elif args.command == "add":
            print(json.dumps(store.add_item(args.sku, args.name, args.quantity)))
    except StoreError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
