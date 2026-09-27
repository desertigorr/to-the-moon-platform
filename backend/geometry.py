"""Fixed directed geometry reconstructed from earlier GPS, with explicit fallback."""

import hashlib
import json
from pathlib import Path


def edge_key(tr, a, b):
    return (int(tr), *(round(v, 7) for v in (*a, *b)))


class GeometryLibrary:
    def __init__(self, path='assets/geometry.json', before=None):
        self.edges = {}
        self.version = 'schematic'
        self.history_until = None
        if not path or not Path(path).is_file():
            return
        from .schedule import stamp
        raw = Path(path).read_bytes()
        data = json.loads(raw)
        if data['version'] != 1:
            raise ValueError('Unsupported geometry version')
        self.history_until = data['history_until']
        if before and stamp(self.history_until) > stamp(before):
            raise ValueError('Geometry history must precede scenario start')
        self.version = hashlib.sha256(raw).hexdigest()[:12]
        for edge in data['edges']:
            coordinates = edge['coordinates']
            if len(coordinates) < 2 or any(len(p) != 2 or not (-180 <= p[0] <= 180 and -90 <= p[1] <= 90) for p in coordinates):
                raise ValueError('Invalid historical geometry')
            self.edges[edge_key(edge['tr_id'], edge['from'], edge['to'])] = edge

    def get(self, tr, a, b):
        return self.edges.get(edge_key(tr, a, b))
