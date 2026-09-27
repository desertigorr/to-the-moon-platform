import {
  parseHealth,
  parseHistory,
  parseStreamMessage,
  parseVehicle,
  parseVehicles,
  type StreamMessage,
} from './contract.ts'
export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected'
export function apiUrl(base: string, path: string): URL {
  const url = new URL(base)
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password
  )
    throw new Error('Укажите HTTP(S)-адрес API без логина и пароля в URL.')
  url.pathname = `${url.pathname.replace(/\/$/, '')}${path}`
  url.search = ''
  url.hash = ''
  return url
}
export function streamUrl(base: string): string {
  const url = apiUrl(base, '/api/stream')
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}
export class TelemetryApi {
  base: string
  constructor(base: string) {
    this.base = base
  }
  private async get(
    path: string,
    signal?: AbortSignal,
    query?: Record<string, string>,
  ) {
    const url = apiUrl(this.base, path)
    Object.entries(query ?? {}).forEach(([key, value]) =>
      url.searchParams.set(key, value),
    )
    const response = await fetch(url, {
      signal,
      headers: { Accept: 'application/json' },
    })
    if (!response.ok)
      throw new Error(
        response.status === 404
          ? 'Устройство не найдено.'
          : response.status === 422
            ? 'API отклонил параметры запроса.'
            : `Ошибка API: HTTP ${response.status}`,
      )
    return response.json() as Promise<unknown>
  }
  async vehicles(signal?: AbortSignal) {
    return parseVehicles(await this.get('/api/vehicles', signal))
  }
  async vehicle(unit: number, signal?: AbortSignal) {
    return parseVehicle(
      await this.get(`/api/vehicles/${encodeURIComponent(unit)}`, signal),
    )
  }
  async history(
    unit: number,
    options: { limit?: number; until?: string } = {},
    signal?: AbortSignal,
  ) {
    const query: Record<string, string> = {}
    if (options.limit !== undefined) query.limit = String(options.limit)
    if (options.until !== undefined) query.until = options.until
    return parseHistory(
      await this.get(
        `/api/vehicles/${encodeURIComponent(unit)}/history`,
        signal,
        query,
      ),
    )
  }
  async health(signal?: AbortSignal) {
    return parseHealth(await this.get('/api/health', signal))
  }
}

export interface SocketPort {
  onopen: (() => void) | null
  onclose: (() => void) | null
  onerror: (() => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  close(): void
}
interface StreamOptions {
  createSocket?: (url: string) => SocketPort
  schedule?: (
    callback: () => void,
    delay: number,
  ) => ReturnType<typeof setTimeout>
  cancel?: (timer: ReturnType<typeof setTimeout>) => void
  onConnection: (status: ConnectionStatus) => void
  onMessage: (message: StreamMessage) => void
  onError: (message: string | null) => void
}

export function connectStream(
  base: string,
  options: StreamOptions,
): () => void {
  const url = streamUrl(base)
  const schedule = options.schedule ?? setTimeout,
    cancel = options.cancel ?? clearTimeout
  const createSocket =
    options.createSocket ??
    ((url) => new WebSocket(url) as unknown as SocketPort)
  let disposed = false,
    attempt = 0,
    socket: SocketPort | null = null
  let timer: ReturnType<typeof setTimeout> | null = null,
    watchdog: ReturnType<typeof setTimeout> | null = null
  const clearWatchdog = () => {
    if (watchdog !== null) cancel(watchdog)
    watchdog = null
  }
  const launch = () => {
    if (disposed) return
    timer = null
    options.onConnection('connecting')
    let ready = false
    const retry = () => {
      if (disposed || timer !== null) return
      options.onConnection('disconnected')
      timer = schedule(
        launch,
        Math.min(10000, 1000 * 2 ** Math.min(attempt++, 4)),
      )
    }
    let current: SocketPort
    try {
      current = createSocket(url)
      socket = current
    } catch {
      options.onError('Не удалось подключиться к API. Повторяем попытку.')
      retry()
      return
    }
    const alive = () => !disposed && socket === current
    const armWatchdog = () => {
      clearWatchdog()
      watchdog = schedule(() => {
        if (!alive()) return
        options.onError('API не присылает данные. Переподключаемся.')
        socket = null
        current.onclose = null
        current.onmessage = null
        current.onerror = null
        current.onopen = null
        current.close()
        retry()
      }, 30000)
    }
    armWatchdog()
    current.onopen = () => {
      if (alive()) armWatchdog()
    }
    current.onmessage = (event) => {
      if (!alive()) return
      try {
        if (typeof event.data !== 'string')
          throw new Error('API прислал сообщение не в формате JSON.')
        const message = parseStreamMessage(JSON.parse(event.data))
        if (!ready && message.type !== 'snapshot')
          throw new Error('Ожидаем начальный полный список транспорта.')
        if (!ready) {
          ready = true
          attempt = 0
          options.onConnection('connected')
        }
        armWatchdog()
        options.onError(null)
        options.onMessage(message)
      } catch (error) {
        options.onError(
          error instanceof Error ? error.message : 'Ошибка данных API.',
        )
      }
    }
    current.onerror = () => {
      if (alive())
        options.onError(
          'Нет связи с API. Проверьте адрес сервера и разрешённый Origin.',
        )
    }
    current.onclose = () => {
      if (!alive()) return
      socket = null
      clearWatchdog()
      retry()
    }
  }
  launch()
  return () => {
    disposed = true
    if (timer !== null) cancel(timer)
    clearWatchdog()
    if (socket) {
      socket.onmessage = null
      socket.onclose = null
      socket.onerror = null
      socket.onopen = null
      socket.close()
      socket = null
    }
  }
}
