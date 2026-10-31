import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


def entry() -> int:
    """The `office` console script and `python -m office` both start here (#920).

    A failed import of `office.cli` from inside the office package means the install is being replaced as this process
    starts, or its files are damaged: exit 75 with one line naming the cause. A failure whose origin is outside the office
    package does not heal by retrying: exit 1 with the cause and a reinstall hint. A command that fails after it has
    started is not converted: its side effects may already have happened, so a retry could repeat them."""
    try:
        from office.cli import main
    except (ImportError, OSError, SyntaxError, ValueError) as exc:
        return _startup_failure(exc)
    return main()


def _startup_failure(exc: Exception) -> int:
    cause = _cause(exc)
    if _is_dependency_failure(exc):
        print(f"office: cannot start, a dependency failed to import ({cause}). Reinstall auto-office from its source: "
              "`uv tool install --force --editable <checkout>` for a checkout, or `uv tool install --force auto-office` "
              "for a release (keep any extras). If a reinstall is already running, retry after it finishes.",
              file=sys.stderr)
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
    """True when the failure originates outside the office package (A15)."""
    if isinstance(exc, ImportError) and exc.name is not None:
        return exc.name.split(".")[0] != "office"
    origin = _origin(exc)
    return origin is not None and not _inside_package(origin)


def _origin(exc: BaseException) -> str | None:
    """The file the failure came from: the exception's own filename, else the innermost frame outside importlib."""
    filename = getattr(exc, "filename", None)
    if filename:
        return str(filename)
    origin = None
    tb = exc.__traceback__
    while tb is not None:
        if not tb.tb_frame.f_code.co_filename.startswith("<frozen"):
            origin = tb.tb_frame.f_code.co_filename
        tb = tb.tb_next
    return origin


def _inside_package(origin: str) -> bool:
    return Path(origin).resolve().is_relative_to(PACKAGE)


def _cause(exc: BaseException) -> str:
    """The exception type and the module it names. `version.failure_cause` gives the same form when version.py loads."""
    module = getattr(exc, "name", None) or _module_of(_origin(exc)) or "office"
    return f"{type(exc).__name__}: {module}"


def _module_of(origin: str | None) -> str | None:
    if origin is None:
        return None
    path = Path(origin).resolve()
    if path.is_relative_to(PACKAGE):
        return ".".join(["office", *path.relative_to(PACKAGE).with_suffix("").parts])
    return path.name


if __name__ == "__main__":
    raise SystemExit(entry())
