export interface Telemetry {
  packet_id: string
  tr_id: number | null
  unit_id: number
  event_time: string
  device_event_id: number
  location_valid: boolean
  gps_time: string | null
  lon: number | null
  lat: number | null
  alt: number | null
  speed: number | null
  heading: number | null
  receive_time: string
  is_hist_data: boolean
}
export interface Vehicle {
  unit_id: number
  tr_id: number | null
  latest: Telemetry
  position: Telemetry | null
  last_received_at: string
  history_count: number
  is_stale: boolean
  position_age_s: number | null
  map_match: MapMatch | null
  prediction?: Prediction | null
  prediction_status?: string
  recommendations?: Recommendation[]
  arrival?: {
    stop_id: string
    event_time: string
    received_at: string
    cur_dev_s: number
    method: 'gps_estimate'
  } | null
}
export interface Prediction {
  sample_id: string
  tr_id: number
  T: string
  target_stop_id: string
  target_time_begin: string
  predicted_delay_s: number
  late_probability: number
  risk_alert: boolean
  early_risk_alert: boolean
  telemetry_stale: boolean
  cur_dev_s: number | null
  delay_threshold_s: number
  alert_threshold: number
  generated_at: string
  model_version: string
  explanations: string[]
  factors?: PredictionFactor[]
  observations?: string[]
  explanation_method?: string
  segment?: SegmentContext | null
  recommendations?: Recommendation[]
}
export interface PredictionFactor {
  feature: string
  label: string
  value: number | null
  unit: string
  contribution: number
  direction: 'increases_risk' | 'decreases_risk'
  method: 'tree_shap'
  scale: 'calibrated_log_odds'
}
export interface SegmentContext {
  segment_id: string
  graph_version: string
  from_stop_id: string
  to_stop_id: string
  from_name: string
  to_name: string
  basis: 'target_approach'
  geometry_source: string
}
export interface Recommendation {
  code: string
  title: string
  rationale: string
  priority: 'high' | 'medium' | 'low'
  requires_dispatcher: boolean
}
export interface Network {
  graph_version: string
  geometry_kind: string
  type: 'FeatureCollection'
  routes: {
    id: string
    tr_id: number
    name: string
    stops: {
      id: string
      name: string
      lon: number
      lat: number
      time_begin: string
    }[]
  }[]
  features: {
    type: 'Feature'
    properties: {
      segment_id: string
      physical_id?: string
      tr_id: number
      from_name?: string
      to_name?: string
      from_stop_id?: string
      to_stop_id?: string
      geometry_source?: string
      risk_level?: string
    }
    geometry: { type: 'LineString'; coordinates: [number, number][] }
  }[]
}
export interface RiskNotification {
  id: string
  unit_id: number
  tr_id: number
  at: string
  target_stop_id: string
  target_name: string
  target_time_begin: string
  predicted_delay_s: number
  late_probability: number
  early_risk_alert: boolean
  explanations: string[]
  observations?: string[]
  segment?: SegmentContext | null
  recommendations?: Recommendation[]
  forecast_at?: string
}
export interface Coordinates {
  lon: number
  lat: number
}
export interface MapMatch {
  status: 'matched' | 'ambiguous' | 'unmatched'
  snapped_position: Coordinates | null
  segment_id: string | null
  route_id: string | null
  direction: string | null
  source_packet_id: string
  source_event_time: string
  graph_version: string
}
export type StreamMessage =
  | { type: 'snapshot'; sequence: number; vehicles: Vehicle[] }
  | { type: 'telemetry'; sequence: number; data: Telemetry; vehicle: Vehicle }
export interface HistoryResponse {
  unit_id: number
  capacity: number
  retained_count: number
  count: number
  items: Telemetry[]
}
export interface HealthResponse {
  status: 'ok'
  ndtp_listening: boolean
  connected_devices: number
  received_packets: number
  rejected_packets: number
  vehicles: number
  history_size: number
  ml_status: 'not_connected' | 'starting' | 'ready' | 'unavailable'
  stale_after_s?: number
  ml_interval_s?: number
  last_inference?: { at: string; elapsed_ms: number; vehicles: number } | null
  source?: {
    mode: string
    label: string
    session_id: string
    elapsed_s?: number | null
    duration_s?: number | null
    cycle?: number | null
    source_start?: string | null
  }
  schedule?: {
    status: 'missing' | 'expired' | 'loaded'
    vehicles: number
    targets_now: number
  }
  emulator?: {
    status: string
    configured_units: number
    active_units: number
    interval_ms: number
    error: string | null
    auto_generate?: boolean
    accepted_measurements?: number
  } | null
}

function fail(path: string): never {
  throw new Error(`Некорректные данные API: ${path}`)
}
function object(value: unknown, path: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value))
    fail(path)
  return value as Record<string, unknown>
}
function number(value: unknown, path: string, nullable = false): number | null {
  if (nullable && value === null) return null
  if (typeof value !== 'number' || !Number.isFinite(value)) fail(path)
  return value
}
function integer(
  value: unknown,
  path: string,
  nullable = false,
): number | null {
  const n = number(value, path, nullable)
  if (n !== null && !Number.isSafeInteger(n)) fail(path)
  return n
}
function boolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') fail(path)
  return value
}
function nonempty(value: unknown, path: string): string {
  if (typeof value !== 'string' || !value.trim()) fail(path)
  return value
}
export function parseMapMatch(value: unknown): MapMatch | null {
  if (value === undefined || value === null) return null
  const path = 'Vehicle.map_match'
  const v = object(value, path)
  if (!['matched', 'ambiguous', 'unmatched'].includes(v.status as string))
    fail(`${path}.status`)
  let snapped: Coordinates | null = null
  if (v.snapped_position != null) {
    const p = object(v.snapped_position, `${path}.snapped_position`)
    const lon = number(p.lon, `${path}.snapped_position.lon`)!
    const lat = number(p.lat, `${path}.snapped_position.lat`)!
    if (Math.abs(lon) > 180 || Math.abs(lat) > 90)
      fail(`${path}: координаты вне диапазона`)
    snapped = { lon, lat }
  }
  const nullableId = (field: string) =>
    v[field] == null ? null : nonempty(v[field], `${path}.${field}`)
  const result: MapMatch = {
    status: v.status as MapMatch['status'],
    snapped_position: snapped,
    segment_id: nullableId('segment_id'),
    route_id: nullableId('route_id'),
    direction: nullableId('direction'),
    source_packet_id: nonempty(v.source_packet_id, `${path}.source_packet_id`),
    source_event_time: timestamp(
      v.source_event_time,
      `${path}.source_event_time`,
    ),
    graph_version: nonempty(v.graph_version, `${path}.graph_version`),
  }
  if (result.status === 'matched') {
    if (!snapped || result.segment_id === null)
      fail(`${path}: matched требует координаты и segment_id`)
    if (result.direction !== null && result.route_id === null)
      fail(`${path}: direction требует route_id`)
  } else if (
    [snapped, result.segment_id, result.route_id, result.direction].some(
      (x) => x !== null,
    )
  ) {
    fail(`${path}: ambiguous/unmatched требуют null в полях результата`)
  }
  return result
}
export function timestamp(value: unknown, path = 'время'): string {
  if (
    typeof value !== 'string' ||
    !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ||
    !Number.isFinite(Date.parse(value))
  )
    fail(`${path}: требуется ISO-время с часовым поясом`)
  return value
}
function parseTelemetry(value: unknown, path: string): Telemetry {
  const v = object(value, path)
  if (typeof v.packet_id !== 'string' || !v.packet_id.length)
    fail(`${path}.packet_id должен быть строкой`)
  const result: Telemetry = {
    packet_id: v.packet_id,
    tr_id: integer(v.tr_id, `${path}.tr_id`, true),
    unit_id: integer(v.unit_id, `${path}.unit_id`)!,
    event_time: timestamp(v.event_time, `${path}.event_time`),
    device_event_id: integer(v.device_event_id, `${path}.device_event_id`)!,
    location_valid: boolean(v.location_valid, `${path}.location_valid`),
    gps_time:
      v.gps_time === null ? null : timestamp(v.gps_time, `${path}.gps_time`),
    lon: number(v.lon, `${path}.lon`, true),
    lat: number(v.lat, `${path}.lat`, true),
    alt: number(v.alt, `${path}.alt`, true),
    speed: number(v.speed, `${path}.speed`, true),
    heading: number(v.heading, `${path}.heading`, true),
    receive_time: timestamp(v.receive_time, `${path}.receive_time`),
    is_hist_data: boolean(v.is_hist_data, `${path}.is_hist_data`),
  }
  if (
    !result.location_valid &&
    [
      result.lon,
      result.lat,
      result.alt,
      result.speed,
      result.heading,
      result.gps_time,
    ].some((x) => x !== null)
  )
    fail(`${path}: недостоверная навигация должна содержать null`)
  if (result.location_valid && (result.lon === null || result.lat === null))
    fail(`${path}: отсутствуют достоверные координаты`)
  if (
    (result.lon !== null && Math.abs(result.lon) > 180) ||
    (result.lat !== null && Math.abs(result.lat) > 90)
  )
    fail(`${path}: координаты вне диапазона`)
  if (result.heading !== null && (result.heading < 0 || result.heading > 360))
    fail(`${path}.heading`)
  return result
}
export function parseVehicle(value: unknown): Vehicle {
  const v = object(value, 'Vehicle')
  const unit = integer(v.unit_id, 'Vehicle.unit_id')!
  const latest = parseTelemetry(v.latest, 'Vehicle.latest')
  const position =
    v.position === null ? null : parseTelemetry(v.position, 'Vehicle.position')
  if (
    latest.unit_id !== unit ||
    (position && (position.unit_id !== unit || !position.location_valid))
  )
    fail('Vehicle: несогласованное устройство или position')
  if (
    position &&
    Date.parse(position.event_time) > Date.parse(latest.event_time)
  )
    fail('Vehicle.position новее latest')
  const count = integer(v.history_count, 'Vehicle.history_count')!
  const age = number(v.position_age_s, 'Vehicle.position_age_s', true)
  if (
    count < 0 ||
    (age !== null && age < 0) ||
    (position === null && age !== null)
  )
    fail('Vehicle: некорректный возраст или размер истории')
  return {
    unit_id: unit,
    tr_id: integer(v.tr_id, 'Vehicle.tr_id', true),
    latest,
    position,
    last_received_at: timestamp(v.last_received_at, 'Vehicle.last_received_at'),
    history_count: count,
    is_stale: boolean(v.is_stale, 'Vehicle.is_stale'),
    position_age_s: age,
    map_match: parseMapMatch(v.map_match),
    prediction: parsePrediction(v.prediction),
    prediction_status:
      typeof v.prediction_status === 'string'
        ? v.prediction_status
        : 'not_connected',
    arrival: parseArrival(v.arrival),
    recommendations: parseRecommendations(v.recommendations),
  }
}
export function parsePrediction(value: unknown): Prediction | null {
  if (value == null) return null
  const v = object(value, 'Prediction')
  const probability = number(v.late_probability, 'Prediction.late_probability')!
  const threshold = number(v.alert_threshold, 'Prediction.alert_threshold')!
  if (probability < 0 || probability > 1 || threshold < 0 || threshold > 1)
    fail('Prediction.probability')
  if (
    !Array.isArray(v.explanations) ||
    v.explanations.some((x) => typeof x !== 'string')
  )
    fail('Prediction.explanations')
  return {
    sample_id: nonempty(v.sample_id, 'Prediction.sample_id'),
    tr_id: integer(v.tr_id, 'Prediction.tr_id')!,
    T: timestamp(v.T),
    target_stop_id: nonempty(v.target_stop_id, 'Prediction.target_stop_id'),
    target_time_begin: timestamp(v.target_time_begin),
    predicted_delay_s: number(
      v.predicted_delay_s,
      'Prediction.predicted_delay_s',
    )!,
    late_probability: probability,
    risk_alert: boolean(v.risk_alert, 'Prediction.risk_alert'),
    early_risk_alert: boolean(
      v.early_risk_alert,
      'Prediction.early_risk_alert',
    ),
    telemetry_stale: boolean(v.telemetry_stale, 'Prediction.telemetry_stale'),
    cur_dev_s: number(v.cur_dev_s, 'Prediction.cur_dev_s', true),
    delay_threshold_s: number(
      v.delay_threshold_s,
      'Prediction.delay_threshold_s',
    )!,
    alert_threshold: threshold,
    generated_at: timestamp(v.generated_at),
    model_version: nonempty(v.model_version, 'Prediction.model_version'),
    explanations: v.explanations as string[],
    factors: parseFactors(v.factors),
    observations: parseStrings(v.observations, 'Prediction.observations'),
    explanation_method:
      typeof v.explanation_method === 'string'
        ? v.explanation_method
        : 'unavailable',
    segment: parseSegment(v.segment),
    recommendations: parseRecommendations(v.recommendations),
  }
}
function parseStrings(value: unknown, path: string): string[] {
  if (value == null) return []
  if (!Array.isArray(value) || value.some((x) => typeof x !== 'string'))
    fail(path)
  return value as string[]
}
function parseRecommendations(value: unknown): Recommendation[] {
  if (value == null) return []
  if (!Array.isArray(value)) fail('recommendations')
  return value.map((item) => {
    const v = object(item, 'Recommendation')
    if (!['high', 'medium', 'low'].includes(v.priority as string))
      fail('Recommendation.priority')
    return {
      code: nonempty(v.code, 'Recommendation.code'),
      title: nonempty(v.title, 'Recommendation.title'),
      rationale: nonempty(v.rationale, 'Recommendation.rationale'),
      priority: v.priority as Recommendation['priority'],
      requires_dispatcher: boolean(
        v.requires_dispatcher,
        'Recommendation.requires_dispatcher',
      ),
    }
  })
}
function parseSegment(value: unknown): SegmentContext | null {
  if (value == null) return null
  const v = object(value, 'Segment')
  if (v.basis !== 'target_approach') fail('Segment.basis')
  return {
    segment_id: nonempty(v.segment_id, 'Segment.id'),
    graph_version: nonempty(v.graph_version, 'Segment.version'),
    from_stop_id: nonempty(v.from_stop_id, 'Segment.from'),
    to_stop_id: nonempty(v.to_stop_id, 'Segment.to'),
    from_name: nonempty(v.from_name, 'Segment.from_name'),
    to_name: nonempty(v.to_name, 'Segment.to_name'),
    basis: 'target_approach',
    geometry_source: nonempty(v.geometry_source, 'Segment.geometry_source'),
  }
}
function parseFactors(value: unknown): PredictionFactor[] {
  if (value == null) return []
  if (!Array.isArray(value)) fail('Prediction.factors')
  return value.map((item) => {
    const v = object(item, 'Factor')
    if (
      v.method !== 'tree_shap' ||
      v.scale !== 'calibrated_log_odds' ||
      !['increases_risk', 'decreases_risk'].includes(v.direction as string)
    )
      fail('Factor.method')
    return {
      feature: nonempty(v.feature, 'Factor.feature'),
      label: nonempty(v.label, 'Factor.label'),
      value: number(v.value, 'Factor.value', true),
      unit: typeof v.unit === 'string' ? v.unit : '',
      contribution: number(v.contribution, 'Factor.contribution')!,
      direction: v.direction as PredictionFactor['direction'],
      method: 'tree_shap',
      scale: 'calibrated_log_odds',
    }
  })
}
function parseArrival(value: unknown): Vehicle['arrival'] {
  if (value == null) return null
  const v = object(value, 'Arrival')
  if (v.method !== 'gps_estimate') fail('Arrival.method')
  return {
    stop_id: nonempty(v.stop_id, 'Arrival.stop_id'),
    event_time: timestamp(v.event_time),
    received_at: timestamp(v.received_at),
    cur_dev_s: number(v.cur_dev_s, 'Arrival.cur_dev_s')!,
    method: 'gps_estimate',
  }
}
export function parseVehicles(value: unknown): Vehicle[] {
  if (!Array.isArray(value)) fail('vehicles: ожидается массив')
  const vehicles = value.map(parseVehicle)
  if (new Set(vehicles.map((v) => v.unit_id)).size !== vehicles.length)
    fail('vehicles: повтор unit_id')
  return vehicles
}
export function parseStreamMessage(value: unknown): StreamMessage {
  const v = object(value, 'сообщение')
  const sequence = integer(v.sequence, 'sequence')!
  if (sequence < 0) fail('sequence')
  if (v.type === 'snapshot')
    return { type: 'snapshot', sequence, vehicles: parseVehicles(v.vehicles) }
  if (v.type === 'telemetry') {
    const vehicle = parseVehicle(v.vehicle),
      data = parseTelemetry(v.data, 'data')
    if (vehicle.unit_id !== data.unit_id)
      fail('data.unit_id не совпадает с vehicle.unit_id')
    return { type: 'telemetry', sequence, data, vehicle }
  }
  return fail('неизвестный тип сообщения')
}
export function parseHistory(value: unknown): HistoryResponse {
  const v = object(value, 'History')
  if (!Array.isArray(v.items)) fail('History.items')
  const unit = integer(v.unit_id, 'History.unit_id')!
  const items = v.items.map((item, i) =>
    parseTelemetry(item, `History.items[${i}]`),
  )
  const count = integer(v.count, 'History.count')!,
    retained = integer(v.retained_count, 'History.retained_count')!,
    capacity = integer(v.capacity, 'History.capacity')!
  if (
    count !== items.length ||
    count < 0 ||
    retained < count ||
    capacity < retained ||
    items.some(
      (item, i) =>
        item.unit_id !== unit ||
        (i > 0 &&
          Date.parse(item.event_time) < Date.parse(items[i - 1].event_time)),
    )
  )
    fail('History: несогласованная история')
  return { unit_id: unit, capacity, retained_count: retained, count, items }
}
export function parseHealth(value: unknown): HealthResponse {
  const v = object(value, 'Health')
  if (
    v.status !== 'ok' ||
    !['not_connected', 'starting', 'ready', 'unavailable'].includes(
      v.ml_status as string,
    )
  )
    fail('Health: неизвестная версия статуса ML или сервиса')
  return {
    status: 'ok',
    ml_status: v.ml_status as HealthResponse['ml_status'],
    stale_after_s:
      v.stale_after_s === undefined
        ? undefined
        : number(v.stale_after_s, 'Health.stale_after_s')!,
    ml_interval_s:
      v.ml_interval_s === undefined
        ? undefined
        : number(v.ml_interval_s, 'Health.ml_interval_s')!,
    last_inference: v.last_inference as HealthResponse['last_inference'],
    source: v.source as HealthResponse['source'],
    schedule: v.schedule as HealthResponse['schedule'],
    emulator: v.emulator as HealthResponse['emulator'],
    ndtp_listening: boolean(v.ndtp_listening, 'Health.ndtp_listening'),
    connected_devices: integer(
      v.connected_devices,
      'Health.connected_devices',
    )!,
    received_packets: integer(v.received_packets, 'Health.received_packets')!,
    rejected_packets: integer(v.rejected_packets, 'Health.rejected_packets')!,
    vehicles: integer(v.vehicles, 'Health.vehicles')!,
    history_size: integer(v.history_size, 'Health.history_size')!,
  }
}
