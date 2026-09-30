"""Synchronous LookupTax API client."""

from __future__ import annotations

import time
from typing import Any, Iterator, Optional, Sequence
from urllib.parse import urlencode

import httpx

from .errors import LookupTaxError, error_from_response
from .models import (
    BatchItem,
    BatchResults,
    TaxIdInput,
    ValidationResponse,
    ValidationSource,
)

#: Default API root. Overridable because the published path prefix is
#: deployment configuration, not a property of the SDK.
DEFAULT_BASE_URL = "https://api.lookuptax.com/v1"

_RETRYABLE = (429, 500, 503)


class LookupTax:
    """Client for the LookupTax API.

    Args:
        api_key: Your organization API key.
        base_url: API root. Point this wherever your account is served from.
        timeout: Per-request timeout in seconds.
        max_retries: Retries for transient failures (429/500/503), honouring
            ``Retry-After`` when present. ``0`` disables. Batch submission is
            never retried regardless — see :meth:`create_batch`.
        client: Inject your own ``httpx.Client`` for proxies, custom transports
            or tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        client: Optional[httpx.Client] = None,
    ) -> None:
        if not api_key:
            raise ValueError("LookupTax: api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    # ─── lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "LookupTax":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ─── HTTP ───────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        retry: bool = True,
    ) -> Any:
        url = self.base_url + path
        flat = _flatten_params(params or {})
        attempts = (self.max_retries + 1) if retry else 1
        last_exc: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                resp = self._client.request(
                    method,
                    url,
                    params=flat or None,
                    json=json_body,
                    headers={"X-API-Key": self.api_key, "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:  # network/timeout
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(2**attempt * 0.5)
                    continue
                raise LookupTaxError(str(exc), code="network_error") from exc

            if resp.status_code == 204:
                return None

            body = _safe_json(resp)

            # 422 is an OUTCOME, not a failure: the body still carries `result`
            # with status UNSUPPORTED. Return it so callers read it uniformly.
            if resp.is_success or (resp.status_code == 422 and isinstance(body, dict) and body.get("result")):
                return body

            retry_after = _retry_after(resp)
            err = error_from_response(resp.status_code, body, retry_after)
            if resp.status_code in _RETRYABLE and attempt < attempts - 1:
                last_exc = err
                time.sleep(retry_after if retry_after else 2**attempt * 0.5)
                continue
            raise err

        assert last_exc is not None  # pragma: no cover
        raise last_exc

    # ─── single validation ──────────────────────────────────────────

    def validate(
        self,
        country_iso: str,
        tin: str,
        *,
        reference_id: Optional[str] = None,
        validation_source: Optional[ValidationSource] = None,
        additional_params: Optional[dict[str, str]] = None,
    ) -> ValidationResponse:
        """Validate one tax ID.

        Returns for every validation outcome, **including INVALID** — read
        ``response.result.status``. Raises only when the request itself could
        not be processed (auth, quota, rate limit, malformed request).

        Args:
            validation_source: EU only. ``"vies"`` or ``"local"`` pins the
                answering authority with no fallback to the other family.
            additional_params: Extra registry params, e.g. ``{"name": "ACME"}``
                for Mexico.
        """
        data = self._request(
            "GET",
            "/validate",
            params={
                "country_iso": country_iso,
                "tin": tin,
                "reference_id": reference_id,
                "validation_source": validation_source,
                "additional_params": additional_params,
            },
        )
        return ValidationResponse.from_dict(data or {})

    # ─── batch ──────────────────────────────────────────────────────

    def create_batch(
        self,
        tax_ids: Sequence[TaxIdInput | dict[str, Any]],
        *,
        user_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        validation_source: Optional[ValidationSource] = None,
    ) -> dict[str, Any]:
        """Submit up to 100 tax IDs. Enterprise plan only.

        Never retried automatically: a retried submission that actually
        succeeded would create a second batch and reserve quota twice.
        """
        wire = [t.to_wire() if isinstance(t, TaxIdInput) else dict(t) for t in tax_ids]
        return self._request(
            "POST",
            "/batch",
            json_body={
                "tax_ids": wire,
                "user_id": user_id,
                "metadata": metadata,
                "validation_source": validation_source,
            },
            retry=False,
        )

    def get_batch(self, batch_id: str, *, limit: Optional[int] = None, cursor: Optional[int] = None) -> BatchResults:
        data = self._request("GET", f"/batch/{batch_id}", params={"limit": limit, "cursor": cursor})
        return BatchResults.from_dict(data or {})

    def list_batches(self, *, page: Optional[int] = None, limit: Optional[int] = None) -> dict[str, Any]:
        return self._request("GET", "/batch", params={"page": page, "limit": limit})

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        """Cancel a pending/processing batch. Reserved quota is released."""
        return self._request("POST", f"/batch/{batch_id}/cancel", retry=False)

    def delete_batch(self, batch_id: str) -> None:
        """Permanently delete a terminal batch. Cannot be undone."""
        self._request("DELETE", f"/batch/{batch_id}", retry=False)

    def get_tax_id(self, request_id: str) -> BatchItem:
        data = self._request("GET", f"/batch/tax-id/{request_id}")
        return BatchItem.from_dict(data or {})

    # ─── convenience ────────────────────────────────────────────────

    def wait_for_batch(
        self, batch_id: str, *, interval: float = 5.0, timeout: float = 1800.0
    ) -> BatchResults:
        """Poll until the batch reaches a terminal state.

        Polls on a relaxed cadence by design: when a registry is unreachable an
        item keeps retrying for roughly three days, so a long-running item is
        not a stuck one.
        """
        deadline = time.monotonic() + timeout
        while True:
            batch = self.get_batch(batch_id)
            if batch.is_terminal:
                return batch
            if time.monotonic() >= deadline:
                raise TimeoutError(f"batch {batch_id} still {batch.status} after {timeout}s")
            time.sleep(interval)

    def iterate_batch_results(self, batch_id: str, *, limit: Optional[int] = None) -> Iterator[BatchItem]:
        """Yield every result across all pages, following ``next_cursor``."""
        cursor: Optional[int] = None
        while True:
            page = self.get_batch(batch_id, limit=limit, cursor=cursor)
            yield from page.results
            if page.next_cursor is None:
                return
            cursor = page.next_cursor


def _flatten_params(params: dict[str, Any]) -> list[tuple[str, str]]:
    """Render nested dicts as ``additional_params[name]=…``.

    A flat ``?name=`` is stripped before it reaches routing, so the nesting is
    required rather than cosmetic.
    """
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                if inner_value is not None:
                    out.append((f"{key}[{inner_key}]", str(inner_value)))
        else:
            out.append((key, str(value)))
    return out


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"message": resp.text}


def _retry_after(resp: httpx.Response) -> Optional[float]:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
