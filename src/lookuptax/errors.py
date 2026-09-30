"""Request errors.

The API separates two kinds of failure, and so does this SDK:

* A **request error** (HTTP 4xx/5xx) means the call could not be processed —
  authentication, quota, rate limit, a malformed request. These **raise**.
* A **validation outcome** (HTTP 200) means the call succeeded but the tax ID
  itself is invalid or could not be confirmed. These **return**, and you read
  ``result.status``.

Mixing the two is the most common integration mistake: an ``INVALID`` tax ID is
a successful call, not an error.
"""

from __future__ import annotations

from typing import Any, Optional


class LookupTaxError(Exception):
    """Base class for every request error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        status: int = 0,
        retry_after: Optional[float] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        #: Stable machine-readable code. Branch on this, never on the message.
        self.code = code
        self.status = status
        #: Seconds to wait, from the ``Retry-After`` header. Only set for 429.
        self.retry_after = retry_after
        self.body = body

    @property
    def is_retryable(self) -> bool:
        """Whether retrying the identical request could plausibly succeed."""
        return self.status in (429, 500, 503)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"{type(self).__name__}(code={self.code!r}, status={self.status})"


class AuthenticationError(LookupTaxError):
    """The API key is missing, malformed, revoked or expired."""


class QuotaExceededError(LookupTaxError):
    """Monthly quota exhausted, or a batch could not be reserved."""


class RateLimitError(LookupTaxError):
    """Too many requests per second. Back off for ``retry_after`` seconds."""


class InvalidRequestError(LookupTaxError):
    """A required parameter is missing or malformed."""


class NotFoundError(LookupTaxError):
    """No batch or tax ID exists with that identifier."""


_BY_STATUS = {
    400: InvalidRequestError,
    401: AuthenticationError,
    402: QuotaExceededError,
    403: AuthenticationError,
    404: NotFoundError,
    429: RateLimitError,
}


def error_from_response(status: int, body: Any, retry_after: Optional[float] = None) -> LookupTaxError:
    """Map an HTTP failure onto the most specific error class.

    Note 422 never reaches here: its body still carries a ``result`` with
    status ``UNSUPPORTED``, so the client returns it as an outcome.
    """
    data = body if isinstance(body, dict) else {}
    code = data.get("error") or data.get("code") or f"http_{status}"
    message = data.get("message") or f"Request failed with HTTP {status}"
    klass = _BY_STATUS.get(status, LookupTaxError)
    return klass(message, code=code, status=status, retry_after=retry_after, body=body)
