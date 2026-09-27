import assert from 'node:assert/strict'
import test from 'node:test'
import {
  parsePrediction,
  parseVehicle,
} from '../../src/features/telemetry/contract.ts'
import { viewVehicle } from '../../src/features/telemetry/fleet.ts'
import {
  predictionView,
  delayLabel,
} from '../../src/features/telemetry/predictions.ts'
import { vehicle, point, start, iso } from './fixtures.mjs'

const prediction = () => ({
  sample_id: 'one',
  tr_id: 120439,
  T: iso(0),
  target_stop_id: 'stop1',
  target_time_begin: iso(720),
  predicted_delay_s: 150,
  late_probability: 0.7,
  risk_alert: true,
  early_risk_alert: true,
  telemetry_stale: false,
  cur_dev_s: null,
  delay_threshold_s: 120,
  alert_threshold: 0.164,
  generated_at: iso(1),
  model_version: 'catboost-combined-1.0.0',
  explanations: [],
})
const view = (changes = {}, now = start) =>
  viewVehicle(
    {
      vehicle: parseVehicle(
        vehicle(point(), {
          prediction: prediction(),
          prediction_status: 'ready',
          ...changes,
        }),
      ),
      observedAt: start,
    },
    now,
    30,
  )

test('risk is based on model flag and preserves signed regression and unknown deviation', () => {
  const p = parsePrediction(prediction())
  assert.equal(p.cur_dev_s, null)
  assert.equal(predictionView(view(), start).tone, 'risk')
  assert.equal(delayLabel(-65), '−1 мин 5 с')
  assert.equal(delayLabel(null), '—')
  assert.throws(() =>
    parsePrediction({ ...prediction(), late_probability: 1.2 }),
  )
})
test('signal loss overrides risk, never moves the frozen position, recovery restores color', () => {
  const fresh = view()
  const offline = view({}, start + 31000)
  assert.equal(predictionView(offline, start + 31000).tone, 'offline')
  assert.deepEqual(offline.vehicle.position, fresh.vehicle.position)
  assert.equal(predictionView(fresh, start, false).tone, 'offline')
  assert.equal(predictionView(fresh, start, true).tone, 'risk')
})
test('invalid GPS, stale ML or missing forecast cannot appear as reassuring green', () => {
  assert.equal(
    predictionView(view({ prediction: null }), start).tone,
    'pending',
  )
  assert.equal(
    predictionView(view({ prediction_status: 'ml_unavailable' }), start).tone,
    'pending',
  )
  assert.equal(
    predictionView(
      view({ prediction: { ...prediction(), telemetry_stale: true } }),
      start,
    ).tone,
    'pending',
  )
  const invalid = point(666753, 1, {
    location_valid: false,
    gps_time: null,
    lon: null,
    lat: null,
    alt: null,
    speed: null,
    heading: null,
  })
  assert.equal(
    predictionView(view({ latest: invalid }), start + 1000).tone,
    'offline',
  )
})
