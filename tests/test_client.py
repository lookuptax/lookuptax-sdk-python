import httpx
import pytest

from lookuptax import (
    AuthenticationError,
    LookupTax,
    LookupTaxError,
    QuotaExceededError,
    RateLimitError,
    TaxIdInput,
)

VALID = {
    "referenceId": "r1",
    "countryCode": "IE",
    "tin": "53057102A",
    "result": {"status": "VALID", "reason": None, "message": "ok", "retryable": False},
    "tinInfo": {"label": "VAT", "name": "VAT number", "formattedTin": "IE53057102A"},
    "entity": {"name": "GRAFTON FOODS LIMITED", "status": "Active"},
}


def client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return LookupTax("k", client=httpx.Client(transport=transport), max_retries=0, **kwargs)


def json_handler(body, status=200, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.request = request  # type: ignore[attr-defined]
        return httpx.Response(status, json=body, headers=headers or {})

    return handler


class TestRequestShape:
    def test_sends_api_key_and_default_base_url(self):
        h = json_handler(VALID)
        client(h).validate("IE", "53057102A")
        assert h.request.headers["X-API-Key"] == "k"
        assert str(h.request.url).startswith("https://api.lookuptax.com/v1/validate")

    def test_custom_base_url_strips_trailing_slash(self):
        h = json_handler(VALID)
        client(h, base_url="https://api.lookuptax.com/").validate("IE", "1")
        assert "https://api.lookuptax.com/validate" in str(h.request.url)

    def test_nests_additional_params(self):
        h = json_handler(VALID)
        client(h).validate("MX", "X", additional_params={"name": "ACME"})
        assert "additional_params%5Bname%5D=ACME" in str(h.request.url) or \
               "additional_params[name]=ACME" in str(h.request.url)

    def test_forwards_validation_source(self):
        h = json_handler(VALID)
        client(h).validate("DE", "DE123", validation_source="vies")
        assert "validation_source=vies" in str(h.request.url)

    def test_omits_unset_params(self):
        h = json_handler(VALID)
        client(h).validate("IE", "1")
        assert "reference_id" not in str(h.request.url)


class TestOutcomesVsErrors:
    def test_invalid_returns_rather_than_raising(self):
        body = {**VALID, "result": {"status": "INVALID", "reason": "NOT_REGISTERED", "message": "", "retryable": False}}
        res = client(json_handler(body)).validate("IE", "x")
        assert res.result.status == "INVALID"
        assert res.result.is_valid is False

    def test_422_unsupported_returns_as_outcome(self):
        body = {**VALID, "result": {"status": "UNSUPPORTED", "reason": "COUNTRY_NOT_SUPPORTED", "message": "", "retryable": False}}
        res = client(json_handler(body, 422)).validate("ZZ", "x")
        assert res.result.status == "UNSUPPORTED"

    # UNVERIFIED means format-only — a pass is not proof the entity exists, so
    # is_valid must stay False even though the call succeeded.
    def test_unverified_is_not_valid(self):
        body = {**VALID, "result": {"status": "UNVERIFIED", "reason": "NO_SOURCE_AVAILABLE", "message": "", "retryable": False}}
        res = client(json_handler(body)).validate("EG", "1")
        assert res.result.is_valid is False
        assert res.result.should_retry is False

    def test_indeterminate_flags_retry(self):
        body = {**VALID, "result": {"status": "INDETERMINATE", "reason": "SOURCE_UNAVAILABLE", "message": "", "retryable": True}}
        res = client(json_handler(body)).validate("DE", "1")
        assert res.result.should_retry is True

    @pytest.mark.parametrize(
        "status,klass",
        [(401, AuthenticationError), (402, QuotaExceededError), (429, RateLimitError)],
    )
    def test_typed_errors(self, status, klass):
        with pytest.raises(klass):
            client(json_handler({"error": "e", "message": "m"}, status)).validate("IE", "1")

    def test_error_exposes_machine_code(self):
        with pytest.raises(QuotaExceededError) as exc:
            client(json_handler({"error": "quota_exceeded", "message": "m"}, 402)).validate("IE", "1")
        assert exc.value.code == "quota_exceeded"
        assert exc.value.status == 402

    def test_retry_after_surfaced(self):
        with pytest.raises(RateLimitError) as exc:
            client(json_handler({"error": "rl"}, 429, {"retry-after": "7"})).validate("IE", "1")
        assert exc.value.retry_after == 7.0

    def test_only_429_500_503_are_retryable(self):
        mk = lambda s: LookupTaxError("m", code="c", status=s)
        assert mk(429).is_retryable and mk(503).is_retryable
        assert not mk(400).is_retryable and not mk(402).is_retryable


class TestRetries:
    def test_retries_503_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, json={"error": "service_unavailable"})
            return httpx.Response(200, json=VALID)

        c = LookupTax("k", client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=1)
        assert c.validate("IE", "1").result.status == "VALID"
        assert calls["n"] == 2

    def test_batch_submission_never_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503, json={"error": "service_unavailable"})

        c = LookupTax("k", client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=3)
        with pytest.raises(LookupTaxError):
            c.create_batch([TaxIdInput("IE", "1")])
        assert calls["n"] == 1


class TestBatch:
    def test_maps_input_to_wire(self):
        h = json_handler({"batch_id": "b", "status": "pending", "total_count": 1})
        client(h).create_batch([TaxIdInput("MX", "X", name="ACME")], validation_source="local")
        import json as _json

        body = _json.loads(h.request.content)
        assert body["tax_ids"][0] == {"country_iso": "MX", "tin": "X", "name": "ACME"}
        assert body["validation_source"] == "local"

    def test_iterates_all_pages(self):
        pages = [
            {"results": [{"request_id": "1"}], "next_cursor": 1, "status": "completed"},
            {"results": [{"request_id": "2"}], "next_cursor": None, "status": "completed"},
        ]
        state = {"i": 0}

        def handler(request):
            body = pages[state["i"]]
            state["i"] += 1
            return httpx.Response(200, json=body)

        c = LookupTax("k", client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
        assert [i.request_id for i in c.iterate_batch_results("b")] == ["1", "2"]

    def test_delete_returns_none_on_204(self):
        def handler(request):
            return httpx.Response(204)

        c = LookupTax("k", client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
        assert c.delete_batch("b") is None


class TestConstruction:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="api_key is required"):
            LookupTax("")

    def test_context_manager(self):
        with LookupTax("k") as c:
            assert c.api_key == "k"
