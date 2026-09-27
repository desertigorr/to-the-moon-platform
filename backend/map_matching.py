"""Contract for the team's GPS-to-graph matching module; no matching algorithm here."""

from typing import Literal, Protocol, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Coordinates(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class MatchResult(BaseModel):
    """What the matcher returns, without backend-managed source identifiers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["matched", "ambiguous", "unmatched"]
    snapped_position: Coordinates | None = None
    segment_id: str | None = Field(default=None, min_length=1)
    route_id: str | None = Field(default=None, min_length=1)
    direction: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if self.status == "matched":
            if self.snapped_position is None or self.segment_id is None:
                raise ValueError("matched requires snapped_position and segment_id")
            if self.direction is not None and self.route_id is None:
                raise ValueError("direction requires confirmed route_id")
        elif any(value is not None for value in (
                self.snapped_position, self.segment_id, self.route_id, self.direction)):
            raise ValueError("ambiguous/unmatched must not claim a selected point or segment")
        return self


class MapMatch(MatchResult):
    """Enrichment attached to Vehicle.position, never a replacement for raw GPS."""

    source_packet_id: str
    source_event_time: AwareDatetime
    graph_version: str = Field(min_length=1)


class MapMatcher(Protocol):
    """Load geometry once in __init__; match one point using only available history."""

    graph_version: str

    def match(self, point: dict, history: list[dict]) -> dict:
        """Return MatchResult fields. Inputs are JSON-compatible Telemetry objects."""
        ...
