import assert from 'node:assert/strict'
import test from 'node:test'
import {
  reserveScenario,
  segmentTones,
} from '../../src/features/telemetry/decisions.ts'
import { viewVehicle } from '../../src/features/telemetry/fleet.ts'
import { parseVehicle } from '../../src/features/telemetry/contract.ts'
import { vehicle, point, start, iso } from './fixtures.mjs'

test('reserve compares explicit dispatch and travel assumptions, not causal ML effects', () => {
  const result = reserveScenario(start, iso(720), 180, 2, 8)
  assert.equal(result.reserveAt, start + 600000)
  assert.equal(result.earlierByS, 300)
  assert.equal(result.reserveDeviationS, -120)
  assert.equal(reserveScenario(start, iso(720), 180, -1, 8), null)
  assert.equal(reserveScenario(start, iso(-1), 180, 2, 8), null)
  assert.equal(reserveScenario(start, iso(720), 180, NaN, 8), null)
})

test('segment coloring expires with signal or forecast and rejects another graph version', () => {
  const network = {
    graph_version: 'g',
    features: [{ properties: { segment_id: 's', physical_id: 'physical' } }],
  }
  const v = parseVehicle(
    vehicle(point(), {
      prediction_status: 'ready',
      prediction: {
        sample_id: 'p',
        tr_id: 120439,
        T: iso(0),
        target_stop_id: 'stop',
        target_time_begin: iso(720),
        predicted_delay_s: 180,
        late_probability: 0.8,
        risk_alert: true,
        early_risk_alert: true,
        telemetry_stale: false,
        cur_dev_s: 0,
        delay_threshold_s: 120,
        alert_threshold: 0.16,
        generated_at: iso(0),
        model_version: 'test',
        explanations: [],
        segment: {
          segment_id: 's',
          graph_version: 'g',
          from_stop_id: 'a',
          to_stop_id: 'b',
          from_name: 'А',
          to_name: 'Б',
          basis: 'target_approach',
          geometry_source: 'historical_gps',
        },
      },
    }),
  )
  const view = viewVehicle({ vehicle: v, observedAt: start }, start, 30)
  assert.equal(
    segmentTones(network, [view], start, true).get('physical'),
    'risk',
  )
  assert.equal(segmentTones(network, [view], start, false).size, 0)
  assert.equal(segmentTones(network, [view], start + 31000, true).size, 0)
  assert.equal(
    segmentTones({ ...network, graph_version: 'new' }, [view], start, true)
      .size,
    0,
  )
})
