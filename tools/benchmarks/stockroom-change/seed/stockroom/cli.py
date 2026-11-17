"""The command line: ``python -m stockroom.cli --db PATH COMMAND ...``."""

import argparse
import json
import sqlite3
import sys

from .store import Store, StoreError


RESERVATION_COMMANDS = {"reserve", "release", "commit", "show", "availability"}


class _UsageError(Exception):
    pass


class _QuietParser(argparse.ArgumentParser):
    """Probe command selection without emitting argparse output."""

    def parse_known_args(self, args=None, namespace=None):
        if hasattr(self, "reservation_command"):
            self.selection["command"] = self.reservation_command
        return super().parse_known_args(args, namespace)

    def error(self, message):
        raise _UsageError(message)

    def exit(self, status=0, message=None):
        raise _UsageError(message or "help requested")

    def _print_message(self, message, file=None):
        pass


def _decimal(value):
    if not value or any(char < "0" or char > "9" for char in value):
        raise argparse.ArgumentTypeError("expected an unsigned ASCII decimal integer")
    # Chunk conversion also accepts integers beyond Python's string conversion limit.
    number = 0
    for start in range(0, len(value), 18):
        chunk = value[start:start + 18]
        number = number * 10 ** len(chunk) + int(chunk)
    return number


def build_parser(reservations=False, selection=None):
    parser_type = _QuietParser if reservations else argparse.ArgumentParser
    parser = parser_type(prog="stockroom")
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    add = commands.add_parser("add")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("quantity", type=int)
    if reservations:
        for command in sorted(RESERVATION_COMMANDS):
            subparser = commands.add_parser(command)
            subparser.reservation_command = command
            subparser.selection = selection
            if command == "reserve":
                subparser.add_argument("sku")
                subparser.add_argument("quantity", type=_decimal)
                subparser.add_argument("holder")
                subparser.add_argument("--ttl", type=_decimal, default=90)
            elif command == "availability":
                subparser.add_argument("sku")
            else:
                subparser.add_argument("id", type=_decimal)
    return parser


def _reservation_main(args):
    try:
        store = Store(args.db)
        if args.command == "reserve":
            result = store.reserve(args.sku, args.quantity, args.holder, args.ttl)
        elif args.command == "availability":
            result = store.availability(args.sku)
        else:
            method = {
                "release": store.release,
                "commit": store.commit,
                "show": store.get_reservation,
            }[args.command]
            result = method(args.id)
    except StoreError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}), file=sys.stderr)
        return 1
    except (sqlite3.Error, ValueError) as exc:
        message = str(exc).strip() or "database cannot be opened or used"
        print(json.dumps({"error": "usage", "message": message}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


def _command_word(argv):
    """Let argparse locate the command despite rejected top-level options."""
    parser = _QuietParser(add_help=False)
    # A missing database value must not stop command discovery. Otherwise
    # argparse consumes its value exactly as it does in the real parser.
    parser.add_argument("--db", nargs="?")
    # PARSER preserves the subparser boundary, including a literal "--".
    parser.add_argument("command", nargs=argparse.PARSER)
    try:
        args, _ = parser.parse_known_args(argv)
    except _UsageError:
        return None
    return args.command[0]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    selection = {}
    usage_error = None
    try:
        reservation_args = build_parser(True, selection).parse_args(argv)
    except _UsageError as exc:
        usage_error = str(exc)
        if not selection:
            selection["command"] = _command_word(argv)
    if selection.get("command") in RESERVATION_COMMANDS:
        if usage_error is not None:
            print(json.dumps({"error": "usage", "message": usage_error}), file=sys.stderr)
            return 2
        return _reservation_main(reservation_args)

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
