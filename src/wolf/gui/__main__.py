"""`python -m wolf.gui` alias.

Equivalent to `python -m wolf.cli gui` with the user's cwd as
project root and the default 127.0.0.1:8765 bind.
"""

from __future__ import annotations

import sys

from ..cli import main as cli_main


def main(argv=None):
    return cli_main(["gui", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    sys.exit(main())
