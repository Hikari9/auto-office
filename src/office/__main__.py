import sys


def entry() -> int:
    """The `office` console script and `python -m office` both start here (#920).

    A failed import of `office.cli` from inside the office package means the install is being replaced as this process
    starts, or its files are damaged: exit 75 with one line naming the cause. A dependency outside the office package
    does not heal by retrying: exit 1 with the cause and a reinstall hint. A command that fails after it has started is
    not converted: its side effects may already have happened, so a retry could repeat them."""
    try:
        from office.cli import main
    except (ImportError, OSError, SyntaxError, ValueError) as exc:
        return _startup_failure(exc)
    return main()


def _startup_failure(exc: Exception) -> int:
    cause = _cause(exc)
    if _is_dependency_failure(exc):
        print(f"office: cannot start, a dependency failed to import ({cause}); "
              "reinstall auto-office with `uv tool install --force auto-office`", file=sys.stderr)
        return 1
    try:
        from office import version
        code, line = version.REBUILD_EXIT_CODE, version.rebuild_line(cause)
    except (ImportError, OSError, SyntaxError, ValueError):
        # version.py imports only the standard library, so when it cannot load, its file is being replaced or damaged.
        code, line = 75, f"office: install is being rebuilt, retry in 5 s ({cause})"
    print(line, file=sys.stderr)
    return code


def _is_dependency_failure(exc: Exception) -> bool:
    """A module outside the office package that does not import (A15)."""
    return isinstance(exc, ImportError) and exc.name is not None and exc.name.split(".")[0] != "office"


def _cause(exc: BaseException) -> str:
    """The exception type and the module it names. version.failure_cause keeps the same form when version.py loads."""
    module = getattr(exc, "name", None) or getattr(exc, "filename", None) or "office.cli"
    return f"{type(exc).__name__}: {module}"


if __name__ == "__main__":
    raise SystemExit(entry())
