"""The `refkv` command: `refkv zarrify <file or store> <out.vzip>` zarrifies a file or store
without copying it, writing an archive whose chunks are references to the input's bytes (the
same as `python -m refkv.zarrify`); `refkv convert <in> <out>` converts a vzip archive to a phkv
store or back."""

import argparse
import re
import sys
from typing import Any
from urllib.parse import urlsplit

from refkv.zarrify.__main__ import main as zarrify

_BINDINGS = ("vzip", "phkv")


def _binding(name: str) -> str:
    """The binding a name says: a .phkv name is phkv, any other vzip. A URL's path is what counts, not
    its query; a local path is taken whole, `#` and `?` included."""
    if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://", name):
        name = urlsplit(name).path
    return "phkv" if name.lower().endswith(".phkv") else "vzip"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="refkv convert",
        description="Convert the store <in> (a local path or an http(s) URL) to <out>, a local path, in "
                    "the other binding. A name ending in .phkv is a phkv store, any other a vzip "
                    "archive, unless --from or --to says. References are copied, not resolved. "
                    "Relative source URLs stay relative when they still resolve to the same place "
                    "from <out>, else they are written absolute.")
    p.add_argument("src", metavar="in")
    p.add_argument("dst", metavar="out")
    p.add_argument("--from", dest="from_", choices=_BINDINGS)
    p.add_argument("--to", choices=_BINDINGS)
    p.add_argument("--page-size", type=int, help="the page size of the page index written (to vzip: none by default, "
                                                 "65536 suits a store of many keys; to phkv: 4096)")
    p.add_argument("--allow-scheme", action="append", default=[], metavar="SCHEME",
                   help="a scheme the reader policy otherwise refuses, such as file, for a store whose sources "
                        "are local files (given more than once for more schemes)")
    p.add_argument("--relative-to", metavar="URL", help="write source URLs under this prefix relative to <out>")
    return p


def convert(argv: list[str]) -> int:
    """`refkv convert`: 0 on success, 2 for a command line it does not take, 1 for a store it
    cannot read or write."""
    import refkv  # noqa: PLC0415 (a command that is not this one needs none of it)

    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:  # argparse's: 0 for --help, 2 for a usage error
        return e.code if isinstance(e.code, int) else 2
    from_, to = args.from_ or _binding(args.src), args.to or _binding(args.dst)
    if from_ == to:
        print(f"refkv convert: both are {from_}; convert from one binding to the other "
              "(--from, --to, or the names' extensions)", file=sys.stderr)
        return 2
    options: dict[str, Any] = {"relative_to": args.relative_to}
    if args.allow_scheme:
        options["policy"] = refkv.Policy(schemes=frozenset(args.allow_scheme))
    if args.page_size is not None:
        options["page_size"] = args.page_size
    try:
        summary = (refkv.vzip_to_phkv if from_ == "vzip" else refkv.phkv_to_vzip)(args.src, args.dst, **options)
    except Exception as e:  # noqa: BLE001 (any failure of either store is the command's status)
        print(f"refkv convert: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(summary)
    return 0


COMMANDS = {"zarrify": zarrify, "convert": convert}
USAGE = ("usage: refkv zarrify <url or path> <out.vzip> [options] (refkv zarrify --help for the options)\n"
         "       refkv convert <in> <out> [--from vzip|phkv] [--to vzip|phkv] [--allow-scheme file]\n"
         "       (refkv convert --help for the options)")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if not args or args[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    return COMMANDS[args[0]](args[1:])


if __name__ == "__main__":
    sys.exit(main())
