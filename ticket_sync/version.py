"""Package version and smoke-test entry point."""

__version__ = "0.1.0"


def version() -> str:
    """Return a greeting string. Used as a smoke test for the package.

    Returns:
        A fixed string confirming the package is importable.

    >>> version()
    'hello ticket'
    """
    return "hello ticket"
