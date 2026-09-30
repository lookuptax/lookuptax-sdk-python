# LookupTax SDK for Python

Validate tax IDs — VAT, EIN, GSTIN, ABN and more — against official government registries.

```bash
pip install lookuptax
```

Python 3.9+.

## Quick start

```python
from lookuptax import LookupTax

with LookupTax(api_key="...") as lookuptax:
    res = lookuptax.validate("IE", "53057102A")

    if res.result.status == "VALID":
        print("Confirmed:", res.entity.name)
```

## Read `result.status`, not `isValid`

Every response carries a `result` whose `status` is the single source of truth. Five mutually exclusive values:

| `result.status` | Meaning |
|---|---|
| `VALID` | Format passed and a government registry confirmed the entity. |
| `INVALID` | Definitive negative — bad structure/check digit, or no registry record. |
| `UNVERIFIED` | No queryable registry for that country, so only the format was checked. A pass is **not** proof the business exists. |
| `INDETERMINATE` | A registry was temporarily unreachable. Retry — do not treat as invalid. |
| `UNSUPPORTED` | We don't validate that country or number type yet. |

Do **not** branch on `validation["overall"]["isValid"]`: it is true for `VALID`, `UNVERIFIED` **and** `INDETERMINATE` alike, so a format-only pass looks identical to a registry-confirmed one.

Two helpers encode that correctly:

```python
res.result.is_valid      # True only for VALID — UNVERIFIED is not "valid"
res.result.should_retry  # True only for INDETERMINATE
```

```python
match res.result.status:
    case "VALID":         accept(res.entity)
    case "INVALID":       reject(res.result.reason)
    case "UNVERIFIED":    accept_with_caveat()   # format checked only
    case "INDETERMINATE": retry_later()          # registry was down
    case "UNSUPPORTED":   skip()
```

## Errors vs outcomes

The SDK mirrors the API's split:

- **Validation outcomes return.** An `INVALID` tax ID is a *successful* call — you get a response, not an exception. `UNSUPPORTED` returns too, even though it arrives as HTTP 422, because the body still carries a `result`.
- **Request errors raise.** Authentication, quota, rate limit, malformed request — the call could not be processed at all.

```python
from lookuptax import LookupTaxError, QuotaExceededError, RateLimitError

try:
    res = lookuptax.validate("IE", "53057102A")
except RateLimitError as e:
    time.sleep(e.retry_after or 1)
except QuotaExceededError:
    notify_billing()
except LookupTaxError as e:
    log.error("%s (%s)", e.code, e.status)
```

Branch on `e.code` (a stable machine-readable string), never on the message.

Transient failures — 429, 500, 503 — retry automatically (2 attempts by default, honouring `Retry-After`). Pass `max_retries=0` to opt out. Batch submission is **never** retried automatically, because a retried submission that actually succeeded would create a second batch and reserve quota twice.

## EU routing

```python
lookuptax.validate("DE", "DE123456789", validation_source="vies")   # VIES only
lookuptax.validate("DE", "DE123456789", validation_source="local")  # national registry only
```

With `vies` or `local` there is no fallback to the other — if the requested source cannot answer you get `INDETERMINATE` rather than a silent answer from the other one, which is what makes it usable for reconciliation. Default is `auto`. Ignored outside VIES-covered countries.

## Countries needing a name

```python
lookuptax.validate("MX", "XAXX010101000", additional_params={"name": "Empresa Ejemplo SA de CV"})
```

## Batch

Up to 100 IDs, validated asynchronously. Enterprise plan only.

```python
from lookuptax import TaxIdInput

batch = lookuptax.create_batch([
    TaxIdInput("IE", "53057102A"),
    TaxIdInput("MX", "XAXX010101000", name="Empresa Ejemplo SA de CV"),
])

done = lookuptax.wait_for_batch(batch["batch_id"])

for item in lookuptax.iterate_batch_results(batch["batch_id"]):
    print(item.tin, item.validation_result.result.status if item.validation_result else item.status)
```

`wait_for_batch` polls on a relaxed cadence by design. When a registry is unreachable an item keeps retrying for roughly three days, carrying a `next_retry_at` — a long-running item is not a stuck one.

## Configuration

```python
LookupTax(
    api_key="...",                              # required
    base_url="https://api.lookuptax.com/v1",    # default
    timeout=30.0,                               # seconds, per request
    max_retries=2,                              # 429/500/503 only
    client=httpx.Client(...),                   # inject a proxy/transport, or for tests
)
```

`base_url` is configurable because the published path prefix is deployment configuration rather than a property of the SDK.

## API

| Method | Endpoint |
|---|---|
| `validate(country_iso, tin, ...)` | `GET /validate` |
| `create_batch(tax_ids, ...)` | `POST /batch` |
| `get_batch(batch_id, ...)` | `GET /batch/{id}` |
| `list_batches(...)` | `GET /batch` |
| `cancel_batch(batch_id)` | `POST /batch/{id}/cancel` |
| `delete_batch(batch_id)` | `DELETE /batch/{id}` |
| `get_tax_id(request_id)` | `GET /batch/tax-id/{id}` |
| `wait_for_batch(batch_id, ...)` | polls to a terminal state |
| `iterate_batch_results(batch_id, ...)` | generator over every page |

## License

MIT
