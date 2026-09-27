"""CatBoost baseline for the supplied Moscow transport dataset. Python 3.12+.

No schedule actuals, identifiers or labels enter feature construction.
All telemetry features use event_time <= T; receive_time is optional.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import itertools
import json
import platform
import shutil
import sys
import time
import warnings
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from sklearn.model_selection import GroupKFold, GroupShuffleSplit

VERSION = "1.0.0"
PLAN_COLS = ["tt_action_item_id", "tr_id", "time_begin", "geom"]
POINT_COLS = ["sample_id", "tr_id", "T", "target_stop_id", "target_time_begin", "cur_dev_s"]
TRAFFIC_COLS = ["tr_id", "event_time", "receive_time", "location_valid", "lat", "lon", "speed", "heading"]


@dataclass
class FeatureConfig:
    windows_s: tuple[int, ...] = (60, 180, 300, 600)
    max_speed_kmh: float = 130.0
    max_hold_s: float = 30.0
    stale_after_s: float = 120.0
    stop_speed_kmh: float = 1.0
    respect_receive_time: bool = False


@dataclass
class TrainConfig:
    seed: int = 42
    task_type: str = "CPU"
    thread_count: int = 4
    max_iterations: int = 1000
    early_stopping_rounds: int = 100
    learning_rate: float = 0.05
    l2_leaf_reg: float = 10.0
    depths: tuple[int, ...] = (4, 6)
    modes: tuple[str, ...] = ("direct", "residual")
    synthetic_weights: tuple[float, ...] = (0.0,)
    cv_folds: int = 3
    holdout_fraction: float = 0.25
    features: FeatureConfig = field(default_factory=FeatureConfig)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def timestamps(values):
    """Naive timestamps use one common clock; no local timezone conversion."""
    return pd.to_datetime(values, format="mixed", errors="raise", utc=True)


def seconds(values):
    return timestamps(values).astype("int64").to_numpy(dtype=np.float64) / 1e9


def haversine(lat1, lon1, lat2, lon2):
    a, b = np.radians(lat1), np.radians(lat2)
    dl, dn = b - a, np.radians(lon2) - np.radians(lon1)
    z = np.sin(dl / 2) ** 2 + np.cos(a) * np.cos(b) * np.sin(dn / 2) ** 2
    return 6371000.0 * 2 * np.arcsin(np.sqrt(np.clip(z, 0, 1)))


def bool_values(s):
    return s.astype(str).str.strip().str.lower().isin(["true", "1", "1.0"])


def find_data_root(input_path=None, work_dir="/kaggle/working/data"):
    """Accept an extracted dataset, a ZIP, or an input directory containing either.

    Only explicitly required CSV files are extracted. The emulator is never loaded.
    Ambiguous multiple dataset copies require an explicit input_path.
    """
    base = Path(input_path or "/kaggle/input")
    roots = []
    if base.is_dir():
        if (base / "labels/labels_train.csv").is_file():
            roots = [base]
        else:
            roots = sorted({x.parent.parent for x in base.rglob("labels_train.csv")
                            if x.parent.name == "labels"})
        roots = [p for p in roots if (p / "validate/points.csv").exists()]
        if len(roots) == 1:
            return roots[0]
        if len(roots) > 1:
            raise ValueError(f"Several datasets found. Set DATA_PATH explicitly: {roots}")
        archives = sorted(base.rglob("*.zip"))
    elif base.is_file() and base.suffix.lower() == ".zip":
        archives = [base]
    else:
        raise FileNotFoundError(f"Dataset not found: {base}")
    matches = []
    for archive in archives:
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if name.endswith("labels/labels_train.csv"):
                    matches.append((archive, name[:-len("labels/labels_train.csv")]))
    if len(matches) != 1:
        raise ValueError(f"Expected one dataset ZIP, found {len(matches)}. Set DATA_PATH explicitly.")
    archive, prefix = matches[0]
    required = ["labels/labels_train.csv", "labels/labels_test.csv", "train/traffic.csv",
                "train/schedule.csv", "test/traffic.csv", "test/schedule.csv",
                "validate/traffic.csv", "validate/schedule_plan.csv", "validate/points.csv",
                "sample_submission.csv"]
    root = Path(work_dir)
    with zipfile.ZipFile(archive) as z:
        for relative in required:
            dest = root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(prefix + relative) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    return root


def read_points(path, labeled=False):
    cols = POINT_COLS + (["target_delay_s"] if labeled else [])
    d = pd.read_csv(path, usecols=cols, dtype={c: str for c in ["sample_id", "tr_id", "target_stop_id"]})
    if d["sample_id"].isna().any() or d["sample_id"].duplicated().any():
        raise ValueError(f"Missing/duplicate sample_id in {path}")
    for c in ["T", "target_time_begin"]:
        d[c] = timestamps(d[c])
    h = (d["target_time_begin"] - d["T"]).dt.total_seconds()
    if not ((h > 600) & (h <= 900)).all():
        raise ValueError("Points must target a planned arrival in (T+600s, T+900s].")
    if labeled and not np.isfinite(d["target_delay_s"]).all():
        raise ValueError("Target contains missing/nonfinite values")
    return d


def read_plan(path):
    
    d = pd.read_csv(path, usecols=PLAN_COLS, dtype={"tt_action_item_id": str, "tr_id": str})
    d["time_begin"] = timestamps(d["time_begin"])
    if d["tt_action_item_id"].duplicated().any():
        raise ValueError("Plan arrival IDs must be unique")
    return d


def read_traffic(path):
    return pd.read_csv(path, usecols=TRAFFIC_COLS, dtype={"tr_id": str})


def infer_lineage(plan):
    """Dataset-specific synthetic IDs >= 9000000; verify their parent by plan geometry.

    Future PLAN geometry is allowed. Actual arrivals and labels are never accessed.
    Unknown/ambiguous synthetic parents raise rather than silently leaking across folds.
    """
    groups = {str(k): g.sort_values("time_begin", kind="stable").reset_index(drop=True)
              for k, g in plan.groupby("tr_id", sort=False)}
    real = [k for k in groups if int(k) < 9000000]
    signatures = {}
    for k in real:
        sig = tuple(groups[k]["geom"].fillna("").str.strip())
        signatures.setdefault(sig, []).append(k)
    records = []
    for k, g in groups.items():
        synthetic = int(k) >= 9000000
        parent = k
        if synthetic:
            sig = tuple(g["geom"].fillna("").str.strip())
            candidates = []
            for r in signatures.get(sig, []):
                delta = seconds(g["time_begin"]) - seconds(groups[r]["time_begin"])
                if np.ptp(delta) < 0.01:
                    candidates.append(r)
            if len(candidates) != 1:
                raise ValueError(f"Cannot establish unique synthetic parent for {k}: {candidates}")
            parent = candidates[0]
        records.append({"tr_id": k, "family_id": parent, "is_synthetic": synthetic})
    return pd.DataFrame(records)


class FeatureBuilder:
    """Reusable offline/online feature builder. No fitting, target encoding or backfill."""

    def __init__(self, traffic: pd.DataFrame, schedule_plan: pd.DataFrame,
                 config: FeatureConfig | None = None):
        self.config = config or FeatureConfig()
        
        tr = traffic.loc[:, TRAFFIC_COLS].copy()
        tr["tr_id"] = tr["tr_id"].astype(str)
        tr["et"] = seconds(tr["event_time"])
        tr["rt"] = seconds(tr["receive_time"])
        for c in ["speed", "lat", "lon", "heading"]:
            tr[c] = pd.to_numeric(tr[c], errors="coerce")
        tr["nav_ok"] = (bool_values(tr["location_valid"]) & tr["lat"].between(-90, 90)
                        & tr["lon"].between(-180, 180))
        tr["speed_ok"] = tr["nav_ok"] & tr["speed"].between(0, self.config.max_speed_kmh)
        tr["heading"] = tr["heading"].where(tr["heading"].between(0, 360))
        
        
        tr = tr.sort_values(["tr_id", "et", "rt"], kind="stable")
        columns = ["et", "rt", "nav_ok", "speed_ok", "lat", "lon", "speed", "heading"]
        self.groups = {k: {c: g[c].to_numpy() for c in columns} for k, g in tr.groupby("tr_id")}
        p = schedule_plan.loc[:, PLAN_COLS].copy()
        p["tr_id"] = p["tr_id"].astype(str)
        p["tt_action_item_id"] = p["tt_action_item_id"].astype(str)
        if p["tt_action_item_id"].duplicated().any():
            raise ValueError("Duplicate plan arrival IDs")
        p["pt"] = seconds(p["time_begin"])
        xy = p["geom"].str.extract(r"POINT\s*\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)")
        p["stop_lon"] = pd.to_numeric(xy[0], errors="coerce")
        p["stop_lat"] = pd.to_numeric(xy[1], errors="coerce")
        self.stops = p.set_index("tt_action_item_id").to_dict("index")
        self.plan_groups = {k: np.sort(g["pt"].to_numpy()) for k, g in p.groupby("tr_id")}

    def _window(self, g, now, window):
        cfg = self.config
        start = now - window
        inside = g["et"] > start
        nav = inside & g["nav_ok"]
        good = inside & g["speed_ok"]
        speed = g["speed"][good]
        f = {"n_packets": float(inside.sum()), "n_valid": float(nav.sum()),
             "invalid_fraction": float(1 - nav.sum() / inside.sum()) if inside.any() else np.nan,
             "speed_mean": float(speed.mean()) if len(speed) else np.nan,
             "speed_median": float(np.median(speed)) if len(speed) else np.nan,
             "speed_std": float(speed.std()) if len(speed) else np.nan,
             "speed_p10": float(np.quantile(speed, .1)) if len(speed) else np.nan,
             "speed_p90": float(np.quantile(speed, .9)) if len(speed) else np.nan}
        
        
        ts = g["et"]
        end = np.minimum(np.r_[ts[1:], now], ts + cfg.max_hold_s) if len(ts) else ts
        duration = np.maximum(0, np.minimum(end, now) - np.maximum(ts, start))
        covered = duration * g["speed_ok"]
        total = covered.sum()
        safe_speed = np.where(g["speed_ok"], g["speed"], 0.0)
        f["speed_time_mean"] = float((safe_speed * covered).sum() / total) if total else np.nan
        f["coverage_fraction"] = float(total / window)
        f["stopped_fraction"] = float(covered[safe_speed <= cfg.stop_speed_kmh].sum() / total) if total else np.nan
        f["speed_distance_m"] = float((safe_speed * covered).sum() / 3.6) if total else np.nan
        idx = np.flatnonzero(nav)
        f["gps_displacement_m"] = (float(haversine(g["lat"][idx[0]], g["lon"][idx[0]],
                                                         g["lat"][idx[-1]], g["lon"][idx[-1]]))
                                     if len(idx) >= 2 else np.nan)
        return f

    def transform(self, points: pd.DataFrame, progress=False):
        p = points.loc[:, POINT_COLS].copy().reset_index(drop=True)
        p["tr_id"] = p["tr_id"].astype(str)
        p["target_stop_id"] = p["target_stop_id"].astype(str)
        p["T"] = timestamps(p["T"])
        p["target_time_begin"] = timestamps(p["target_time_begin"])
        now_s, target_s = seconds(p["T"]), seconds(p["target_time_begin"])
        rows = []
        empty = {c: np.array([], dtype=bool if c.endswith("ok") else float)
                 for c in ["et", "rt", "nav_ok", "speed_ok", "lat", "lon", "speed", "heading"]}
        for i, row in enumerate(p.to_dict("records")):
            now, target = now_s[i], target_s[i]
            horizon = target - now
            if not 600 < horizon <= 900:
                raise ValueError("Prediction horizon must be in (600, 900] seconds")
            stop = self.stops.get(row["target_stop_id"])
            if stop is None or stop["tr_id"] != row["tr_id"] or abs(stop["pt"] - target) > .01:
                raise ValueError(f"Target stop/vehicle/time mismatch for {row['sample_id']}")
            hour = row["T"].hour + row["T"].minute / 60
            cur = float(row["cur_dev_s"]) if pd.notna(row["cur_dev_s"]) else np.nan
            f = {"cur_dev_s": cur, "cur_dev_missing": float(not np.isfinite(cur)),
                 "horizon_s": horizon, "hour_sin": np.sin(2*np.pi*hour/24),
                 "hour_cos": np.cos(2*np.pi*hour/24),
                 "target_lat": stop["stop_lat"], "target_lon": stop["stop_lon"]}
            full = self.groups.get(row["tr_id"], empty)
            right = np.searchsorted(full["et"], now, side="right")
            g = {k: v[:right] for k, v in full.items()}
            if self.config.respect_receive_time:
                available = g["rt"] <= now
                g = {k: v[available] for k, v in g.items()}
            f["last_packet_age_s"] = float(now - g["et"][-1]) if len(g["et"]) else np.nan
            valid_idx = np.flatnonzero(g["nav_ok"])
            j = valid_idx[-1] if len(valid_idx) else None
            age = float(now - g["et"][j]) if j is not None else np.nan
            fresh = j is not None and age <= self.config.stale_after_s
            f.update({"gps_age_s": age, "gps_missing": float(j is None),
                      "gps_stale": float(not fresh), "last_lat": np.nan, "last_lon": np.nan,
                      "last_speed": np.nan, "distance_to_target_m": np.nan,
                      "required_straight_speed_kmh": np.nan, "heading_alignment": np.nan,
                      "approach_speed_mps": np.nan})
            if fresh:
                lat, lon = g["lat"][j], g["lon"][j]
                distance = float(haversine(lat, lon, stop["stop_lat"], stop["stop_lon"]))
                f.update({"last_lat": lat, "last_lon": lon,
                          "last_speed": g["speed"][j] if g["speed_ok"][j] else np.nan,
                          "distance_to_target_m": distance,
                          "required_straight_speed_kmh": distance / horizon * 3.6})
                dlon = np.radians(stop["stop_lon"] - lon)
                a, b = np.radians(lat), np.radians(stop["stop_lat"])
                bearing = np.arctan2(np.sin(dlon)*np.cos(b), np.cos(a)*np.sin(b)-np.sin(a)*np.cos(b)*np.cos(dlon))
                f["heading_alignment"] = np.cos(np.radians(g["heading"][j]) - bearing)
                previous = valid_idx[(g["et"][valid_idx] >= now - 300) & (g["et"][valid_idx] < g["et"][j])]
                if len(previous):
                    k = previous[0]
                    prev_dist = haversine(g["lat"][k], g["lon"][k], stop["stop_lat"], stop["stop_lon"])
                    f["approach_speed_mps"] = (prev_dist - distance) / (g["et"][j]-g["et"][k])
            
            lower = now - max(self.config.windows_s) - self.config.max_hold_s
            mask = g["et"] >= lower
            recent = {k: v[mask] for k, v in g.items()}
            for window in self.config.windows_s:
                f.update({f"{key}_{window}s": value for key, value in self._window(recent, now, window).items()})
            if 60 in self.config.windows_s and 300 in self.config.windows_s:
                f["speed_change_60_vs_300"] = f["speed_time_mean_60s"] - f["speed_time_mean_300s"]
            plan_times = self.plan_groups[row["tr_id"]]
            before = np.searchsorted(plan_times, now, side="right")
            end = np.searchsorted(plan_times, target, side="right")
            f["planned_arrivals_until_target"] = float(end - before)
            f["since_previous_planned_s"] = now-plan_times[before-1] if before else np.nan
            f["until_next_planned_s"] = plan_times[before]-now if before < len(plan_times) else np.nan
            rows.append(f)
            if progress and (i+1) % 1000 == 0:
                print(f"Features: {i+1}/{len(p)}", flush=True)
        return pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan).astype("float32")


def mae(y, pred):
    return float(np.mean(np.abs(np.asarray(y)-np.asarray(pred))))


def base_values(points):
    return points["cur_dev_s"].fillna(0).to_numpy(dtype=float)


def model_params(cfg, depth, iterations):
    if cfg.task_type not in ["CPU", "GPU"]:
        raise ValueError("task_type must be CPU or GPU")
    params = dict(loss_function="MAE", eval_metric="MAE", depth=int(depth),
                  iterations=int(iterations), learning_rate=cfg.learning_rate,
                  l2_leaf_reg=cfg.l2_leaf_reg, random_seed=cfg.seed,
                  task_type=cfg.task_type, thread_count=cfg.thread_count,
                  allow_writing_files=False, verbose=False)
    if cfg.task_type == "GPU":
        params.update(devices="0", border_count=64)
    return params


def fit_one(X, points, indices, cfg, depth, mode, synthetic_weight, iterations,
            eval_indices=None):
    model = CatBoostRegressor(**model_params(cfg, depth, iterations))
    y = points["target_delay_s"].to_numpy(dtype=float)
    base = base_values(points)
    target = y-base if mode == "residual" else y
    weights = np.where(points.iloc[indices]["is_synthetic"], synthetic_weight, 1.0)
    extra = {}
    if eval_indices is not None:
        extra.update(eval_set=(X.iloc[eval_indices], target[eval_indices]),
                     early_stopping_rounds=cfg.early_stopping_rounds, use_best_model=True)
    model.fit(X.iloc[indices], target[indices], sample_weight=weights, **extra)
    return model


def predict_values(model, X, points, mode):
    pred = np.asarray(model.predict(X), dtype=float)
    return pred + base_values(points) if mode == "residual" else pred


def training_indices(points, families, synthetic_weight):
    m = points["family_id"].isin(families)
    if synthetic_weight == 0:
        m &= ~points["is_synthetic"]
    return np.flatnonzero(m.to_numpy())


def baseline_metrics(points, pred=None):
    y = points["target_delay_s"]
    result = {"n": len(points), "zero_mae_s": mae(y, np.zeros(len(points))),
              "cur_dev_mae_s": mae(y, base_values(points))}
    if pred is not None:
        result["model_mae_s"] = mae(y, pred)
    return result


def evaluate_slices(points, X, pred):
    d = points[["tr_id", "target_delay_s"]].copy().reset_index(drop=True)
    d["error"] = np.abs(d["target_delay_s"].to_numpy()-pred)
    d["baseline_error"] = np.abs(d["target_delay_s"].to_numpy()-base_values(points))
    masks = {"all": np.ones(len(d), bool), "late_gt120": d.target_delay_s > 120,
             "early_lt_minus60": d.target_delay_s < -60,
             "gps_fresh": X.gps_stale.to_numpy() == 0,
             "gps_stale_or_missing": X.gps_stale.to_numpy() == 1}
    masks.update({f"vehicle_{k}": d.tr_id == k for k in d.tr_id.unique()})
    return pd.DataFrame([{"slice": name, "n": int(np.sum(m)),
                          "mae_s": float(d.loc[m, "error"].mean()),
                          "baseline_mae_s": float(d.loc[m, "baseline_error"].mean())}
                         for name, m in masks.items() if np.sum(m)])


def make_submission(template_path, points, predictions, destination):
    template = pd.read_csv(template_path, sep=";", dtype={"sample_id": str})
    if list(template.columns) != ["sample_id", "prediction"]:
        raise ValueError("Unexpected submission template columns")
    ids = points["sample_id"].astype(str)
    if ids.duplicated().any() or template.sample_id.duplicated().any():
        raise ValueError("Duplicate submission IDs")
    if set(ids) != set(template.sample_id) or len(ids) != len(predictions):
        raise ValueError("Submission IDs do not match prediction points")
    prediction_map = pd.Series(np.asarray(predictions), index=ids)
    template["prediction"] = template.sample_id.map(prediction_map)
    if not np.isfinite(template.prediction).all():
        raise ValueError("Submission contains nonfinite predictions")
    template.to_csv(destination, sep=";", index=False, float_format="%.6f")
    return template


class Predictor:
    """Load a saved model and reuse exactly the training feature definitions.

    predict(points, traffic, schedule_plan) returns seconds, not probabilities.
    Supply a maintained telemetry buffer and known target planned arrivals.
    """

    def __init__(self, model_dir):
        root = Path(model_dir)
        self.metadata = json.loads((root / "model_meta.json").read_text())
        if self.metadata["feature_version"] != VERSION:
            raise ValueError("Feature code/model version mismatch")
        self.model = CatBoostRegressor()
        self.model.load_model(str(root / "model.cbm"))
        self.config = FeatureConfig(**self.metadata["feature_config"])

    def predict(self, points, traffic, schedule_plan):
        X = FeatureBuilder(traffic, schedule_plan, self.config).transform(points)
        return self.predict_features(points, X)

    def predict_features(self, points, X):
        expected = self.metadata["feature_names"]
        if set(X.columns) != set(expected):
            raise ValueError("Feature schema mismatch")
        return predict_values(self.model, X[expected], points, self.metadata["mode"])


def run_pipeline(data_root, output_dir, cfg=None):
    cfg = cfg or TrainConfig()
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12+ is required by this project")
    if not cfg.modes or not set(cfg.modes) <= {"direct", "residual"}:
        raise ValueError("modes must contain direct and/or residual")
    if not cfg.synthetic_weights or any(w < 0 for w in cfg.synthetic_weights):
        raise ValueError("synthetic_weights must be nonnegative")
    start = time.perf_counter()
    root, out = Path(data_root), Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"Output directory is not empty: {out}. Choose a new run name.")
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "config.json", asdict(cfg))
    train = read_points(root / "labels/labels_train.csv", labeled=True)
    test = read_points(root / "labels/labels_test.csv", labeled=True)
    validate = read_points(root / "validate/points.csv")
    for x, y in itertools.combinations([train, test, validate], 2):
        if set(x.sample_id) & set(y.sample_id):
            raise ValueError("Label splits have overlapping sample IDs")
    plan = read_plan(root / "train/schedule.csv")
    lineage = infer_lineage(plan)
    train = train.merge(lineage, on="tr_id", how="left", validate="many_to_one")
    if train.family_id.isna().any():
        raise ValueError("Missing lineage")
    
    if max(cfg.synthetic_weights) == 0:
        train = train.loc[~train.is_synthetic].reset_index(drop=True)
    lineage.to_csv(out / "lineage.csv", index=False)
    print(f"Train rows: {len(train)}; real: {(~train.is_synthetic).sum()}; test: {len(test)}; submission: {len(validate)}", flush=True)
    traffic = read_traffic(root / "train/traffic.csv")
    X = FeatureBuilder(traffic, plan, cfg.features).transform(train, progress=True)
    Xt = FeatureBuilder(read_traffic(root / "test/traffic.csv"), read_plan(root / "test/schedule.csv"), cfg.features).transform(test)
    Xv = FeatureBuilder(read_traffic(root / "validate/traffic.csv"), read_plan(root / "validate/schedule_plan.csv"), cfg.features).transform(validate)
    if list(X.columns) != list(Xt.columns) or list(X.columns) != list(Xv.columns):
        raise ValueError("Train/test feature mismatch")
    for name, frame, pts in [("train", X, train), ("test", Xt, test), ("validate", Xv, validate)]:
        frame.assign(sample_id=pts.sample_id.to_numpy()).to_csv(out / f"features_{name}.csv", index=False)
    real_idx = np.flatnonzero(~train.is_synthetic.to_numpy())
    real = train.iloc[real_idx]
    splitter = GroupShuffleSplit(n_splits=1, test_size=cfg.holdout_fraction, random_state=cfg.seed)
    dev_pos, hold_pos = next(splitter.split(real, groups=real.family_id))
    dev_idx, hold_idx = real_idx[dev_pos], real_idx[hold_pos]
    dev_families = set(train.iloc[dev_idx].family_id)
    hold_families = set(train.iloc[hold_idx].family_id)
    assert not dev_families & hold_families
    write_json(out / "split.json", {"development_families": sorted(dev_families),
                                    "holdout_families": sorted(hold_families), "seed": cfg.seed})
    folds = list(GroupKFold(n_splits=cfg.cv_folds).split(dev_idx, groups=train.iloc[dev_idx].family_id))
    experiments = []
    for mode, depth, synth_weight in itertools.product(cfg.modes, cfg.depths, cfg.synthetic_weights):
        oof = np.full(len(dev_idx), np.nan)
        counts, fold_maes = [], []
        for fold_no, (fit_pos, val_pos) in enumerate(folds):
            fit_families = set(train.iloc[dev_idx[fit_pos]].family_id)
            val_idx = dev_idx[val_pos]
            fit_idx = training_indices(train, fit_families, synth_weight)
            assert not set(train.iloc[fit_idx].family_id) & set(train.iloc[val_idx].family_id)
            model = fit_one(X, train, fit_idx, cfg, depth, mode, synth_weight,
                            cfg.max_iterations, val_idx)
            pred = predict_values(model, X.iloc[val_idx], train.iloc[val_idx], mode)
            oof[val_pos] = pred
            counts.append(int(model.tree_count_))
            fold_maes.append(mae(train.iloc[val_idx].target_delay_s, pred))
        record = {"mode": mode, "depth": int(depth), "synthetic_weight": float(synth_weight),
                  "cv_mae_s": mae(train.iloc[dev_idx].target_delay_s, oof),
                  "iterations": max(1, int(np.median(counts))),
                  "fold_mae_s": fold_maes, "fold_iterations": counts}
        experiments.append(record)
        print(f"CV {mode}, depth={depth}, synth_weight={synth_weight}: MAE={record['cv_mae_s']:.3f}s", flush=True)
    best = min(experiments, key=lambda r: r["cv_mae_s"])
    write_json(out / "experiments.json", experiments)
    pd.DataFrame([{k:v for k,v in r.items() if not isinstance(v,list)} for r in experiments]).to_csv(out / "experiments.csv", index=False)
    
    
    dev_train_idx = training_indices(train, dev_families, best["synthetic_weight"])
    hold_model = fit_one(X, train, dev_train_idx, cfg, best["depth"], best["mode"],
                         best["synthetic_weight"], best["iterations"])
    hold_pred = predict_values(hold_model, X.iloc[hold_idx], train.iloc[hold_idx], best["mode"])
    hold_metrics = baseline_metrics(train.iloc[hold_idx], hold_pred)
    train.iloc[hold_idx][POINT_COLS + ["target_delay_s"]].assign(prediction=hold_pred).to_csv(out / "holdout_predictions.csv", index=False)
    
    
    all_idx = training_indices(train, set(train.family_id), best["synthetic_weight"])
    final = fit_one(X, train, all_idx, cfg, best["depth"], best["mode"], best["synthetic_weight"], best["iterations"])
    model_dir = out / "ml_module"
    model_dir.mkdir()
    final.save_model(str(model_dir / "model.cbm"))
    metadata = {"feature_version": VERSION, "mode": best["mode"], "feature_names": list(X.columns),
                "feature_config": asdict(cfg.features), "selected": best,
                "target": "actual arrival minus planned arrival, seconds", "horizon": "(600,900] seconds to planned arrival",
                "training_labels": "labels_train.csv only", "risk_probability": "not provided by this regression model"}
    write_json(model_dir / "model_meta.json", metadata)
    source = Path(__file__)
    shutil.copy2(source, model_dir / "transit_catboost.py")
    versions = {n: importlib.metadata.version(n) for n in ["catboost", "numpy", "pandas", "scikit-learn"]}
    (model_dir / "requirements.txt").write_text("\n".join(f"{k}=={v}" for k,v in versions.items())+"\n")
    write_json(out / "environment.json", {"python": platform.python_version(), "packages": versions})
    test_pred = predict_values(final, Xt, test, best["mode"])
    val_pred = predict_values(final, Xv, validate, best["mode"])
    
    loaded_pred = Predictor(model_dir).predict_features(validate, Xv)
    np.testing.assert_allclose(val_pred, loaded_pred, atol=1e-8, rtol=1e-8)
    make_submission(root / "sample_submission.csv", validate, val_pred, out / "submission.csv")
    test[POINT_COLS + ["target_delay_s"]].assign(prediction=test_pred).to_csv(out / "test_predictions.csv", index=False)
    evaluate_slices(test, Xt, test_pred).to_csv(out / "test_slices.csv", index=False)
    pd.DataFrame({"feature": X.columns, "importance": final.feature_importances_}).sort_values("importance", ascending=False).to_csv(out / "feature_importance.csv", index=False)
    report = {"selected": best, "group_holdout": hold_metrics,
              "official_test": baseline_metrics(test, test_pred), "feature_count": X.shape[1],
              "elapsed_seconds": time.perf_counter()-start,
              "limitations": ["Only one observed day; no cross-day generalization estimate.",
                              "Official test shares vehicles/day with train; synthetic relatives may occur if enabled.",
                              "CV metrics include early-stopping/model selection and are not an unbiased final test.",
                              "Group holdout score belongs to the development model, before final refitting.",
                              "No validate targets or schedule actuals are used."]}
    write_json(out / "metrics.json", report)
    write_json(out / "input_hashes.json", {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                                           for p in root.rglob("*.csv")})
    shutil.make_archive(str(out / "ml_module"), "zip", model_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"Submission: {out / 'submission.csv'}\nModel package: {out / 'ml_module.zip'}", flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--task-type", choices=["CPU", "GPU"], default="CPU")
    parser.add_argument("--synthetic-weight", type=float, default=0.0)
    args = parser.parse_args()
    config = TrainConfig(max_iterations=args.iterations, task_type=args.task_type,
                         synthetic_weights=(args.synthetic_weight,))
    root = find_data_root(args.data, Path(args.out).parent / "extracted_dataset")
    run_pipeline(root, args.out, config)
