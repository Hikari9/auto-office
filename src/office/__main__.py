import sys


def entry() -> int:
    """The `office` console script and `python -m office` both start here (#920).

    A failed import of `office.cli` means the install is being replaced as this process starts, or its files are
    damaged. Exit with a retry code and one line instead of a traceback. A command that fails after it has started
    is not converted: its side effects may already have happened, so a retry could repeat them."""
    try:
        from office.cli import main
    except (ImportError, OSError, SyntaxError, ValueError):
        try:
            from office import version
            code, line = version.REBUILD_EXIT_CODE, version.rebuild_line()
        except (ImportError, OSError, SyntaxError, ValueError):
            # version.py imports only the standard library, so when it cannot load, its file is being replaced or damaged.
            code, line = 75, "office: install is being rebuilt, retry in 5 s"
        print(line, file=sys.stderr)
        return code
    return main()


if __name__ == "__main__":
    raise SystemExit(entry())
