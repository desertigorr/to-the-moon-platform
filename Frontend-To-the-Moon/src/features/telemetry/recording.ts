import {
  parseStreamMessage,
  timestamp,
  type StreamMessage,
} from './contract.ts'
import {
  applyMessage,
  emptyFleet,
  newConnection,
  type FleetState,
} from './fleet.ts'

export interface RecordedEvent {
  at: number
  connection: string
  message: StreamMessage
}
export interface Recording {
  name: string
  start: number
  end: number
  events: RecordedEvent[]
  checkpoints: { index: number; connection: string; fleet: FleetState }[]
  bounds: readonly [number, number, number, number] | null
}

export function parseRecording(text: string, name: string): Recording {
  const content = text.replace(/^\uFEFF/, '').trim()
  if (!content) throw new Error('Файл записи пуст.')
  let input: unknown
  try {
    input = JSON.parse(content)
  } catch {
    try {
      input = content
        .split(/\r?\n/)
        .filter((line) => line.trim())
        .map((line) => JSON.parse(line) as unknown)
    } catch {
      throw new Error(
        'Не удалось прочитать JSON/JSONL. Формат записи нужно согласовать с выгрузкой Игоря.',
      )
    }
  }
  const envelope =
    input && typeof input === 'object' && !Array.isArray(input)
      ? (input as Record<string, unknown>)
      : null
  const rows = Array.isArray(input)
    ? input
    : (envelope?.events ?? (envelope?.message ? [envelope] : null))
  if (!Array.isArray(rows) || !rows.length)
    throw new Error(
      'Ожидается массив events с временем at и сообщением message. Для другого формата нужен адаптер выгрузки.',
    )
  if (rows.length > 200000)
    throw new Error('Запись слишком велика: максимум 200 000 сообщений.')
  let previousAt = -Infinity,
    connection = '',
    fleet = emptyFleet()
  const checkpoints: Recording['checkpoints'] = []
  const events: RecordedEvent[] = []
  let minLon = Infinity,
    minLat = Infinity,
    maxLon = -Infinity,
    maxLat = -Infinity
  for (const [index, row] of rows.entries()) {
    if (!row || typeof row !== 'object')
      throw new Error(`Событие ${index + 1}: ожидается объект.`)
    const v = row as Record<string, unknown>
    const at = Date.parse(timestamp(v.at, `Событие ${index + 1}.at`))
    if (at < previousAt)
      throw new Error(
        `Событие ${index + 1}: время получения должно идти по порядку. Поздние GPS-пакеты не сортируются по event_time.`,
      )
    const session = v.connection_id ?? 'default'
    if (typeof session !== 'string' || !session)
      throw new Error(`Событие ${index + 1}: неверный connection_id.`)
    const message = parseStreamMessage(v.message)
    if (session !== connection) {
      if (message.type !== 'snapshot')
        throw new Error(
          `Событие ${index + 1}: соединение должно начинаться со snapshot.`,
        )
      fleet = newConnection(fleet)
      connection = session
    }
    if (
      message.type === 'snapshot' &&
      fleet.sequence !== null &&
      message.sequence < fleet.sequence
    )
      throw new Error(
        `Событие ${index + 1}: для рестарта укажите новый connection_id.`,
      )
    fleet = applyMessage(fleet, message, at)
    if (index % 256 === 0) checkpoints.push({ index, connection, fleet })

    const vehicles =
      message.type === 'snapshot' ? message.vehicles : [message.vehicle]
    for (const v of vehicles)
      if (v.position) {
        minLon = Math.min(minLon, v.position.lon!)
        maxLon = Math.max(maxLon, v.position.lon!)
        minLat = Math.min(minLat, v.position.lat!)
        maxLat = Math.max(maxLat, v.position.lat!)
      }
    events.push({ at, connection: session, message })
    previousAt = at
  }
  const start =
    envelope?.started_at === undefined
      ? events[0].at
      : Date.parse(timestamp(envelope.started_at, 'started_at'))
  const end =
    envelope?.ended_at === undefined
      ? events.at(-1)!.at
      : Date.parse(timestamp(envelope.ended_at, 'ended_at'))
  if (start > events[0].at || end < events.at(-1)!.at || end < start)
    throw new Error('Границы записи не охватывают все сообщения.')
  return {
    name,
    start,
    end,
    events,
    checkpoints,
    bounds: Number.isFinite(minLon) ? [minLon, minLat, maxLon, maxLat] : null,
  }
}

export function replayAt(recording: Recording, at: number): FleetState {
  let lo = 0,
    hi = recording.events.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (recording.events[mid].at <= at) lo = mid + 1
    else hi = mid
  }
  const endIndex = lo - 1
  if (endIndex < 0) return emptyFleet()
  const checkpoint = recording.checkpoints[Math.floor(endIndex / 256)]
  let fleet = checkpoint.fleet,
    connection = checkpoint.connection
  for (let i = checkpoint.index + 1; i <= endIndex; i++) {
    const event = recording.events[i]
    if (connection !== event.connection) {
      fleet = newConnection(fleet)
      connection = event.connection
    }
    fleet = applyMessage(fleet, event.message, event.at)
  }
  return fleet
}
