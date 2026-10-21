from typing import Annotated
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StrictBool,
    AwareDatetime,
    field_validator,
)
from datetime import datetime


class DocumentRecord(BaseModel):
    allowed_groups: list[str]
    deleted: StrictBool
    doc_id: str
    received_at: AwareDatetime
    revision: StrictInt = Field(ge=1)
    tenant_id: str
    text: str
    title: str

    model_config = ConfigDict(frozen=True)

    @field_validator("allowed_groups", mode="before")
    @classmethod
    def _allowed_groups_(cls, value):
        if not isinstance(value, list):
            raise ValueError(
                f"allowed_groups: expected a list of strings, got {type(value).__name__} {value!r}"
            )
        for i in value:
            if not isinstance(i, str):
                raise ValueError(
                    f"allowed_groups: every item must be a string, got {type(i).__name__} {i!r}"
                )
        return list(value)

    @field_validator("doc_id", "tenant_id", "title", "text")
    @classmethod
    def must_be_str(cls, value):
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"expected a string, got {type(value).__name__} {value!r}")
        return value

    @field_validator("received_at", mode="before")
    @classmethod
    def must_be_datetime(cls, value):
        if isinstance(value, datetime):

            if value.utcoffset() is None:
                raise ValueError(
                    f"received_at: timestamp must include an explicit UTC offset, got {value!r}"
                )
            return value

        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(
                f"received_at: expected a non-empty ISO 8601 timestamp string, "
                f"got {type(value).__name__} {value!r}"
            )
        try:
            iso_dateformat = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"received_at: expected an ISO 8601 timestamp, got {value!r}"
            ) from exc
        # checked outside the try so this message is not replaced by the generic one above
        if iso_dateformat.utcoffset() is None:
            raise ValueError(
                f"received_at: timestamp must include an explicit UTC offset, got {value!r}"
            )
        return iso_dateformat
