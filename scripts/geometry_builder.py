from __future__ import annotations

import argparse

from collections import Counter, defaultdict

from dataclasses import dataclass, asdict

import hashlib

import heapq

import json

import math

from pathlib import Path

import re

import numpy as np

import pandas as pd

EARTH = 6371000.0

ORIGIN = np.array([37.5, 55.75])

SCALE = np.array([math.cos(math.radians(ORIGIN[1])), 1.0]) * EARTH * math.pi / 180

@dataclass(frozen=True)
class Settings:
    history_fraction: float = 0.7
    stop_radius_m: float = 50
    stop_margin_m: float = 8
    max_gap_s: float = 90
    max_speed_kmh: float = 120
    max_leg_s: float = 900
    max_schedule_gap_s: float = 1200
    candidate_radius_m: float = 80
    good_distance_m: float = 30
    simplify_m: float = 5

def xy(coordinates):
    return (np.asarray(coordinates, dtype=float) - ORIGIN) * SCALE

def ll(coordinates):
    return np.asarray(coordinates, dtype=float) / SCALE + ORIGIN

def distance(a, b):
    return float(np.linalg.norm(xy(a) - xy(b)))

def stop_key(position):
    value = ','.join(f'{v:.7f}' for v in position)
    return 's-' + hashlib.sha256(value.encode()).hexdigest()[:12]

def edge_key(a, b):
    return f'{a}:{b}'

def simplify(points, tolerance):
    if len(points) <= 2:
        return points
    arr = xy(points)
    delta = arr[-1] - arr[0]
    denominator = float(delta @ delta)
    ratio = np.clip((arr - arr[0]) @ delta / denominator, 0, 1) if denominator else np.zeros(len(arr))
    distances = np.linalg.norm(arr - (arr[0] + ratio[:, None] * delta), axis=1)
    idx = int(np.argmax(distances))
    if distances[idx] <= tolerance:
        return [points[0], points[-1]]
    return simplify(points[:idx + 1], tolerance)[:-1] + simplify(points[idx:], tolerance)

def read_schedule(path):
    frame = pd.read_csv(path, dtype={'tr_id': str, 'tt_action_item_id': str},
                        usecols=['tr_id', 'tt_action_item_id', 'time_begin', 'geom', 'building_address'])
    frame['time'] = pd.to_datetime(frame.time_begin, format='mixed', errors='coerce')
    nodes = {}
    rows = []
    invalid = 0
    for row in frame.itertuples():
        match = re.fullmatch(r'POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)', str(row.geom))
        if not match or pd.isna(row.time):
            invalid += 1
            continue
        point = [round(float(match[1]), 7), round(float(match[2]), 7)]
        if not (-180 <= point[0] <= 180 and -85 <= point[1] <= 85) or point == [0, 0]:
            invalid += 1
            continue
        key = stop_key(point)
        name = str(row.building_address) if pd.notna(row.building_address) else 'Остановочная точка'
        nodes.setdefault(key, {'id': key, 'position': point, 'name': name})
        rows.append({'vehicle': row.tr_id, 'event': row.tt_action_item_id, 'time': row.time,
                     'stop': key})
    result = pd.DataFrame(rows).drop_duplicates(['vehicle', 'event', 'time', 'stop'])
    return result, nodes, invalid

def schedule_edges(events, nodes, settings):
    """Ties at different stops are barriers, never ordered by arbitrary row IDs."""
    blocks = []
    ambiguous = 0
    for timestamp, group in events.groupby('time', sort=True):
        stops = sorted(set(group.stop))
        if len(stops) != 1:
            ambiguous += len(stops)
        blocks.append((timestamp, stops))
    edges = Counter()
    rejected = 0
    for (ta, aa), (tb, bb) in zip(blocks, blocks[1:]):
        if len(aa) != 1 or len(bb) != 1:
            rejected += 1
            continue
        a, b = aa[0], bb[0]
        if a == b:
            continue
        dt = (tb - ta).total_seconds()
        length = distance(nodes[a]['position'], nodes[b]['position'])
        if not 0 < dt <= settings.max_schedule_gap_s or length / dt * 3.6 > settings.max_speed_kmh:
            rejected += 1
            continue
        edges[(a, b)] += 1
    return edges, ambiguous, rejected

def possible_schedule_pairs(events, nodes, settings):
    """A tied time permits alternatives, but does not assert their ordering."""
    blocks = [(t, sorted(set(g.stop))) for t, g in events.groupby('time', sort=True)]
    pairs = set()
    for _, stops in blocks:
        for a in stops:
            for b in stops:
                if a != b and distance(nodes[a]['position'], nodes[b]['position']) <= 2000:
                    pairs.add((a, b))
    for (ta, aa), (tb, bb) in zip(blocks, blocks[1:]):
        dt = (tb - ta).total_seconds()
        if not 0 < dt <= settings.max_schedule_gap_s:
            continue
        for a in aa:
            for b in bb:
                if a != b and distance(nodes[a]['position'], nodes[b]['position']) / dt * 3.6 <= settings.max_speed_kmh:
                    pairs.add((a, b))
    return pairs

def clean_traffic(frame, settings):
    frame = frame.copy()
    frame['time'] = pd.to_datetime(frame.event_time, format='mixed', errors='coerce')
    frame = frame[frame.time.notna()].sort_values('time', kind='stable')
    original = len(frame)
    frame['valid'] = (frame.location_valid.eq(True) & frame.lon.between(-180, 180)
                      & frame.lat.between(-85, 85) & ~((frame.lon == 0) & (frame.lat == 0)))
    frame['reason'] = np.where(frame.valid, '', 'invalid_gps')
    unreasonable = frame.valid & (frame.speed.gt(settings.max_speed_kmh) | frame.speed.lt(0))
    frame.loc[unreasonable, ['valid', 'reason']] = [False, 'invalid_speed']
    conflict_times = set()
    duplicates = frame[frame.duplicated('time', keep=False)]
    for timestamp, group in duplicates.groupby('time'):
        positions = group[group.valid][['lon', 'lat']].to_numpy()
        if len(positions) > 1 and np.linalg.norm(np.ptp(xy(positions), axis=0)) > 20:
            conflict_times.add(timestamp)
    
    frame = frame.sort_values(['time', 'valid'], ascending=[True, False], kind='stable').drop_duplicates('time')
    frame.loc[frame.time.isin(conflict_times), ['valid', 'reason']] = [False, 'conflicting_gps']
    good = frame[frame.valid]
    if len(good) >= 3:
        coords = xy(good[['lon', 'lat']].to_numpy())
        times = good.time.astype('datetime64[ns]').astype('int64').to_numpy() / 1e9
        dt = np.diff(times)
        speeds = np.linalg.norm(np.diff(coords, axis=0), axis=1) / dt * 3.6
        across = np.linalg.norm(coords[2:] - coords[:-2], axis=1) / (times[2:] - times[:-2]) * 3.6
        spikes = ((speeds[:-1] > settings.max_speed_kmh) & (speeds[1:] > settings.max_speed_kmh)
                  & (across < settings.max_speed_kmh))
        frame.loc[good.index[1:-1][spikes], ['valid', 'reason']] = [False, 'gps_jump']
    return frame.reset_index(drop=True), {'inputPoints': original, 'duplicatePoints': original - len(frame),
                                       'conflictingTimestamps': len(conflict_times),
                                       'rejectedPoints': int((~frame.valid).sum())}

def extract_observations(history, nodes, settings):
    """Stop approaches and inter-stop geometry come only from earlier GPS."""
    good = history[history.valid].reset_index(drop=True)
    observations = defaultdict(list)
    if len(good) < 3 or not nodes:
        return observations, 0
    positions = good[['lon', 'lat']].to_numpy()
    coordinates = xy(positions)
    times = good.time.astype('datetime64[ns]').astype('int64').to_numpy() / 1e9
    stop_ids = list(nodes)
    stop_xy = xy([nodes[k]['position'] for k in stop_ids])
    distances = np.linalg.norm(coordinates[:, None, :] - stop_xy[None, :, :], axis=2)
    order = np.argsort(distances, axis=1)
    nearest = order[:, 0]
    best = distances[np.arange(len(good)), nearest]
    margin = distances[np.arange(len(good)), order[:, 1]] - best if len(nodes) > 1 else np.full(len(good), 999)
    valid_visit = (best <= settings.stop_radius_m) & (margin >= settings.stop_margin_m)
    dt = np.diff(times)
    breaks = ((dt > settings.max_gap_s)
              | (np.linalg.norm(np.diff(coordinates, axis=0), axis=1) / dt * 3.6 > settings.max_speed_kmh))
    cumulative_breaks = np.r_[0, np.cumsum(breaks)]
    visits = []
    for index in np.flatnonzero(valid_visit):
        key = stop_ids[nearest[index]]
        if visits and visits[-1][0] == key and cumulative_breaks[index] == cumulative_breaks[visits[-1][1]]:
            if best[index] < best[visits[-1][1]]:
                visits[-1] = (key, int(index))
        else:
            visits.append((key, int(index)))
    rejected = 0
    for (a, start), (b, end) in zip(visits, visits[1:]):
        elapsed = times[end] - times[start]
        if a == b or end - start < 2 or not 0 < elapsed <= settings.max_leg_s or cumulative_breaks[end] != cumulative_breaks[start]:
            rejected += 1
            continue
        part = positions[start:end + 1]
        length = float(np.linalg.norm(np.diff(xy(part), axis=0), axis=1).sum())
        chord = distance(nodes[a]['position'], nodes[b]['position'])
        if length > max(chord * 3.5, chord + 500) or length < 20:
            rejected += 1
            continue
        geometry = simplify(part.tolist(), settings.simplify_m)
        observations[(a, b)].append({'geometry': [[round(v, 7) for v in p] for p in geometry],
                                     'lengthM': round(length, 1), 'durationSeconds': round(elapsed, 1),
                                     'start': good.iloc[start].time.isoformat(),
                                     'end': good.iloc[end].time.isoformat(), 'points': end - start + 1})
    return observations, rejected

def build_edges(planned, observed, nodes):
    edges = []
    for a, b in sorted(set(planned) | set(observed)):
        traces = observed.get((a, b), [])
        chosen = sorted(traces, key=lambda x: (x['lengthM'], x['start']))[len(traces) // 2] if traces else None
        edges.append({'id': edge_key(a, b), 'from': a, 'to': b,
                      'scheduleSupport': planned.get((a, b), 0), 'observedPassages': len(traces),
                      'kind': 'observed' if chosen else 'schedule',
                      'geometry': chosen['geometry'] if chosen else [nodes[a]['position'], nodes[b]['position']],
                      'lengthM': chosen['lengthM'] if chosen else round(distance(nodes[a]['position'], nodes[b]['position']), 1),
                      'evidence': chosen})
    return edges


def build(dataset, output, cutoff):
    cfg = Settings()
    cutoff = pd.Timestamp(cutoff).tz_convert('UTC').tz_localize(None)
    plan, all_nodes, _ = read_schedule(dataset / 'validate/schedule_plan.csv')
    traffic_path = dataset / 'train/traffic.csv'
    traffic = pd.read_csv(traffic_path, usecols=['tr_id', 'event_time', 'receive_time',
        'location_valid', 'lon', 'lat', 'speed', 'heading'], dtype={'tr_id': str})
    et = pd.to_datetime(traffic.event_time, format='mixed', utc=True).dt.tz_localize(None)
    rt = pd.to_datetime(traffic.receive_time, format='mixed', utc=True).dt.tz_localize(None)
    traffic = traffic[(et < cutoff) & (rt < cutoff)]
    result = []
    for tr, events in plan.groupby('vehicle'):
        nodes = {key: all_nodes[key] for key in set(events.stop)}
        clean, _ = clean_traffic(traffic[traffic.tr_id == tr], cfg)
        observed, _ = extract_observations(clean, nodes, cfg)
        possible = possible_schedule_pairs(events, nodes, cfg)
        observed = {pair: traces for pair, traces in observed.items() if pair in possible}
        for edge in build_edges({}, observed, nodes):
            a, b = nodes[edge['from']]['position'], nodes[edge['to']]['position']
            result.append({'tr_id': int(tr), 'from': a, 'to': b,
                           'coordinates': [a, *edge['geometry'], b],
                           'passages': edge['observedPassages'], 'evidence': edge['evidence']})
    payload = {'version': 1, 'source': 'train/traffic.csv',
               'history_until': cutoff.isoformat() + 'Z',
               'source_sha256': hashlib.sha256(traffic_path.read_bytes()).hexdigest(),
               'edges': result}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Historical geometry: {len(result)} directed edges, cutoff {cutoff}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build route geometry using only earlier training GPS.')
    parser.add_argument('--dataset', type=Path, default=Path('dataset'))
    parser.add_argument('--output', type=Path, default=Path('assets/geometry.json'))
    parser.add_argument('--cutoff', default='2026-01-06T12:30:00Z')
    args = parser.parse_args()
    build(args.dataset, args.output, args.cutoff)
