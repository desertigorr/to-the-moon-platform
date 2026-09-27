"""Read-only local acceptance checks and a bounded performance sample."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from urllib.request import urlopen


def get(base, path):
    started = time.perf_counter()
    with urlopen(base + path, timeout=10) as response:
        data = json.load(response)
    return data, (time.perf_counter()-started)*1000


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)
    return {'n': len(values), 'p50': statistics.median(values),
            'p95': ordered[math.ceil(len(values)*.95)-1], 'max': max(values)}


def verify(base, seconds, output):
    first, _ = get(base, '/api/health')
    timings, batches = [], {}
    deadline = time.monotonic() + seconds
    while True:
        health, elapsed = get(base, '/api/health')
        timings.append(elapsed)
        if health.get('last_inference'):
            batch = health['last_inference']
            batches[batch['at']] = batch
        if time.monotonic() >= deadline:
            break
        time.sleep(min(1, max(0, deadline-time.monotonic())))
    vehicles, _ = get(base, '/api/vehicles')
    network, _ = get(base, '/api/network')
    predictions = [v['prediction'] for v in vehicles if v.get('prediction')]
    for p in predictions:
        horizon = (datetime.fromisoformat(p['target_time_begin'].replace('Z', '+00:00'))-
                   datetime.fromisoformat(p['T'].replace('Z', '+00:00'))).total_seconds()
        if not 600 < horizon <= 900:
            raise ValueError('Invalid horizon')
        if p['explanation_method'] != 'tree_shap' or not p['factors']:
            raise ValueError('Missing individual factors')
        logit = p['risk_baseline_log_odds'] + p['risk_other_contribution'] + sum(f['contribution'] for f in p['factors'])
        if not math.isclose(1/(1+math.exp(-logit)), p['late_probability'], abs_tol=1e-8):
            raise ValueError('Explanation does not reconcile with model probability')
    same_session = first['source']['session_id'] == health['source']['session_id']
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'duration_s': seconds,
        'vehicles_observed': len(vehicles), 'scheduled_vehicles': len(network['routes']),
        'prediction_statuses': dict(Counter(v.get('prediction_status') for v in vehicles)),
        'predictions_with_factors': len(predictions), 'same_session': same_session,
        'accepted_delta': health['emulator']['accepted_measurements']-first['emulator']['accepted_measurements'] if same_session else None,
        'missed_delta': health['emulator']['missed_measurements']-first['emulator']['missed_measurements'] if same_session else None,
        'rejected_delta': health['rejected_packets']-first['rejected_packets'],
        'ml_processing_ms': distribution([b['elapsed_ms'] for b in batches.values()]),
        'batch_sizes': sorted({b['vehicles'] for b in batches.values()}),
        'health_http_ms': distribution(timings),
        'geometry_segments': dict(Counter(f['properties']['geometry_source'] for f in network['features'])),
        'risk_segments': dict(Counter(f['properties']['risk_level'] for f in network['features'])),
        'matching': dict(Counter((v.get('map_match') or {}).get('status', 'not_calculated') for v in vehicles)),
        'ml_status': health['ml_status'], 'emulator_status': health['emulator']['status'],
        'limits': 'Small local scenario. ML time is not complete NDTP-to-screen latency. No leaderboard score is measured.'}
    if report['ml_status'] != 'ready' or report['emulator_status'] != 'ready' or not predictions:
        raise ValueError('Stack is not ready or no forecasts are available yet; inspect /api/health')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://localhost:8000')
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--output', type=Path, default=Path('docs/reports/runtime.json'))
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error('seconds must be between 1 and 300')
    verify(args.base.rstrip('/'), args.seconds, args.output)
