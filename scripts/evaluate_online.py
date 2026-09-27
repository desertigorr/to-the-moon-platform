"""Evaluate causal test features with supplied versus GPS-estimated current deviation."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backend.schedule import Schedule, ArrivalEstimator
from backend.state import Telemetry
from ml_service.app import RISK, REGRESSION, CombinedPredictor, FeatureBuilder
from transit_catboost import read_points, read_plan, read_traffic, timestamps


def evaluate(split='test'):
    root = Path('dataset')
    model = CombinedPredictor(RISK, REGRESSION)
    points = read_points(root/f'labels/labels_{split}.csv', labeled=True)
    plan = read_plan(root/f'{split}/schedule.csv')
    traffic = read_traffic(root/f'{split}/traffic.csv')
    schedule = Schedule.from_csv(root/f'{split}/schedule.csv')
    estimator = ArrivalEstimator(schedule)
    rows = traffic.copy()
    rows['event_time'], rows['receive_time'] = timestamps(rows.event_time), timestamps(rows.receive_time)
    rows['available'] = rows[['event_time', 'receive_time']].max(axis=1)
    rows = rows[rows.tr_id.astype(int).isin(schedule.by_vehicle)].sort_values('available')
    events = list(rows.itertuples(index=False))
    cursor, known, diagnostics = 0, {}, []
    for index in sorted(range(len(points)), key=lambda i: pd.Timestamp(points.iloc[i]['T'])):
        T = pd.Timestamp(points.iloc[index]['T']).to_pydatetime(warn=False)
        while cursor < len(events) and events[cursor].available <= T:
            row = events[cursor]
            valid = bool(row.location_valid) and pd.notna(row.lat) and pd.notna(row.lon)
            def value(name):
                x = getattr(row, name)
                return float(x) if valid and pd.notna(x) else None
            estimator.ingest(Telemetry(packet_id=str(cursor), tr_id=int(row.tr_id), unit_id=int(row.tr_id),
                event_time=row.event_time.to_pydatetime(warn=False), receive_time=row.receive_time.to_pydatetime(warn=False),
                location_valid=valid, device_event_id=0, gps_time=row.event_time.to_pydatetime(warn=False) if valid else None,
                lon=value('lon'), lat=value('lat'), speed=value('speed'), heading=value('heading'), alt=None, is_hist_data=False))
            cursor += 1
        arrival = estimator.latest(int(points.iloc[index].tr_id), T)
        known[index] = arrival.deviation if arrival else np.nan
        diagnostics.append({'sample_id': points.iloc[index].sample_id, 'tr_id': int(points.iloc[index].tr_id),
            'T': T.isoformat(), 'provided': float(points.iloc[index].cur_dev_s), 'estimated': known[index],
            'arrival_id': arrival.stop_id if arrival else None,
            'arrival_age_s': (T-arrival.event_time).total_seconds() if arrival else None,
            'arrival_time': arrival.event_time.isoformat() if arrival else None})
    estimated = points.copy()
    estimated['cur_dev_s'] = [known[i] for i in range(len(points))]
    cfg = replace(model.risk.feature_config, respect_receive_time=True)
    builder = FeatureBuilder(traffic, plan, cfg)
    original = model.predict_features(points, builder.transform(points))
    live_like = model.predict_features(estimated, builder.transform(estimated))
    truth = points.target_delay_s.to_numpy()
    late = truth > model.risk.meta['delay_threshold_s']
    def metrics(output):
        flags = output.risk_alert.to_numpy()
        tp, fp, fn = int((flags & late).sum()), int((flags & ~late).sum()), int((~flags & late).sum())
        return {'mae_s': float(np.mean(np.abs(output.predicted_delay_s-truth))), 'tp': tp, 'fp': fp, 'fn': fn,
                'precision': tp/(tp+fp) if tp+fp else None, 'recall': tp/(tp+fn) if tp+fn else None}
    matched = np.isfinite(estimated.cur_dev_s) & np.isfinite(points.cur_dev_s)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'split': split, 'rows': len(points),
        'provided_cur_dev_with_receive_cutoff': metrics(original), 'gps_cur_dev_with_receive_cutoff': metrics(live_like),
        'gps_cur_dev_known': int(np.isfinite(estimated.cur_dev_s).sum()),
        'cur_dev_difference_mae_s': float(np.mean(np.abs(estimated.loc[matched, 'cur_dev_s']-points.loc[matched, 'cur_dev_s']))) if matched.any() else None,
        'limitations': f'Offline replay of {split} with causal event/receive cutoff, original timestamps and all received packets. '
                      'This is not shifted official-emulator latency or leaderboard evaluation. '
                      'Arrival safeguards were investigated with train diagnostics after an initial test exposed the integration gap; '
                      'this is a diagnostic comparison, not a new independent holdout.'}
    path = Path(f'docs/reports/online-evaluation-{split}.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    diagnostic_path = Path(f'artifacts/arrival-diagnostics-{split}.csv')
    diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(diagnostics).to_csv(diagnostic_path, index=False)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--split', choices=['train', 'test'], default='test')
    evaluate(parser.parse_args().split)
