import type { StreamMessage, Vehicle } from './contract.ts'
import { resolveMapMatch, type MatchingGraph } from './matching.ts'

export interface ObservedVehicle {
  vehicle: Vehicle
  observedAt: number
}
export interface FleetState {
  sequence: number | null
  ready: boolean
  vehicles: ReadonlyMap<number, ObservedVehicle>
}
export function emptyFleet(): FleetState {
  return { sequence: null, ready: false, vehicles: new Map() }
}
export function newConnection(state: FleetState): FleetState {
  return { ...state, ready: false, sequence: null }
}
export function applyMessage(
  state: FleetState,
  message: StreamMessage,
  observedAt: number,
): FleetState {
  if (message.type === 'snapshot') {
    if (state.sequence !== null && message.sequence < state.sequence)
      return state
    return {
      sequence: message.sequence,
      ready: true,
      vehicles: new Map(
        message.vehicles.map((vehicle) => [
          vehicle.unit_id,
          { vehicle, observedAt },
        ]),
      ),
    }
  }
  if (
    !state.ready ||
    (state.sequence !== null && message.sequence <= state.sequence)
  )
    return state
  const vehicles = new Map(state.vehicles)
  vehicles.set(message.vehicle.unit_id, {
    vehicle: message.vehicle,
    observedAt,
  })
  return { sequence: message.sequence, ready: true, vehicles }
}
export function viewVehicle(
  entry: ObservedVehicle,
  at: number,
  staleAfterSeconds: number,
  graph: MatchingGraph | null = null,
) {
  const { vehicle: v, observedAt } = entry
  const elapsed = Math.max(0, (at - observedAt) / 1000)
  const telemetryAge = Math.max(
    0,
    (at - Date.parse(v.latest.event_time)) / 1000,
  )
  const receivedAge = Math.max(0, (at - Date.parse(v.last_received_at)) / 1000)
  const positionAge =
    v.position === null
      ? null
      : (v.position_age_s ??
          Math.max(
            0,
            (observedAt - Date.parse(v.position.event_time)) / 1000,
          )) + elapsed
  const stale =
    v.is_stale ||
    telemetryAge > staleAfterSeconds ||
    receivedAge > staleAfterSeconds
  const gpsMissing = !v.latest.location_valid || v.position === null
  const gpsStale = positionAge !== null && positionAge > staleAfterSeconds
  const status = stale
    ? 'stale'
    : gpsMissing
      ? 'no-gps'
      : gpsStale
        ? 'old-gps'
        : 'fresh'
  const label = v.tr_id === null ? `Устройство ${v.unit_id}` : `ТС ${v.tr_id}`
  const description =
    status === 'stale'
      ? 'Данные устарели'
      : status === 'no-gps'
        ? 'Нет достоверного GPS'
        : status === 'old-gps'
          ? 'Координаты устарели'
          : 'Свежие данные'
  return {
    ...entry,
    label,
    status,
    description,
    stale,
    gpsMissing,
    gpsStale,
    positionAge,
    telemetryAge,
    receivedAge,
    matching: resolveMapMatch(v, graph),
  }
}
export type VehicleView = ReturnType<typeof viewVehicle>
