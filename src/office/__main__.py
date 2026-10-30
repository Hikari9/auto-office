import sys


def entry() -> int:
    """The `office` console script and `python -m office` both start here (#920).

    An ImportError or OSError while `office.cli` loads, or one that escapes a command, can mean the install
    is being replaced under this process. Exit with a retry code and one line instead of a traceback. Any
    other failure re-raises."""
    try:
        from office.cli import main
        return main()
    except (ImportError, OSError):
        try:
            from office import version
            reason, code, line = version.rebuilding_reason(fresh=True), version.REBUILD_EXIT_CODE, version.rebuild_line()
        except (ImportError, OSError):  # version.py itself cannot load: the install is being replaced
            reason, code, line = "version unloadable", 75, "office: install is being rebuilt, retry in 5 s"
        if reason is None:
            raise
        print(line, file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(entry())
