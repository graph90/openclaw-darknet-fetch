"""Stable error-code taxonomy and exit-code mapping.

Exit-code ranges (documented in plan.md section 6):
0 ok, 1 usage, 2 proxy down, 3 timeout, 4 connection, 5 http/request, 6 app error.

The taxonomy is additive only: never rename an existing ``error_code`` string.
"""

# error_code -> process exit code
EXIT_CODES = {
    "OK": 0,
    "USAGE": 1,
    "SCHEME_BLOCKED": 1,
    "NETWORK_MISMATCH": 1,
    "PROXY_DOWN": 2,
    "TIMEOUT": 3,
    "CONNECTION": 4,
    "HTTP": 5,
    "REQUEST": 5,
    "EXPECT_MISMATCH": 5,
    "APP_ERROR": 6,
    "BODY_TOO_LARGE": 6,
    "BUDGET": 6,
    "HOST_BLOCKED": 6,
}

DEFAULT_EXIT_CODE = 6


def exit_code_for(error_code):
    """Return the process exit code for an ``error_code`` string."""
    return EXIT_CODES.get(error_code, DEFAULT_EXIT_CODE)


class FetchError(Exception):
    """A programmer/usage-level error that should never be returned as data.

    Runtime failures (proxy down, timeouts, bad status codes, oversized
    bodies, ...) are *not* raised: they are returned as ``ok: false`` results
    so agents can branch on them. ``FetchError`` is reserved for invariant
    violations that callers must fix (bad scheme, wrong network for target,
    non-string URL).
    """

    def __init__(self, error_code="USAGE", message="", cause=None):
        super().__init__(message)
        self.error_code = error_code
        self.exit_code = exit_code_for(error_code)
        self.cause = cause


from .result import Result


def error_result(error_code, message, requested_url=None, network=None, **extra):
    """Build a ``ok: false`` result for a given error code."""
    result = Result(
        ok=False,
        error=message,
        error_code=error_code,
        requested_url=requested_url,
        network=network,
    )
    result.update(extra)
    return result