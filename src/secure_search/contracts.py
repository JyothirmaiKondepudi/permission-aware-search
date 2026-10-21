"""Stable candidate result wrapper. Required payload keys are described in README."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
import json


@dataclass(frozen=True)
class Result:
    entity_id: str
    as_of: str
    payload: Mapping[str, Any]

    def to_wire(self) -> bytes:
        return json.dumps(
            dict(
                entity_id=self.entity_id, as_of=self.as_of, payload=dict(self.payload)
            ),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
