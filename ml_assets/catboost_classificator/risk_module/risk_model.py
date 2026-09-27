"""Probability of a >120 s late arrival at a known target stop, 10–15 min ahead.

Compatible with transit_catboost.py v1.0.0. Training selects only on grouped
real-vehicle folds; synthetic relatives remain with their real parent.
"""
from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, precision_score, recall_score
from sklearn.model_selection import GroupKFold, GroupShuffleSplit

from transit_catboost import (VERSION as FEATURE_VERSION, FeatureBuilder, FeatureConfig,
                              POINT_COLS, Predictor as RegressionPredictor,
                              find_data_root, infer_lineage, read_plan, read_points,
                              read_traffic, write_json)

RISK_VERSION = "1.0.0"


@dataclass
class RiskConfig:
    delay_threshold_s: float = 120.0
    seed: int = 42
    depths: tuple[int, ...] = (4, 6)
    synthetic_weights: tuple[float, ...] = (0.0, 0.2, 1.0)
    folds: int = 3
    holdout_fraction: float = 0.25
    max_iterations: int = 800
    early_stopping_rounds: int = 80
    learning_rate: float = 0.05
    l2_leaf_reg: float = 10.0
    task_type: str = "CPU"
    thread_count: int = 4
    min_alert_precision: float = 0.5
    features: FeatureConfig = field(default_factory=FeatureConfig)


def _check_config(cfg):
    if cfg.delay_threshold_s <= 0 or cfg.folds < 2:
        raise ValueError("Need positive delay threshold and at least 2 grouped folds")
    if not cfg.depths or not cfg.synthetic_weights or any(w < 0 for w in cfg.synthetic_weights):
        raise ValueError("Expected nonempty depths and nonnegative synthetic weights")
    if not 0 < cfg.min_alert_precision <= 1 or not 0 < cfg.holdout_fraction < .5:
        raise ValueError("Expected precision in (0,1] and holdout fraction in (0,0.5)")
    if cfg.task_type not in {"CPU", "GPU"}:
        raise ValueError("task_type must be CPU or GPU")


def _truth(points, threshold):
    
    return (points.target_delay_s.to_numpy(dtype=float) > threshold).astype(int)


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1-1e-6)
    return np.log(p / (1-p))


def _calibrate_fit(raw, y):
    """Sigmoid fit on REAL out-of-family predictions only; no test or synthetic labels."""
    if len(np.unique(y)) != 2:
        raise ValueError("Calibration requires both classes in real development folds")
    lr = LogisticRegression(C=1.0, max_iter=2000, random_state=42)
    lr.fit(_logit(raw).reshape(-1, 1), y)
    return {"coefficient": float(lr.coef_[0, 0]), "intercept": float(lr.intercept_[0]),
            "method": "sigmoid_on_out_of_family_raw_probability_logit"}


def _calibrate(raw, params):
    z = np.clip(params["coefficient"] * _logit(raw) + params["intercept"], -50, 50)
    return 1 / (1 + np.exp(-z))


def _fit_classifier(X, points, indices, cfg, depth, synthetic_weight, iterations, val_idx=None):
    labels = _truth(points, cfg.delay_threshold_s)
    par = dict(loss_function="Logloss", eval_metric="Logloss", depth=int(depth),
               iterations=int(iterations), learning_rate=cfg.learning_rate,
               l2_leaf_reg=cfg.l2_leaf_reg, random_seed=cfg.seed, task_type=cfg.task_type,
               thread_count=cfg.thread_count, allow_writing_files=False, verbose=False)
    if cfg.task_type == "GPU":
        par.update(devices="0", border_count=64)
    w = np.where(points.iloc[indices].is_synthetic.to_numpy(), synthetic_weight, 1.0)
    extra = {}
    if val_idx is not None:
        extra = dict(eval_set=(X.iloc[val_idx], labels[val_idx]),
                     early_stopping_rounds=cfg.early_stopping_rounds, use_best_model=True)
    m = CatBoostClassifier(**par)
    m.fit(X.iloc[indices], labels[indices], sample_weight=w, **extra)
    return m


def _fit_indices(points, families, synthetic_weight):
    keep = points.family_id.isin(families)
    if synthetic_weight == 0:
        keep &= ~points.is_synthetic
    return np.flatnonzero(keep.to_numpy())


def _raw(model, X):
    return model.predict_proba(X)[:, 1].astype(float)


def _metrics(points, probabilities, threshold_s, alert_threshold):
    y = _truth(points, threshold_s)
    p = np.asarray(probabilities, dtype=float)
    eligible = points.cur_dev_s.le(threshold_s).fillna(False).to_numpy()
    out = {"n": len(y), "positive": int(y.sum()),
           "prevalence": float(y.mean()),
           "average_precision": float(average_precision_score(y, p)) if y.sum() else None,
           "brier": float(brier_score_loss(y, p)),
           "log_loss": float(log_loss(y, p, labels=[0, 1])),
           "threshold": float(alert_threshold)}
    for label, mask in [("all", np.ones(len(y), bool)), ("not_yet_late", eligible)]:
        actual, predicted = y[mask], p[mask] >= alert_threshold
        out[label] = {"n": int(mask.sum()), "positive": int(actual.sum()),
                      "ap": float(average_precision_score(actual, p[mask])) if actual.sum() else None,
                      "alerts": int(predicted.sum()), "tp": int((predicted & (actual == 1)).sum()),
                      "fp": int((predicted & (actual == 0)).sum()),
                      "fn": int((~predicted & (actual == 1)).sum()),
                      "precision": float(precision_score(actual, predicted, zero_division=0)),
                      "recall": float(recall_score(actual, predicted, zero_division=0))}
    return out


def _choose_threshold(y, p, eligible, min_precision):
    """Optimize for early recall at minimum precision; use dev OOF only."""
    ye, pe = y[eligible], np.asarray(p)[eligible]
    if ye.sum() == 0 or np.unique(pe).size < 2:
        raise ValueError("No eligible positives or varying probabilities to select alert threshold")
    thresholds = np.unique(np.quantile(pe, np.linspace(0, 1, 201)))
    candidates = []
    for threshold in thresholds:
        positive = pe >= threshold
        precision = precision_score(ye, positive, zero_division=0)
        recall = recall_score(ye, positive, zero_division=0)
        candidates.append((float(threshold), float(precision), float(recall), int(positive.sum())))
    feasible = [x for x in candidates if x[1] >= min_precision and x[3] >= 3]
    if feasible:
        selected = max(feasible, key=lambda x: (x[2], x[1], x[0]))
        rule = "max_recall_with_min_precision"
    else:
        selected = max(candidates, key=lambda x: (2*x[1]*x[2]/(x[1]+x[2]) if x[1]+x[2] else 0, x[1]))
        rule = "fallback_max_f1_no_threshold_met_min_precision"
    return {"probability": selected[0], "oof_eligible_precision": selected[1],
            "oof_eligible_recall": selected[2], "min_precision_requested": min_precision,
            "selection_rule": rule}


class RiskPredictor:
    """The probability is P(target_delay_s > threshold | info available at T).

    It is NOT the probability of first onset of a disruption; already-late
    vehicles can also have high probability. No cause inference is provided.
    """

    def __init__(self, model_dir):
        root = Path(model_dir)
        self.meta = json.loads((root/"risk_meta.json").read_text(encoding="utf-8"))
        if self.meta["risk_version"] != RISK_VERSION or self.meta["feature_version"] != FEATURE_VERSION:
            raise ValueError("Risk/features source versions do not match artifact")
        self.model = CatBoostClassifier()
        self.model.load_model(str(root/"risk_model.cbm"))
        self.feature_config = FeatureConfig(**self.meta["feature_config"])

    def predict_features(self, X):
        expected = self.meta["feature_names"]
        if set(X.columns) != set(expected):
            raise ValueError("Risk feature schema mismatch")
        return _calibrate(_raw(self.model, X[expected]), self.meta["calibration"])

    def predict(self, points, traffic, schedule_plan):
        X = FeatureBuilder(traffic, schedule_plan, self.feature_config).transform(points)
        return self.predict_features(X)

    def predict_table(self, points, traffic, schedule_plan):
        """Dispatcher-facing risks; an early alert does not establish first onset."""
        X = FeatureBuilder(traffic, schedule_plan, self.feature_config).transform(points)
        p = self.predict_features(X)
        alert = p >= float(self.meta["alert_threshold"]["probability"])
        eligible = points.cur_dev_s.le(self.meta["delay_threshold_s"]).fillna(False).to_numpy()
        return pd.DataFrame({"sample_id": points.sample_id.to_numpy(),
                             "tr_id": points.tr_id.to_numpy(),
                             "T": points["T"].to_numpy(),
                             "target_stop_id": points.target_stop_id.to_numpy(),
                             "target_time_begin": points.target_time_begin.to_numpy(),
                             "late_probability": p, "risk_alert": alert,
                             "early_risk_alert": alert & eligible,
                             "telemetry_stale": X.gps_stale.to_numpy() >= 1})


class CombinedPredictor:
    """Calculate features once for existing seconds regressor and new risk model."""

    def __init__(self, risk_dir, regression_dir):
        self.risk = RiskPredictor(risk_dir)
        self.regression = RegressionPredictor(regression_dir)
        reg_cfg = FeatureConfig(**self.regression.metadata["feature_config"])
        if asdict(reg_cfg) != asdict(self.risk.feature_config):
            raise ValueError("Risk and regressor require different feature definitions")
        if set(self.regression.metadata["feature_names"]) != set(self.risk.meta["feature_names"]):
            raise ValueError("Risk and regressor feature schemas differ")

    def predict(self, points, traffic, schedule_plan):
        X = FeatureBuilder(traffic, schedule_plan, self.risk.feature_config).transform(points)
        return self.predict_features(points, X)

    def predict_features(self, points, X):
        probability = self.risk.predict_features(X)
        seconds = self.regression.predict_features(points, X)
        threshold = float(self.risk.meta["alert_threshold"]["probability"])
        s = float(self.risk.meta["delay_threshold_s"])
        eligible = points.cur_dev_s.le(s).fillna(False).to_numpy()
        return pd.DataFrame({"sample_id": points.sample_id.to_numpy(),
                             "tr_id": points.tr_id.to_numpy(),
                             "T": points["T"].to_numpy(),
                             "target_stop_id": points.target_stop_id.to_numpy(),
                             "target_time_begin": points.target_time_begin.to_numpy(),
                             "predicted_delay_s": seconds,
                             "late_probability": probability,
                             "risk_alert": probability >= threshold,
                             "early_risk_alert": (probability >= threshold) & eligible,
                             "telemetry_stale": X.gps_stale.to_numpy() >= 1})


def run(data_root, output_dir, cfg=None, regression_results_zip=None):
    cfg = cfg or RiskConfig()
    _check_config(cfg)
    if sys.version_info < (3, 12):
        print("Kaggle runtime is below Python 3.12; deploy the exported module with Python 3.12+.", flush=True)
    root, out = Path(data_root), Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"Choose a new output directory: {out}")
    out.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    write_json(out/"risk_config.json", asdict(cfg))
    train = read_points(root/"labels/labels_train.csv", labeled=True)
    test = read_points(root/"labels/labels_test.csv", labeled=True)
    validate = read_points(root/"validate/points.csv")
    lineage = infer_lineage(read_plan(root/"train/schedule.csv"))
    train = train.merge(lineage, on="tr_id", validate="many_to_one")
    if max(cfg.synthetic_weights) == 0:
        train = train.loc[~train.is_synthetic].reset_index(drop=True)
    plan_train = read_plan(root/"train/schedule.csv")  
    X = FeatureBuilder(read_traffic(root/"train/traffic.csv"), plan_train, cfg.features).transform(train, progress=True)
    Xt = FeatureBuilder(read_traffic(root/"test/traffic.csv"), read_plan(root/"test/schedule.csv"), cfg.features).transform(test)
    Xv = FeatureBuilder(read_traffic(root/"validate/traffic.csv"), read_plan(root/"validate/schedule_plan.csv"), cfg.features).transform(validate)
    if list(X.columns) != list(Xt.columns) or list(X.columns) != list(Xv.columns):
        raise ValueError("Feature train/test/validate schema mismatch")
    real_idx = np.flatnonzero(~train.is_synthetic.to_numpy())
    groups = train.iloc[real_idx].family_id
    dev_pos, hold_pos = next(GroupShuffleSplit(n_splits=1, test_size=cfg.holdout_fraction,
                                               random_state=cfg.seed).split(real_idx, groups=groups))
    dev_idx, hold_idx = real_idx[dev_pos], real_idx[hold_pos]
    dev_families = set(train.iloc[dev_idx].family_id)
    hold_families = set(train.iloc[hold_idx].family_id)
    if dev_families & hold_families:
        raise RuntimeError("Synthetic-relative leakage between development and holdout")
    write_json(out/"split.json", {"development_families": sorted(dev_families),
                                   "holdout_families": sorted(hold_families)})
    folds = list(GroupKFold(n_splits=cfg.folds).split(dev_idx, groups=train.iloc[dev_idx].family_id))

    labels = _truth(train, cfg.delay_threshold_s)
    eligible_dev = train.iloc[dev_idx].cur_dev_s.le(cfg.delay_threshold_s).fillna(False).to_numpy()
    trials = []
    for depth, synth_weight in itertools.product(cfg.depths, cfg.synthetic_weights):
        oof = np.full(len(dev_idx), np.nan)
        counts = []
        for fi, vi in folds:
            families = set(train.iloc[dev_idx[fi]].family_id)
            val_idx = dev_idx[vi]
            fit_idx = _fit_indices(train, families, synth_weight)
            if set(train.iloc[fit_idx].family_id) & set(train.iloc[val_idx].family_id):
                raise RuntimeError("Cross-validation family leakage")
            model = _fit_classifier(X, train, fit_idx, cfg, depth, synth_weight,
                                    cfg.max_iterations, val_idx)
            oof[vi] = _raw(model, X.iloc[val_idx])
            counts.append(int(model.tree_count_))
        y = labels[dev_idx]
        ap = float(average_precision_score(y, oof))
        early_ap = float(average_precision_score(y[eligible_dev], oof[eligible_dev]))
        row = {"depth": depth, "synthetic_weight": synth_weight,
               "cv_ap_all": ap, "cv_ap_not_yet_late": early_ap,
               "selection_score": (ap + early_ap)/2,
               "iterations": max(1, int(np.median(counts))), "fold_iterations": counts}
        trials.append(row)
        print(f"depth={depth} synth={synth_weight}: AP={ap:.3f}, AP_eligible={early_ap:.3f}", flush=True)
    best = max(trials, key=lambda r: r["selection_score"])
    write_json(out/"experiments.json", trials)
    pd.DataFrame([{k:v for k,v in row.items() if not isinstance(v,list)} for row in trials]).to_csv(out/"experiments.csv", index=False)

    def crossfit(real_index, selected):
        oof = np.full(len(real_index), np.nan)
        real_groups = train.iloc[real_index].family_id
        for fi, vi in GroupKFold(n_splits=cfg.folds).split(real_index, groups=real_groups):
            parents = set(train.iloc[real_index[fi]].family_id)
            fit_idx = _fit_indices(train, parents, selected["synthetic_weight"])
            model = _fit_classifier(X, train, fit_idx, cfg, selected["depth"],
                                    selected["synthetic_weight"], selected["iterations"])
            oof[vi] = _raw(model, X.iloc[real_index[vi]])
        return oof

    
    dev_oof = crossfit(dev_idx, best)
    dev_cal = _calibrate_fit(dev_oof, labels[dev_idx])
    dev_p = _calibrate(dev_oof, dev_cal)
    dev_threshold = _choose_threshold(labels[dev_idx], dev_p, eligible_dev, cfg.min_alert_precision)
    fit_dev = _fit_indices(train, dev_families, best["synthetic_weight"])
    model_dev = _fit_classifier(X, train, fit_dev, cfg, best["depth"],
                                best["synthetic_weight"], best["iterations"])
    hold_p = _calibrate(_raw(model_dev, X.iloc[hold_idx]), dev_cal)
    holdout_metrics = _metrics(train.iloc[hold_idx], hold_p, cfg.delay_threshold_s,
                               dev_threshold["probability"])
    print("Independent family holdout:", json.dumps(holdout_metrics, ensure_ascii=False), flush=True)

    
    all_oof = crossfit(real_idx, best)
    all_cal = _calibrate_fit(all_oof, labels[real_idx])
    all_eligible = train.iloc[real_idx].cur_dev_s.le(cfg.delay_threshold_s).fillna(False).to_numpy()
    final_threshold = _choose_threshold(labels[real_idx], _calibrate(all_oof, all_cal),
                                        all_eligible, cfg.min_alert_precision)
    fit_all = _fit_indices(train, set(train.family_id), best["synthetic_weight"])
    final = _fit_classifier(X, train, fit_all, cfg, best["depth"],
                            best["synthetic_weight"], best["iterations"])
    risk_dir = out/"risk_module"
    risk_dir.mkdir()
    final.save_model(str(risk_dir/"risk_model.cbm"))
    meta = {"risk_version": RISK_VERSION, "feature_version": FEATURE_VERSION,
            "feature_names": list(X.columns), "feature_config": asdict(cfg.features),
            "delay_threshold_s": cfg.delay_threshold_s,
            "target_definition": "P(actual arrival minus planned arrival > 120 s at specified stop)",
            "horizon": "planned arrival T+10 to T+15 minutes, (600,900] seconds",
            "selected": best, "calibration": all_cal, "alert_threshold": final_threshold,
            "is_probability_of_new_delay_onset": False}
    meta["target_definition"] = f"P(actual arrival minus planned arrival > {cfg.delay_threshold_s:g} s at specified stop)"
    write_json(risk_dir/"risk_meta.json", meta)
    shutil.copy2(Path(__file__), risk_dir/"risk_model.py")
    shutil.copy2(Path(__file__).with_name("transit_catboost.py"), risk_dir/"transit_catboost.py")
    pred = RiskPredictor(risk_dir)
    test_p, validate_p = pred.predict_features(Xt), pred.predict_features(Xv)
    test_metrics = _metrics(test, test_p, cfg.delay_threshold_s, final_threshold["probability"])
    for name, pts, prob, features in [("test", test, test_p, Xt),
                                      ("validate", validate, validate_p, Xv)]:
        d = pts[POINT_COLS].copy()
        if name == "test":
            d["target_delay_s"] = pts.target_delay_s.to_numpy()
            d["actual_late"] = _truth(pts, cfg.delay_threshold_s)
        d["late_probability"] = prob
        d["risk_alert"] = prob >= final_threshold["probability"]
        d["early_risk_alert"] = d["risk_alert"] & pts.cur_dev_s.le(cfg.delay_threshold_s).fillna(False).to_numpy()
        d["telemetry_stale"] = features.gps_stale.to_numpy() >= 1
        d.to_csv(out/f"risk_predictions_{name}.csv", index=False)
    report = {"best": best, "group_holdout": holdout_metrics, "official_test": test_metrics,
              "dev_alert_threshold": dev_threshold, "final_alert_threshold": final_threshold,
              "rows_train": len(train), "feature_count": X.shape[1],
              "elapsed_seconds": time.perf_counter()-start,
              "limitations": ["Probability means > threshold at a known future stop, not first disruption onset.",
                              "Calibration and alert threshold use group out-of-fold REAL points only.",
                              "Selection uses only development families; holdout is evaluated before refit.",
                              "Test labels never tune the model; test shares vehicles/day with train.",
                              "No generalization estimate beyond the single available day."]}
    write_json(out/"risk_metrics.json", report)
    
    if regression_results_zip:
        regression_dir = out/"regression_model"
        regression_dir.mkdir()
        with zipfile.ZipFile(regression_results_zip) as archive:
            for filename in ["model.cbm", "model_meta.json", "requirements.txt"]:
                src = f"ml_module/{filename}"
                with archive.open(src) as f, (regression_dir/filename).open("wb") as target:
                    shutil.copyfileobj(f, target)
        combined = CombinedPredictor(risk_dir, regression_dir)
        combined_out = combined.predict(validate, read_traffic(root/"validate/traffic.csv"),
                                        read_plan(root/"validate/schedule_plan.csv"))
        np.testing.assert_allclose(combined_out.late_probability.to_numpy(), validate_p, rtol=1e-10, atol=1e-10)
        combined_out.to_csv(out/"combined_predictions_validate.csv", index=False)
    shutil.make_archive(str(out.parent / f"{out.name}_artifacts"), "zip", out)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--regression-zip")
    parser.add_argument("--delay-threshold-s", type=float, default=120)
    args = parser.parse_args()
    root = find_data_root(args.data, Path(args.out).parent/"extracted_dataset")
    run(root, args.out, RiskConfig(delay_threshold_s=args.delay_threshold_s), args.regression_zip)
