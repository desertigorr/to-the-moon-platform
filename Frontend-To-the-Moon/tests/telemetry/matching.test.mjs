import assert from 'node:assert/strict'
import test from 'node:test'
import {
  parseVehicle,
  parseMapMatch,
  parseStreamMessage,
} from '../../src/features/telemetry/contract.ts'
import {
  parseMatchingGraph,
  resolveMapMatch,
} from '../../src/features/telemetry/matching.ts'
import {
  applyMessage,
  emptyFleet,
  viewVehicle,
} from '../../src/features/telemetry/fleet.ts'
import {
  parseRecording,
  replayAt,
} from '../../src/features/telemetry/recording.ts'
import { point, vehicle, snapshot, telemetry, start, iso } from './fixtures.mjs'

export function graphFixture() {
  return {
    type: 'FeatureCollection',
    graph_version: 'contract-test-v2',
    features: [
      {
        type: 'Feature',
        properties: { segment_id: 'test-segment-1' },
        geometry: {
          type: 'LineString',
          coordinates: [
            [37.617, 55.755],
            [37.619, 55.756],
          ],
        },
      },
    ],
  }
}
export function matchFixture(p = point(), changes = {}) {
  return {
    status: 'matched',
    snapped_position: { lon: p.lon + 0.0001, lat: p.lat + 0.0001 },
    segment_id: 'test-segment-1',
    route_id: null,
    direction: null,
    source_packet_id: p.packet_id,
    source_event_time: p.event_time,
    graph_version: 'contract-test-v2',
    ...changes,
  }
}
const graph = parseMatchingGraph(graphFixture())

test('v0.2 normalizes optional fields and preserves valid zero coordinates and string source IDs', () => {
  const p = point()
  const v = parseVehicle(
    vehicle(p, {
      map_match: matchFixture(p, { snapped_position: { lon: 0, lat: 0 } }),
    }),
  )
  assert.deepEqual(resolveMapMatch(v, graph).position, { lon: 0, lat: 0 })
  assert.deepEqual(v.position, p)
  assert.equal(v.map_match.source_packet_id, p.packet_id)
  const omitted = vehicle()
  delete omitted.map_match
  assert.equal(parseVehicle(omitted).map_match, null)
  assert.equal(parseVehicle(vehicle()).map_match, null)
  const optional = matchFixture(p, { status: 'unmatched' })
  for (const key of ['snapped_position', 'segment_id', 'route_id', 'direction'])
    delete optional[key]
  assert.equal(parseMapMatch(optional).snapped_position, null)
})

test('invalid matches fail validation instead of creating plausible but false positions', () => {
  const invalid = [
    { status: 'unknown' },
    { snapped_position: null },
    { segment_id: null },
    { segment_id: '' },
    { graph_version: '' },
    { source_packet_id: 123 },
    { source_event_time: '2026-09-26T12:00:00' },
    { snapped_position: { lon: 0, lat: 91 } },
    { snapped_position: { lon: Infinity, lat: 0 } },
    { snapped_position: { lon: '0', lat: 0 } },
    { direction: 'outbound', route_id: null },
    { status: 'ambiguous' },
    { status: 'unmatched' },
  ]
  for (const change of invalid)
    assert.throws(
      () =>
        parseVehicle(
          vehicle(undefined, { map_match: matchFixture(undefined, change) }),
        ),
      /map_match/,
    )
})

test('a graph needs stable unique segment IDs, a version and valid lines', () => {
  assert.equal(graph.version, 'contract-test-v2')
  assert.equal(graph.segmentIds.size, 1)
  assert.throws(
    () => parseMatchingGraph({ ...graphFixture(), graph_version: '' }),
    /graph_version/,
  )
  assert.throws(
    () =>
      parseMatchingGraph({
        ...graphFixture(),
        features: [...graphFixture().features, ...graphFixture().features],
      }),
    /повтор/,
  )
  const missingId = graphFixture()
  missingId.features[0].properties = { id: 'legacy-id' }
  assert.throws(() => parseMatchingGraph(missingId), /segment_id/)
  const invalidPoint = graphFixture()
  invalidPoint.features[0].geometry.coordinates[1][0] = 181
  assert.throws(() => parseMatchingGraph(invalidPoint), /координаты/)
  const pointGeometry = graphFixture()
  pointGeometry.features[0].geometry.type = 'Point'
  assert.throws(() => parseMatchingGraph(pointGeometry), /LineString/)
})

test('unverified source, graph and segment fall back to raw GPS and hide route claims', () => {
  const p = point()
  const raw = { lon: p.lon, lat: p.lat }
  for (const [change, sourceGraph, state] of [
    [{ source_packet_id: 'another-packet' }, graph, 'source-mismatch'],
    [{ source_event_time: iso(1) }, graph, 'source-mismatch'],
    [{ graph_version: 'other-version' }, graph, 'graph-mismatch'],
    [{}, null, 'graph-missing'],
    [{ segment_id: 'missing' }, graph, 'segment-missing'],
  ]) {
    const v = parseVehicle(
      vehicle(p, {
        map_match: matchFixture(p, {
          route_id: 'test-route',
          direction: 'outbound',
          ...change,
        }),
      }),
    )
    const resolved = resolveMapMatch(v, sourceGraph)
    assert.equal(resolved.state, state)
    assert.deepEqual(resolved.position, raw)
    assert.equal(resolved.routeId, null)
    assert.equal(resolved.direction, null)
  }
  const matched = resolveMapMatch(
    parseVehicle(
      vehicle(p, {
        map_match: matchFixture(p, {
          source_event_time: '2026-09-26T15:00:00+03:00',
          route_id: 'test-route',
          direction: 'outbound',
        }),
      }),
    ),
    graph,
  )
  assert.equal(matched.applied, true)
  assert.equal(matched.routeId, 'test-route')
  assert.equal(matched.direction, 'outbound')
})

test('null, ambiguous and unmatched are distinct states with no invented marker or route', () => {
  for (const status of ['ambiguous', 'unmatched']) {
    const v = parseVehicle(
      vehicle(undefined, {
        map_match: matchFixture(undefined, {
          status,
          snapped_position: null,
          segment_id: null,
        }),
      }),
    )
    const resolved = resolveMapMatch(v, graph)
    assert.equal(resolved.state, status)
    assert.deepEqual(resolved.position, {
      lon: v.position.lon,
      lat: v.position.lat,
    })
    assert.equal(resolved.applied, false)
  }
  assert.equal(resolveMapMatch(parseVehicle(vehicle()), graph).state, 'pending')
  const noGps = point(666753, 10, {
    location_valid: false,
    lon: null,
    lat: null,
    alt: null,
    heading: null,
    speed: null,
    gps_time: null,
  })
  const v = parseVehicle(
    vehicle(noGps, {
      position: null,
      position_age_s: null,
      map_match: matchFixture(),
    }),
  )
  assert.equal(resolveMapMatch(v, graph).position, null)
})

test('GPS loss retains matching of position rather than latest, without making old GPS fresh', () => {
  const p = point()
  const lost = point(666753, 25, {
    location_valid: false,
    lon: null,
    lat: null,
    alt: null,
    heading: null,
    speed: null,
    gps_time: null,
  })
  const v = parseVehicle(
    vehicle(lost, {
      position: p,
      position_age_s: 25,
      map_match: matchFixture(p),
    }),
  )
  const result = viewVehicle(
    { vehicle: v, observedAt: start + 25000 },
    start + 31000,
    30,
    graph,
  )
  assert.equal(result.matching.applied, true)
  assert.equal(result.positionAge, 31)
  assert.equal(result.gpsStale, true)
  assert.equal(result.gpsMissing, true)
  assert.equal(result.status, 'no-gps')
})

test('same-sequence snapshot applies matching, new GPS clears it and replay cannot leak future results', () => {
  const first = vehicle(),
    matched = vehicle(undefined, { map_match: matchFixture() })
  let state = applyMessage(
    emptyFleet(),
    parseStreamMessage(snapshot([first], 1)),
    start,
  )
  state = applyMessage(
    state,
    parseStreamMessage(snapshot([matched], 1)),
    start + 2000,
  )
  assert.equal(
    resolveMapMatch(state.vehicles.get(first.unit_id).vehicle, graph).applied,
    true,
  )
  const moved = vehicle(point(666753, 5))
  const recording = parseRecording(
    JSON.stringify({
      events: [
        { at: iso(0), message: snapshot([first], 1) },
        { at: iso(2), message: snapshot([matched], 1) },
        { at: iso(5), message: telemetry(moved, 2) },
        {
          at: iso(6),
          message: snapshot([{ ...moved, map_match: matchFixture() }], 2),
        },
      ],
    }),
    'contract-v0.2-test.json',
  )
  const at = (seconds) =>
    resolveMapMatch(
      replayAt(recording, start + seconds * 1000).vehicles.get(first.unit_id)
        .vehicle,
      graph,
    )
  assert.equal(at(2).applied, true)
  assert.equal(at(5).state, 'pending')
  assert.equal(at(6).state, 'source-mismatch')
  assert.equal(at(0).state, 'pending')
  assert.equal(at(2).applied, true)
})
