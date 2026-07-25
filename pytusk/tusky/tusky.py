#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""tusky CLI entry point.

This is currently a parser-only scaffold: it builds the argument parser and
prints the parsed arguments. Dispatch to command handlers (tusky_cmds.py)
and PytuskConfiguration/WalrusClient wiring are added in a follow-up once
the PytuskConfiguration initialization issue is resolved.
"""

import sys

from pytusk.tusky.tusky_args import build_parser


def main() -> None:
    """Parse CLI arguments and print the resulting namespace.

    This is a placeholder entry point for exercising the argparse structure
    (`tusky -h`, `tusky <command> -h`) before command dispatch is wired in.
    """
    parsed = build_parser(in_args=sys.argv[1:])
    print(parsed)


if __name__ == "__main__":
    main()
