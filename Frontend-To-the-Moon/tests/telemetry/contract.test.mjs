import assert from 'node:assert/strict'
import test from 'node:test'
import {
  parseVehicle,
  parseStreamMessage,
  parseHistory,
} from '../../src/features/telemetry/contract.ts'
import {
  applyMessage,
  emptyFleet,
  newConnection,
  viewVehicle,
} from '../../src/features/telemetry/fleet.ts'
import {
  parseRecording,
  replayAt,
} from '../../src/features/telemetry/recording.ts'
import {
  point,
  vehicle,
  snapshot,
  telemetry,
  iso,
  start,
  exampleRecording,
} from './fixtures.mjs'

test('wire validation preserves zero, null and large string packet IDs', () => {
  const v = parseVehicle(
    vehicle(point(1, 0, { lon: 0, lat: 0, heading: 0, speed: 0 })),
  )
  assert.equal(v.latest.lon, 0)
  assert.equal(v.latest.speed, 0)
  assert.equal(v.tr_id, null)
  assert.equal(typeof v.latest.packet_id, 'string')
  assert.throws(
    () =>
      parseVehicle(
        vehicle(point(1, 0, { packet_id: Number('1790425560952971501') })),
      ),
    /packet_id/,
  )
  assert.throws(
    () =>
      parseVehicle(vehicle(point(1, 0, { event_time: '2026-09-26T12:00:00' }))),
    /часовым поясом/,
  )
  assert.throws(
    () => parseVehicle(vehicle(point(1, 0, { lon: null }))),
    /координаты/,
  )
})
test('missing GPS remains null and cannot become a marker at zero', () => {
  const p = point(1, 0, {
    location_valid: false,
    gps_time: null,
    lon: null,
    lat: null,
    alt: null,
    speed: null,
    heading: null,
  })
  const v = parseVehicle(vehicle(p, { position: null, position_age_s: null }))
  const view = viewVehicle({ vehicle: v, observedAt: start }, start, 30)
  assert.equal(view.status, 'no-gps')
  assert.equal(view.vehicle.position, null)
})
test('snapshot replaces fleet; telemetry changes one unit and uses vehicle.position instead of late data', () => {
  let state = applyMessage(
    emptyFleet(),
    snapshot([vehicle(), vehicle(point(2))]),
    start,
  )
  const newer = vehicle(point(666753, 5, { speed: 12 }), { history_count: 2 })
  state = applyMessage(state, telemetry(newer, 1, point()), start + 6000)
  assert.equal(state.vehicles.size, 2)
  assert.equal(state.vehicles.get(666753).vehicle.position.event_time, iso(5))
  const duplicate = applyMessage(state, telemetry(vehicle(), 1), start + 7000)
  assert.equal(duplicate, state)
  state = applyMessage(state, snapshot([], 1), start + 8000)
  assert.equal(state.vehicles.size, 0)
})
test('equal-sequence snapshot refreshes ages, lower sequence is only accepted after reconnect', () => {
  const state = applyMessage(emptyFleet(), snapshot([vehicle()], 42), start)
  const refreshed = applyMessage(
    state,
    snapshot([vehicle(undefined, { is_stale: true })], 42),
    start + 50000,
  )
  assert.equal(refreshed.vehicles.get(666753).vehicle.is_stale, true)
  assert.equal(
    applyMessage(refreshed, snapshot([], 0), start + 51000),
    refreshed,
  )
  const pending = newConnection(refreshed)
  assert.equal(
    applyMessage(pending, telemetry(vehicle(), 1), start + 52000),
    pending,
  )
  assert.equal(
    applyMessage(pending, snapshot([], 0), start + 53000).vehicles.size,
    0,
  )
})
test('fresh packets without GPS do not reset coordinate age; stale threshold is configurable', () => {
  const p = point(666753, 25, {
    location_valid: false,
    gps_time: null,
    lon: null,
    lat: null,
    alt: null,
    speed: null,
    heading: null,
  })
  const entry = {
    vehicle: vehicle(p, { position: point(), position_age_s: 25 }),
    observedAt: start + 25000,
  }
  const view = viewVehicle(entry, start + 31000, 30)
  assert.equal(view.stale, false)
  assert.equal(view.positionAge, 31)
  assert.equal(view.gpsStale, true)
  assert.equal(viewVehicle(entry, start + 56000, 30).stale, true)
  assert.equal(viewVehicle(entry, start + 56000, 60).stale, false)
  assert.equal(
    viewVehicle({ vehicle: vehicle(), observedAt: start }, start + 30000, 30)
      .stale,
    false,
  )
})
test('history validates counts, unit IDs and chronological ordering', () => {
  const response = {
    unit_id: 666753,
    capacity: 300,
    retained_count: 2,
    count: 2,
    items: [point(), point(666753, 5)],
  }
  assert.equal(parseHistory(response).count, 2)
  assert.throws(() => parseHistory({ ...response, count: 1 }), /история/)
  assert.throws(
    () => parseHistory({ ...response, items: [point(666753, 5), point()] }),
    /история/,
  )
})
test('recording seeks backward deterministically, retains lost GPS, clears on server restart', () => {
  const recording = parseRecording(
    JSON.stringify(exampleRecording()),
    'contract-test.json',
  )
  assert.equal(recording.end - recording.start, 60000)
  assert.equal(replayAt(recording, start - 1).vehicles.size, 0)
  const lost = replayAt(recording, start + 12000).vehicles.get(666753).vehicle
  assert.equal(lost.latest.speed, null)
  assert.equal(lost.position.event_time, iso(5))
  assert.equal(
    replayAt(recording, start + 20000).vehicles.get(666753).vehicle.position
      .event_time,
    iso(5),
  )
  assert.equal(replayAt(recording, start + 56000).vehicles.size, 0)
  assert.equal(
    replayAt(recording, start).vehicles.get(666753).vehicle.position.event_time,
    iso(0),
  )
})
test('seek checkpoints are immutable and never expose future devices', () => {
  const events = [{ at: iso(0), message: snapshot([vehicle()]) }]
  for (let i = 1; i < 800; i++)
    events.push({
      at: iso(i),
      message: telemetry(vehicle(point(i > 600 ? 2 : 666753, i)), i),
    })
  const recording = parseRecording(JSON.stringify(events), 'many.json')
  assert.equal(replayAt(recording, start + 790000).vehicles.size, 2)
  assert.equal(replayAt(recording, start + 256000).vehicles.size, 1)
  assert.equal(
    replayAt(recording, start + 256000).vehicles.get(666753).vehicle.latest
      .event_time,
    iso(256),
  )
  assert.equal(
    replayAt(recording, start).vehicles.get(666753).vehicle.latest.event_time,
    iso(0),
  )
})
test('JSONL, single line and empty snapshot recordings are supported', () => {
  const rows = exampleRecording().events
  assert.equal(
    parseRecording(
      rows.map((x) => JSON.stringify(x)).join('\n'),
      'sample.jsonl',
    ).events.length,
    6,
  )
  assert.equal(
    parseRecording(JSON.stringify(rows[0]), 'one.jsonl').events.length,
    1,
  )
  const empty = parseRecording(
    JSON.stringify([{ at: iso(0), message: snapshot([]) }]),
    'empty-fleet.json',
  )
  assert.equal(replayAt(empty, start).vehicles.size, 0)
})
test('malformed recordings fail before replacing the active session', () => {
  assert.throws(
    () => parseRecording('id,lon,lat\n1,2,3', 'unknown.csv'),
    /формат/i,
  )
  assert.throws(
    () =>
      parseRecording(
        JSON.stringify([{ message: snapshot([]) }]),
        'no-clock.json',
      ),
    /at/,
  )
  assert.throws(
    () =>
      parseRecording(
        JSON.stringify([
          { at: iso(5), message: snapshot([]) },
          { at: iso(0), message: snapshot([]) },
        ]),
        'reordered.json',
      ),
    /по порядку/,
  )
  assert.throws(
    () =>
      parseRecording(
        JSON.stringify([
          { at: iso(0), message: snapshot([], 2) },
          { at: iso(1), message: snapshot([], 0) },
        ]),
        'reset.json',
      ),
    /connection_id/,
  )
  assert.throws(
    () => parseStreamMessage({ type: 'unknown', sequence: 1 }),
    /тип/,
  )
})
