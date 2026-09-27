"""Models load once; one bounded, non-overlapping inference batch per process."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from fastapi import FastAPI, HTTPException
import pandas as pd

from shared.contracts import PredictRequest, PredictResponse, Prediction

ROOT = Path(__file__).resolve().parents[1]
RISK = ROOT / "ml_assets/catboost_classificator/risk_module"
REGRESSION = ROOT / "ml_assets/catboost/ml_module"

sys.path.insert(0, str(RISK))
from risk_model import CombinedPredictor  
from transit_catboost import TRAFFIC_COLS, PLAN_COLS, FeatureBuilder
from .explanations import explain_batch


def infer(predictor, request: PredictRequest) -> PredictResponse:
    started = time.perf_counter()
    T = request.points[0].T
    points = pd.DataFrame([p.model_dump() for p in request.points])
    
    points["cur_dev_s"] = pd.to_numeric(points["cur_dev_s"], errors="coerce").astype(float)
    traffic = pd.DataFrame([p.model_dump() for p in request.traffic_buffer
                            if p.event_time <= T and p.receive_time <= T], columns=TRAFFIC_COLS)
    schedule = pd.DataFrame([p.model_dump() for p in request.schedule_as_of_T], columns=PLAN_COLS)
    features = FeatureBuilder(traffic, schedule, predictor.risk.feature_config).transform(points)
    output = predictor.predict_features(points, features)
    explanations = explain_batch(predictor, features, output.late_probability.to_numpy())
    rows = json.loads(output.to_json(orient="records", date_format="iso", double_precision=15))
    current = {p.sample_id: p for p in request.points}
    now = datetime.now(timezone.utc)
    
    for row in rows:
        source = current[row["sample_id"]]
        row.update(T=source.T, target_time_begin=source.target_time_begin,
                   target_stop_id=source.target_stop_id, tr_id=source.tr_id)
    predictions = [Prediction(**row, cur_dev_s=current[row["sample_id"]].cur_dev_s,
                              delay_threshold_s=predictor.risk.meta["delay_threshold_s"],
                              alert_threshold=predictor.risk.meta["alert_threshold"]["probability"],
                              generated_at=now, model_version="catboost-combined-1.1.0",
                              **explanations[index]) for index, row in enumerate(rows)]
    return PredictResponse(request_id=request.request_id, predictions=predictions,
                           elapsed_ms=(time.perf_counter() - started) * 1000)


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        app.state.predictor = CombinedPredictor(RISK, REGRESSION)
        app.state.busy = False
        yield

    app = FastAPI(title="To the Moon ML", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    async def health():
        meta = app.state.predictor.risk.meta
        return {"status": "ok", "models_loaded": 2, "busy": app.state.busy,
                "feature_count": len(meta["feature_names"]), "explanations_available": True,
                "delay_threshold_s": meta["delay_threshold_s"],
                "alert_threshold": meta["alert_threshold"]["probability"]}

    @app.post("/predict", response_model=PredictResponse)
    async def predict(request: PredictRequest):
        if app.state.busy:
            raise HTTPException(429, "An inference batch is already running")
        app.state.busy = True
        job = asyncio.create_task(asyncio.to_thread(infer, app.state.predictor, request))
        
        job.add_done_callback(lambda _: setattr(app.state, "busy", False))
        try:
            return await asyncio.shield(job)
        except (ValueError, KeyError) as error:
            raise HTTPException(422, str(error)) from error

    return app


app = create_app()
