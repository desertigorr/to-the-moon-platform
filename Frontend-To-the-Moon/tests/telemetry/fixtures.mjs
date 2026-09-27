export const start = Date.parse('2026-09-26T12:00:00Z')
export const iso = (seconds) => new Date(start + seconds * 1000).toISOString()
export function point(unit = 666753, seconds = 0, changes = {}) {
  return {
    packet_id: `1790425560952971${unit}${seconds}`,
    tr_id: unit === 666753 ? 120439 : null,
    unit_id: unit,
    event_time: iso(seconds),
    device_event_id: 0,
    location_valid: true,
    gps_time: iso(seconds),
    lon: 37.617321 + seconds * 0.0001,
    lat: 55.7551234 + seconds * 0.00004,
    alt: 150,
    speed: 0,
    heading: 0,
    receive_time: iso(seconds),
    is_hist_data: false,
    ...changes,
  }
}
export function vehicle(p = point(), changes = {}) {
  return {
    unit_id: p.unit_id,
    tr_id: p.tr_id,
    latest: p,
    position: p,
    last_received_at: p.receive_time,
    history_count: 1,
    is_stale: false,
    position_age_s: 0,
    map_match: null,
    ...changes,
  }
}
export const snapshot = (vehicles, sequence = 0) => ({
  type: 'snapshot',
  sequence,
  vehicles,
})
export const telemetry = (v, sequence, data = v.latest) => ({
  type: 'telemetry',
  sequence,
  data,
  vehicle: v,
})
export function exampleRecording() {
  const first = vehicle(),
    second = vehicle(point(794446, 0, { lon: 37.62, lat: 55.758 })),
    moved = point(666753, 5, { speed: 25, heading: 45 })
  const lost = point(666753, 10, {
    location_valid: false,
    gps_time: null,
    lon: null,
    lat: null,
    alt: null,
    speed: null,
    heading: null,
  })
  const gpsLost = vehicle(lost, {
    position: moved,
    position_age_s: 5,
    history_count: 3,
  })
  const late = point(666753, 0, {
    packet_id: '179042556095297199999',
    receive_time: iso(15),
  })
  const events = [
    { at: iso(0), message: snapshot([first, second]) },
    { at: iso(5), message: telemetry(vehicle(moved, { history_count: 2 }), 1) },
    { at: iso(10), message: telemetry(gpsLost, 2) },
    {
      at: iso(15),
      message: telemetry({ ...gpsLost, last_received_at: iso(15) }, 3, late),
    },
    {
      at: iso(50),
      message: snapshot(
        [
          {
            ...gpsLost,
            last_received_at: iso(15),
            is_stale: true,
            position_age_s: 45,
          },
          { ...second, is_stale: true, position_age_s: 50 },
        ],
        3,
      ),
    },
    { at: iso(55), connection_id: 'restarted', message: snapshot([]) },
  ]
  return {
    format: 'local-api-recording-v1',
    started_at: iso(0),
    ended_at: iso(60),
    events,
  }
}
