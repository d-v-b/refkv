"""The `refkv` command: `refkv zarrify <file or store> <out.vzip>` zarrifies a file or store
without copying it, writing an archive whose chunks are references to the input's bytes (the
same as `python -m refkv.zarrify`)."""

import sys

from refkv.zarrify.__main__ import main as zarrify

COMMANDS = {"zarrify": zarrify}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in COMMANDS:
        print("usage: refkv zarrify <url or path> <out.vzip> [options]", file=sys.stderr)
        return 2
    return COMMANDS[args[0]](args[1:])


if __name__ == "__main__":
    sys.exit(main())
