import type { Coordinates, Vehicle } from './contract.ts'

export interface MatchingGraph {
  version: string
  segmentIds: ReadonlySet<string>
}

export function parseMatchingGraph(input: unknown): MatchingGraph {
  const fail = (reason: string): never => {
    throw new Error(`Некорректный файл сети: ${reason}`)
  }
  if (!input || typeof input !== 'object') fail('ожидается GeoJSON')
  const root = input as Record<string, unknown>
  if (root.type !== 'FeatureCollection' || !Array.isArray(root.features))
    fail('ожидается FeatureCollection')
  if (typeof root.graph_version !== 'string' || !root.graph_version.trim())
    fail('не указана graph_version')
  const segmentIds = new Set<string>()
  for (const item of root.features as unknown[]) {
    if (!item || typeof item !== 'object') fail('неверный участок')
    const f = item as {
      type?: unknown
      properties?: { segment_id?: unknown }
      geometry?: { type?: unknown; coordinates?: unknown }
    }
    const id = f.properties?.segment_id
    if (f.type !== 'Feature' || typeof id !== 'string' || !id.trim())
      fail('участку нужен строковый properties.segment_id')
    if (segmentIds.has(id as string)) fail(`повтор segment_id: ${id}`)
    const coords = f.geometry?.coordinates
    if (
      f.geometry?.type !== 'LineString' ||
      !Array.isArray(coords) ||
      coords.length < 2
    )
      fail(`участок ${id}: нужен LineString минимум с двумя точками`)
    for (const point of coords as unknown[]) {
      if (
        !Array.isArray(point) ||
        point.length < 2 ||
        !point.every((x) => typeof x === 'number' && Number.isFinite(x)) ||
        Math.abs(point[0]) > 180 ||
        Math.abs(point[1]) > 90
      )
        fail(`участок ${id}: неверные координаты [lon, lat]`)
    }
    segmentIds.add(id as string)
  }
  return { version: root.graph_version as string, segmentIds }
}

export type MatchDisplayState =
  | 'no-position'
  | 'pending'
  | 'ambiguous'
  | 'unmatched'
  | 'source-mismatch'
  | 'graph-missing'
  | 'graph-mismatch'
  | 'segment-missing'
  | 'matched'
export interface MatchDisplay {
  state: MatchDisplayState
  label: string
  description: string
  position: Coordinates | null
  applied: boolean
  segmentId: string | null
  routeId: string | null
  direction: string | null
}

export function resolveMapMatch(
  v: Vehicle,
  graph: MatchingGraph | null,
): MatchDisplay {
  const raw = v.position ? { lon: v.position.lon!, lat: v.position.lat! } : null
  const fallback = (
    state: MatchDisplayState,
    label: string,
    description: string,
  ): MatchDisplay => ({
    state,
    label,
    description,
    position: raw,
    applied: false,
    segmentId: null,
    routeId: null,
    direction: null,
  })
  if (!v.position)
    return fallback(
      'no-position',
      'Нет координат для привязки',
      'Устройство остаётся в списке без маркера.',
    )
  const match = v.map_match
  if (!match)
    return fallback(
      'pending',
      'Привязка не рассчитана',
      'Показана последняя достоверная GPS-позиция.',
    )

  if (
    match.source_packet_id !== v.position.packet_id ||
    Date.parse(match.source_event_time) !== Date.parse(v.position.event_time)
  )
    return fallback(
      'source-mismatch',
      'Привязка к другой GPS-точке',
      'Результат не применён. Показаны исходные координаты.',
    )
  if (match.status === 'ambiguous')
    return fallback(
      'ambiguous',
      'Неоднозначная привязка',
      'Бэкенд не выбрал один участок. Показан исходный GPS.',
    )
  if (match.status === 'unmatched')
    return fallback(
      'unmatched',
      'Участок не найден',
      'Бэкенд не нашёл подходящий участок. Показан исходный GPS.',
    )
  if (!graph)
    return fallback(
      'graph-missing',
      'Сеть участков не загружена',
      'Для проверки привязки нужен файл сети. Пока показан исходный GPS.',
    )
  if (match.graph_version !== graph.version)
    return fallback(
      'graph-mismatch',
      'Версия сети отличается',
      'Привязка рассчитана по другой версии сети. Показан исходный GPS.',
    )
  if (!graph.segmentIds.has(match.segment_id!))
    return fallback(
      'segment-missing',
      'Участок отсутствует в сети',
      'Участок из результата не найден в файле сети. Показан исходный GPS.',
    )
  return {
    state: 'matched',
    label: 'Привязка подтверждена',
    description:
      'На схеме показана привязанная точка. Исходный GPS сохранён в карточке.',
    position: match.snapped_position,
    applied: true,
    segmentId: match.segment_id,
    routeId: match.route_id,
    direction: match.direction,
  }
}
