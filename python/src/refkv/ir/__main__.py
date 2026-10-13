"""python -m refkv.ir <url or path> <out.vzip> [--url <source url>] [--no-mirror]
[--allow-private-hosts]: as python -m refkv.virtualize, through the IR (exit status 3
when rejected). Every format reads through the read planner, under the reader
policy (spec/archive.md §6.7
<https://github.com/d-v-b/refkv/blob/main/spec/archive.md#67-reader-policy>): a source on a loopback
or private host needs
--allow-private-hosts."""

import json
import sys

from refkv.errors import ResolutionError
from refkv.ir import virtualize
from refkv.policy import Policy
from refkv.virtualize.common import Rejected


def main(argv: list[str]) -> int:
    args = list(argv)
    url, mirror = None, "--no-mirror" not in args
    policy = Policy(allow_private_hosts="--allow-private-hosts" in args)
    args = [a for a in args if a not in ("--no-mirror", "--planner", "--allow-private-hosts")]
    if "--url" in args:
        i = args.index("--url")
        url = args[i + 1]
        del args[i:i + 2]
    try:
        fmt, out, ir = virtualize(args[0], url, mirror, policy)
    except Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    except ResolutionError as e:  # the reader policy refused the source
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:  # the archive's sources are checked against the policy the input was read under
        out.to_store(policy=policy).save(args[1], policy=policy)
    except ResolutionError as e:  # a source a reader under that policy could not read
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"format": fmt, "elements": len(ir), **out.summary}))
    return 0


sys.exit(main(sys.argv[1:]))
