"""Response models.

Deliberately dataclasses over a validation library: the SDK should not reject a
response because the API added a field. Unknown keys are preserved in ``raw`` so
a new field is reachable before the SDK is updated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

ValidationStatus = Literal["VALID", "INVALID", "UNVERIFIED", "INDETERMINATE", "UNSUPPORTED"]
ValidationSource = Literal["auto", "vies", "local"]
BatchStatus = Literal["pending", "processing", "completed", "failed", "canceled"]


@dataclass(frozen=True)
class ValidationResult:
    """The canonical outcome. ``status`` is the single source of truth."""

    status: ValidationStatus
    reason: Optional[str]
    message: str
    retryable: bool

    @property
    def is_valid(self) -> bool:
        """Registry-confirmed. Deliberately narrow — UNVERIFIED is NOT valid.

        ``validation.overall.isValid`` is true for VALID, UNVERIFIED and
        INDETERMINATE alike, so it cannot answer this question.
        """
        return self.status == "VALID"

    @property
    def should_retry(self) -> bool:
        return self.status == "INDETERMINATE"


@dataclass(frozen=True)
class Entity:
    name: Optional[str] = None
    address: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None


@dataclass(frozen=True)
class TinInfo:
    label: str = ""
    name: str = ""
    formatted_tin: Optional[str] = None


@dataclass(frozen=True)
class ValidationResponse:
    reference_id: str
    country_code: str
    tin: str
    result: ValidationResult
    tin_info: TinInfo
    entity: Optional[Entity]
    request_date: Optional[str]
    validation: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ValidationResponse":
        r = d.get("result") or {}
        ti = d.get("tinInfo") or {}
        ent = d.get("entity")
        return cls(
            reference_id=d.get("referenceId", ""),
            country_code=d.get("countryCode", ""),
            tin=d.get("tin", ""),
            result=ValidationResult(
                status=r.get("status", "INDETERMINATE"),
                reason=r.get("reason"),
                message=r.get("message", ""),
                retryable=bool(r.get("retryable", False)),
            ),
            tin_info=TinInfo(
                label=ti.get("label", ""),
                name=ti.get("name", ""),
                formatted_tin=ti.get("formattedTin"),
            ),
            entity=Entity(
                name=ent.get("name"),
                address=ent.get("address"),
                type=ent.get("type"),
                status=ent.get("status"),
            )
            if ent
            else None,
            request_date=d.get("requestDate"),
            validation=d.get("validation") or {},
            metadata=d.get("metadata") or {},
            raw=d,
        )


@dataclass(frozen=True)
class TaxIdInput:
    country_iso: str
    tin: str
    #: Required where the registry matches on name (e.g. MX).
    name: Optional[str] = None

    def to_wire(self) -> dict[str, str]:
        out = {"country_iso": self.country_iso, "tin": self.tin}
        if self.name:
            out["name"] = self.name
        return out


@dataclass(frozen=True)
class BatchItem:
    request_id: str
    country_iso: str
    tin: str
    status: BatchStatus
    validation_result: Optional[ValidationResponse]
    error_code: Optional[str]
    error_message: Optional[str]
    #: Present while the item is waiting to be retried.
    next_retry_at: Optional[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BatchItem":
        vr = d.get("validation_result")
        return cls(
            request_id=d.get("request_id", ""),
            country_iso=d.get("country_iso", ""),
            tin=d.get("tin", ""),
            status=d.get("status", "pending"),
            validation_result=ValidationResponse.from_dict(vr) if vr else None,
            error_code=d.get("error_code"),
            error_message=d.get("error_message"),
            next_retry_at=d.get("next_retry_at"),
            raw=d,
        )


@dataclass(frozen=True)
class BatchResults:
    status: BatchStatus
    total_count: int
    completed_count: int
    success_count: int
    error_count: int
    canceled_count: int
    results: list[BatchItem]
    next_cursor: Optional[int]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "canceled")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BatchResults":
        return cls(
            status=d.get("status", "pending"),
            total_count=d.get("total_count", 0),
            completed_count=d.get("completed_count", 0),
            success_count=d.get("success_count", 0),
            error_count=d.get("error_count", 0),
            canceled_count=d.get("canceled_count", 0),
            results=[BatchItem.from_dict(i) for i in d.get("results") or []],
            next_cursor=d.get("next_cursor"),
            raw=d,
        )
