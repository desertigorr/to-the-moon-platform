"""HTTP/WebSocket API around the existing NDTP receiver."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ndtp.receiver import Receiver, ReceiverOptions
from .state import FleetState, History, Vehicle
from .pipeline import Pipeline
from .emulator import EmulatorController
from .scenario import ScenarioController


@dataclass
class Settings:
    ndtp_host: str = "0.0.0.0"
    ndtp_port: int = 9201
    mapping: Path | None = Path("examples/unit-map.json")
    output_dir: Path = Path("artifacts/ndtp")
    history_size: int = 10000
    history_seconds: int = 900
    stale_after_s: float = 30
    data_mode: str = "live"
    schedule_path: str = ""
    geometry_path: str = "assets/geometry.json"
    ml_url: str = ""
    ml_interval_s: float = 5
    ml_timeout_s: float = 15
    emulator_url: str = ""
    emulator_interval_ms: int = 5000
    emulator_target_host: str = "backend"
    scenario_dataset_dir: str = "dataset/validate"
    scenario_start: str = "2026-01-06T12:30:00Z"
    scenario_duration_s: int = 1800
    scenario_tick_s: float = 1
    cors_origins: list[str] = field(default_factory=lambda: [
        "http://localhost:3000", "http://localhost:5173",
        "http://127.0.0.1:3000", "http://127.0.0.1:5173",
        "http://localhost:5174", "http://127.0.0.1:5174",
        "http://localhost:8080", "http://127.0.0.1:8080",
    ])

    @classmethod
    def from_env(cls):
        defaults = cls()
        mapping = os.getenv("NDTP_MAPPING", str(defaults.mapping))
        origins = os.getenv("CORS_ORIGINS")
        return cls(
            ndtp_host=os.getenv("NDTP_HOST", defaults.ndtp_host),
            ndtp_port=int(os.getenv("NDTP_PORT", defaults.ndtp_port)),
            mapping=Path(mapping) if mapping else None,
            output_dir=Path(os.getenv("NDTP_OUTPUT_DIR", str(defaults.output_dir))),
            history_size=int(os.getenv("HISTORY_SIZE", defaults.history_size)),
            history_seconds=int(os.getenv("HISTORY_SECONDS", defaults.history_seconds)),
            stale_after_s=float(os.getenv("STALE_AFTER_S", defaults.stale_after_s)),
            data_mode=os.getenv("DATA_MODE", defaults.data_mode),
            schedule_path=os.getenv("SCHEDULE_PATH", defaults.schedule_path),
            geometry_path=os.getenv("GEOMETRY_PATH", defaults.geometry_path),
            ml_url=os.getenv("ML_URL", defaults.ml_url).rstrip("/"),
            ml_interval_s=float(os.getenv("ML_INTERVAL_S", defaults.ml_interval_s)),
            ml_timeout_s=float(os.getenv("ML_TIMEOUT_S", defaults.ml_timeout_s)),
            emulator_url=os.getenv("EMULATOR_URL", defaults.emulator_url),
            emulator_interval_ms=int(os.getenv("EMULATOR_INTERVAL_MS", defaults.emulator_interval_ms)),
            emulator_target_host=os.getenv("EMULATOR_TARGET_HOST", defaults.emulator_target_host),
            scenario_dataset_dir=os.getenv("SCENARIO_DATASET_DIR", defaults.scenario_dataset_dir),
            scenario_start=os.getenv("SCENARIO_START", defaults.scenario_start),
            scenario_duration_s=int(os.getenv("SCENARIO_DURATION_S", defaults.scenario_duration_s)),
            scenario_tick_s=float(os.getenv("SCENARIO_TICK_S", defaults.scenario_tick_s)),
            cors_origins=[item.strip() for item in origins.split(",") if item.strip()]
                         if origins is not None else defaults.cors_origins,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if not 0 <= settings.ndtp_port <= 65535:
        raise ValueError("Invalid NDTP port")
    if settings.data_mode not in {"live", "emulator", "emulator_replay"} or min(settings.ml_interval_s, settings.ml_timeout_s, settings.history_seconds, settings.emulator_interval_ms) <= 0:
        raise ValueError("Invalid data mode or timing configuration")
    if settings.scenario_duration_s < 60 or not 1 <= settings.scenario_tick_s <= 5:
        raise ValueError("Scenario duration must be >=60s and tick between 1 and 5 seconds")
    if settings.data_mode in {"emulator", "emulator_replay"} and not settings.emulator_url:
        raise ValueError("Emulator mode requires EMULATOR_URL")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        fleet = FleetState(settings.history_size, settings.stale_after_s, history_seconds=settings.history_seconds)
        pipeline = Pipeline(fleet, settings)
        receiver = Receiver(ReceiverOptions(
            host=settings.ndtp_host, port=settings.ndtp_port,
            mapping=settings.mapping, output_dir=settings.output_dir,
        ), on_event=pipeline.ingest)
        app.state.fleet = fleet
        app.state.receiver = receiver
        app.state.pipeline = pipeline
        if pipeline.mapping:
            receiver.mapping = pipeline.mapping
        emulator = EmulatorController(settings.emulator_url, receiver.mapping, settings.emulator_interval_ms,
                                      settings.emulator_target_host, settings.ndtp_port) if settings.data_mode == "emulator" else None
        if settings.data_mode == "emulator_replay":
            emulator = ScenarioController(pipeline, settings)
            pipeline.input_guard = emulator.accept
        app.state.emulator = emulator
        await receiver.start()
        tasks = [asyncio.create_task(pipeline.run_ml()), asyncio.create_task(pipeline.record())]
        if emulator:
            tasks.append(asyncio.create_task(emulator.run()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await receiver.close()

    app = FastAPI(title="Transport telemetry API", version="1.0.0", lifespan=lifespan,
                  description="NDTP, GPS arrival estimates, remote CatBoost inference and dispatcher stream.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["GET"], allow_headers=["*"])
    code_docs = Path(__file__).resolve().parents[1] / 'docs/code'
    if code_docs.is_dir():
        app.mount('/documentation', StaticFiles(directory=code_docs, html=True), name='documentation')

    @app.get("/api/health", tags=["system"])
    async def health():
        receiver = app.state.receiver
        return {"status": "ok", "ndtp_listening": receiver.server is not None,
                "connected_devices": len(receiver.clients),
                "received_packets": receiver.counts["telemetry"],
                "rejected_packets": receiver.counts["rejected"],
                "vehicles": len(app.state.fleet.latest), "history_size": settings.history_size,
                "ml_status": app.state.pipeline.ml_status,
                "ml_error": app.state.pipeline.ml_error,
                "last_inference": app.state.pipeline.last_inference,
                "stale_after_s": settings.stale_after_s,
                "ml_interval_s": settings.ml_interval_s,
                "schedule": app.state.pipeline.schedule_status(),
                "emulator": app.state.emulator.snapshot() if app.state.emulator else None,
                "source": app.state.pipeline.replay_config()}

    @app.get("/api/network", tags=["schedule"])
    async def network():
        app.state.pipeline.ensure_session()
        return app.state.pipeline.network()

    @app.get("/api/alerts", tags=["predictions"])
    async def alerts():
        return list(app.state.pipeline.alerts)

    @app.get("/api/replay", tags=["system"])
    async def replay():
        return app.state.pipeline.replay_config()

    @app.get("/api/recording", tags=["telemetry"])
    async def recording():
        return app.state.pipeline.export_recording()

    @app.get("/api/vehicles", response_model=list[Vehicle], tags=["telemetry"])
    async def vehicles():
        return app.state.fleet.vehicles()

    @app.get("/api/vehicles/{unit_id}", response_model=Vehicle, tags=["telemetry"])
    async def vehicle(unit_id: int):
        try:
            return app.state.fleet.vehicle(unit_id)
        except KeyError:
            raise HTTPException(404, "Unknown unit_id") from None

    @app.get("/api/vehicles/{unit_id}/history", response_model=History, tags=["history"])
    async def history(unit_id: int, limit: int = Query(default=settings.history_size, ge=1,
                                                     le=settings.history_size),
                      until: datetime | None = Query(default=None,
                          description="Forecast time T, including zone: 2026-09-26T12:30:00Z")):
        if until is not None and until.tzinfo is None:
            raise HTTPException(422, "until must include a timezone (Z or an explicit offset)")
        try:
            return app.state.fleet.history(unit_id, limit, until)
        except KeyError:
            raise HTTPException(404, "Unknown unit_id") from None

    @app.websocket("/api/stream")
    async def stream(websocket: WebSocket):
        
        origin = websocket.headers.get("origin")
        if origin and origin not in settings.cors_origins and "*" not in settings.cors_origins:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        fleet = app.state.fleet
        queue = fleet.subscribe()

        async def send_updates():
            await websocket.send_json(fleet.snapshot())
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=10)
                except TimeoutError:
                    
                    message = fleet.snapshot()
                await asyncio.wait_for(websocket.send_json(message), timeout=5)

        async def watch_disconnect():
            while True:
                await websocket.receive_text()

        tasks = [asyncio.create_task(send_updates()), asyncio.create_task(watch_disconnect())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            fleet.unsubscribe(queue)
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, (WebSocketDisconnect, TimeoutError)):
                    raise result

    return app


app = create_app()
