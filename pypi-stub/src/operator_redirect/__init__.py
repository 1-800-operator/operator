import sys


REDIRECT_MESSAGE = """\
operator is not pip-installable.

Install with:

    curl -sSf https://1-800-operator.com/install | sh

The pip-installable package is reserved as a placeholder so this name
cannot be claimed by an unrelated project. operator itself includes a
browser, persistent profile, and other components that pip cannot manage.
"""


def main() -> int:
    sys.stderr.write(REDIRECT_MESSAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
