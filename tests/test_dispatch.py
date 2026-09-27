import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from backend.dispatch import network_risk
from backend.geometry import GeometryLibrary
from backend.schedule import Schedule
from backend.state import FleetState
from shared.contracts import Prediction, SegmentContext
from test_pipeline import BASE, point


class DispatchTests(unittest.TestCase):
    def schedule(self, geometry=None):
        return Schedule([{'tt_action_item_id': str(i), 'tr_id': 1,
            'time_begin': (BASE+timedelta(seconds=t)).isoformat(), 'geom': f'POINT ({lon} 55.7)',
            'building_address': name} for i, t, lon, name in [(1, 0, 37.6, 'А'), (2, 720, 37.61, 'Б')]], geometry=geometry)

    def prediction(self, schedule):
        return Prediction(sample_id='p', tr_id=1, T=BASE, target_stop_id='2', target_time_begin=BASE+timedelta(seconds=720),
            predicted_delay_s=180, late_probability=.8, risk_alert=True, early_risk_alert=True,
            telemetry_stale=False, cur_dev_s=0, delay_threshold_s=120, alert_threshold=.16,
            generated_at=BASE, model_version='test', segment=SegmentContext(**schedule.target_segment('2')))

    def test_risk_is_target_approach_and_expired_signal_clears_color(self):
        schedule = self.schedule()
        fleet = FleetState()
        fleet.ingest(point().model_dump(mode='json'))
        fleet.predictions[10] = self.prediction(schedule)
        fresh = fleet.vehicle(10, BASE)
        net = network_risk(schedule.network(), [fresh], BASE)
        self.assertEqual(net['features'][0]['properties']['risk_level'], 'high')
        self.assertEqual(net['features'][0]['properties']['risk_basis'], 'target_approach')
        self.assertIn('verify_delay', {r.code for r in fresh.recommendations})
        later = BASE+timedelta(seconds=40)
        stale = fleet.vehicle(10, later)
        self.assertEqual(stale.recommendations[0].code, 'restore_signal')
        self.assertEqual(network_risk(schedule.network(), [stale], later)['features'][0]['properties']['risk_level'], 'unknown')
        fleet.predictions[10].segment.graph_version = 'old'
        self.assertEqual(network_risk(schedule.network(), [fleet.vehicle(10, BASE)], BASE)['features'][0]['properties']['risk_level'], 'unknown')

    def test_curved_history_is_used_for_matching_and_raw_point_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'geometry.json'
            path.write_text(json.dumps({'version': 1, 'history_until': (BASE-timedelta(seconds=1)).isoformat(), 'edges': [
                {'tr_id': 1, 'from': [37.6, 55.7], 'to': [37.61, 55.7],
                 'coordinates': [[37.6, 55.7], [37.605, 55.702], [37.61, 55.7]]}]}))
            geometry = GeometryLibrary(path, BASE.isoformat())
            schedule = self.schedule(geometry)
            p = point(360, lon=37.605, lat=55.702, speed=0)
            result = schedule.match(p)
            self.assertEqual(result['status'], 'matched')
            self.assertEqual(result['route_id'], 'schedule-1')
            self.assertEqual(result['direction'], 'А → Б')
            self.assertEqual(schedule.network()['features'][0]['properties']['geometry_source'], 'historical_gps')
            self.assertEqual(p.lat, 55.702)
            with self.assertRaises(ValueError):
                GeometryLibrary(path, (BASE-timedelta(seconds=60)).isoformat())

    def test_heading_rejects_wrong_direction_and_missing_target_has_no_segment(self):
        schedule = self.schedule()
        east = point(100, lon=37.602, speed=30)
        self.assertEqual(schedule.match(east)['status'], 'matched')
        west = east.model_copy(update={'heading': 270, 'event_time': east.event_time+timedelta(seconds=15)})
        self.assertEqual(schedule.match(west)['status'], 'unmatched')
        self.assertIsNone(schedule.target_segment('missing'))


if __name__ == '__main__':
    unittest.main()
