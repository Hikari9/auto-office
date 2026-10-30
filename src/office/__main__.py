import sys


def entry() -> int:
    """The `office` console script and `python -m office` both start here (#920).

    An ImportError while `office.cli` loads means the install is being rebuilt under this process.
    Exit with a retry code and one line, instead of a traceback."""
    try:
        from office.cli import main
    except ImportError:
        from office import version
        if not version.rebuilding_reason():
            raise
        print(version.rebuild_line(), file=sys.stderr)
        return version.REBUILD_EXIT_CODE
    return main()


if __name__ == "__main__":
    raise SystemExit(entry())
