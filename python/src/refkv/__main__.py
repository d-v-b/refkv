"""The `refkv` command: `refkv zarrify <file or store> <out.vzip>` zarrifies a file or store
without copying it, writing an archive whose chunks are references to the input's bytes (the
same as `python -m refkv.zarrify`); `refkv convert <in> <out>` converts a vzip archive to a phkv
store or back."""

import sys
from urllib.parse import urlsplit

from refkv.zarrify.__main__ import main as zarrify

CONVERT_USAGE = """usage: refkv convert <in> <out> [--from vzip|phkv] [--to vzip|phkv]

Convert the store <in> (a local path or an http(s) URL) to <out>, a local path, in the other
binding. A name ending in .phkv is a phkv store, any other a vzip archive, unless --from or
--to says. References are copied, not resolved."""


def _binding(url: str) -> str:
    return "phkv" if urlsplit(url).path.lower().endswith(".phkv") else "vzip"


def convert(argv: list[str]) -> int:
    """`refkv convert`: 0 on success, 2 for a command line it does not take, 1 for a store it
    cannot read or write."""
    import refkv  # noqa: PLC0415 (a command that is not this one needs none of it)

    names: list[str] = []
    given: dict[str, str] = {}
    args = iter(argv)
    for a in args:
        if a in ("--from", "--to"):
            given[a[2:]] = next(args, "")
        elif a.startswith("-"):
            print(CONVERT_USAGE, file=sys.stderr)
            return 2
        else:
            names.append(a)
    if len(names) != 2 or any(b not in refkv.BINDINGS for b in given.values()):
        print(CONVERT_USAGE, file=sys.stderr)
        return 2
    src, dst = names
    from_, to = given.get("from", _binding(src)), given.get("to", _binding(dst))
    if from_ == to:
        print(f"refkv convert: both are {from_}; convert from one binding to the other "
              "(--from, --to, or the names' extensions)", file=sys.stderr)
        return 2
    try:
        summary = (refkv.vzip_to_phkv if from_ == "vzip" else refkv.phkv_to_vzip)(src, dst)
    except Exception as e:  # noqa: BLE001 (any failure of either store is the command's status)
        print(f"refkv convert: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(summary)
    return 0


COMMANDS = {"zarrify": zarrify, "convert": convert}
USAGE = ("usage: refkv zarrify <url or path> <out.vzip> [options] (refkv zarrify --help for the options)\n"
         "       refkv convert <in> <out> [--from vzip|phkv] [--to vzip|phkv]")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if args[:1] == ["convert"] and args[1:2] in (["-h"], ["--help"]):
        print(CONVERT_USAGE)
        return 0
    if not args or args[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    return COMMANDS[args[0]](args[1:])


if __name__ == "__main__":
    sys.exit(main())
