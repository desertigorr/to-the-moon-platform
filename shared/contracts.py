"""JSON boundary between the telemetry backend and the independently deployed ML service."""
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ForecastPoint(StrictModel):
    sample_id: str = Field(min_length=1)
    tr_id: int
    T: AwareDatetime
    cur_dev_s: float | None
    target_stop_id: str
    target_time_begin: AwareDatetime

    @model_validator(mode="after")
    def horizon(self):
        if not 600 < (self.target_time_begin - self.T).total_seconds() <= 900:
            raise ValueError("Target must be in (T+600s, T+900s]")
        return self


class TrafficPoint(StrictModel):
    tr_id: int
    event_time: AwareDatetime
    receive_time: AwareDatetime
    location_valid: bool
    lat: float | None = Field(ge=-90, le=90)
    lon: float | None = Field(ge=-180, le=180)
    speed: float | None
    heading: float | None


class PlanPoint(StrictModel):
    tt_action_item_id: str
    tr_id: int
    time_begin: AwareDatetime
    geom: str


class PredictRequest(StrictModel):
    request_id: str
    points: list[ForecastPoint] = Field(min_length=1, max_length=100)
    traffic_buffer: list[TrafficPoint] = Field(max_length=200000)
    schedule_as_of_T: list[PlanPoint] = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def coherent_batch(self):
        if len({p.T for p in self.points}) != 1:
            raise ValueError("One batch must use one T")
        if len({p.tr_id for p in self.points}) != len(self.points):
            raise ValueError("One point per vehicle")
        if len({p.sample_id for p in self.points}) != len(self.points):
            raise ValueError("Duplicate sample_id")
        plan = {p.tt_action_item_id: p for p in self.schedule_as_of_T}
        if len(plan) != len(self.schedule_as_of_T):
            raise ValueError("Duplicate planned arrival ID")
        for p in self.points:
            target = plan.get(p.target_stop_id)
            if not target or target.tr_id != p.tr_id or target.time_begin != p.target_time_begin:
                raise ValueError("Target does not match schedule")
        return self


class PredictionFactor(StrictModel):
    feature: str
    label: str
    value: float | None
    unit: str
    contribution: float
    direction: Literal["increases_risk", "decreases_risk"]
    method: Literal["tree_shap"] = "tree_shap"
    scale: Literal["calibrated_log_odds"] = "calibrated_log_odds"


class SegmentContext(StrictModel):
    segment_id: str
    graph_version: str
    from_stop_id: str
    to_stop_id: str
    from_name: str
    to_name: str
    basis: Literal["target_approach"] = "target_approach"
    geometry_source: str


class Recommendation(StrictModel):
    code: str
    title: str
    rationale: str
    priority: Literal["high", "medium", "low"]
    requires_dispatcher: bool = True


class Prediction(StrictModel):
    sample_id: str
    tr_id: int
    T: AwareDatetime
    target_stop_id: str
    target_time_begin: AwareDatetime
    predicted_delay_s: float
    late_probability: float = Field(ge=0, le=1)
    risk_alert: bool
    early_risk_alert: bool
    telemetry_stale: bool
    cur_dev_s: float | None
    delay_threshold_s: float
    alert_threshold: float = Field(ge=0, le=1)
    generated_at: AwareDatetime
    model_version: str
    explanations: list[str] = Field(default_factory=list)
    factors: list[PredictionFactor] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    explanation_method: str = "unavailable"
    risk_baseline_log_odds: float | None = None
    risk_other_contribution: float | None = None
    segment: SegmentContext | None = None
    recommendations: list[Recommendation] = Field(default_factory=list)


class PredictResponse(StrictModel):
    request_id: str
    predictions: list[Prediction]
    elapsed_ms: float
