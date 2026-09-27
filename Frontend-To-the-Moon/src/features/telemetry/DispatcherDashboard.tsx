import { useEffect, useMemo, useState } from 'react'
import {
  BusGlyph,
  BusIcon,
  MoonMark,
  SectionIcon,
} from '../../components/Icons'
import { TelemetryApi, apiUrl } from './client'
import type { HealthResponse, Network, RiskNotification } from './contract'
import { viewVehicle, type VehicleView } from './fleet'
import { parseMatchingGraph } from './matching'
import { parseRecording, type Recording } from './recording'
import { useTelemetry } from './use-telemetry'
import { delayLabel, predictionView } from './predictions'
import DecisionSupport, {
  FactorPanel,
  RecommendationList,
} from './DecisionSupport'
import { segmentTones, currentRecommendations } from './decisions'
import './dispatcher.css'

const clock = (value: number | string) =>
  new Date(value).toLocaleTimeString('ru-RU', {
    timeZone: 'Europe/Moscow',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
const age = (seconds: number | null) =>
  seconds === null
    ? '—'
    : seconds < 60
      ? `${Math.floor(seconds)} с`
      : `${Math.floor(seconds / 60)} мин`
const apiBase = import.meta.env.VITE_API_BASE_URL || window.location.origin
type Filter = 'all' | 'risk' | 'attention' | 'offline'

async function read(base: string, path: string, signal?: AbortSignal) {
  const response = await fetch(apiUrl(base, path), { signal })
  if (!response.ok) throw new Error(`Сервер ответил HTTP ${response.status}`)
  return response.json()
}

export default function DispatcherDashboard() {
  const [mode, setMode] = useState<'live' | 'replay'>('live')
  const [recording, setRecording] = useState<Recording | null>(null)
  const [network, setNetwork] = useState<Network | null>(null)
  const [recordedNetwork, setRecordedNetwork] = useState<Network | null>(null)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [alerts, setAlerts] = useState<RiskNotification[]>([])
  const [error, setError] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [routeId, setRouteId] = useState('all')
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const [showUnscheduled, setShowUnscheduled] = useState(false)
  const [section, setSection] = useState<'overview' | 'history' | 'scenarios'>(
    'overview',
  )
  const stream = useTelemetry(mode, recording, apiBase)
  const currentNetwork = mode === 'replay' ? recordedNetwork : network
  const graph = useMemo(
    () => (currentNetwork ? parseMatchingGraph(currentNetwork) : null),
    [currentNetwork],
  )
  const allViews = [...stream.fleet.vehicles.values()].map((v) =>
    viewVehicle(v, stream.at, health?.stale_after_s ?? 30, graph),
  )
  const scheduledIds = new Set(currentNetwork?.routes.map((r) => r.tr_id))
  const hasSchedule = (v: VehicleView) =>
    currentNetwork
      ? v.vehicle.tr_id !== null && scheduledIds.has(v.vehicle.tr_id)
      : v.vehicle.prediction_status !== 'no_schedule'
  const unscheduledCount = allViews.filter((v) => !hasSchedule(v)).length
  const views = allViews.filter((v) => showUnscheduled || hasSchedule(v))
  const connected = mode === 'replay' || stream.connection === 'connected'
  const state = (v: VehicleView) => predictionView(v, stream.at, connected)
  const selected =
    views.find((v) => v.vehicle.unit_id === selectedId) ??
    views.find((v) => v.vehicle.prediction && v.vehicle.position) ??
    views.find((v) => v.vehicle.position) ??
    views[0] ??
    null
  const counts = {
    all: views.length,
    risk: views.filter((v) => state(v).tone === 'risk').length,
    attention: views.filter((v) => state(v).tone === 'attention').length,
    offline: views.filter((v) => state(v).tone === 'offline').length,
  }
  const visible = views.filter(
    (v) =>
      (routeId === 'all' || `unit-${v.vehicle.unit_id}` === routeId) &&
      (filter === 'all' || state(v).tone === filter) &&
      `${v.vehicle.tr_id} ${v.vehicle.unit_id}`.includes(search.trim()),
  )

  function selectVehicle(unitId: number) {
    const repeated = selectedId === unitId
    setSelectedId(unitId)
    setRouteId(repeated ? `unit-${unitId}` : 'all')
    if (repeated) {
      setFilter('all')
      setSearch('')
    }
    setSection('overview')
  }

  useEffect(() => {
    if (mode !== 'live') return
    const controller = new AbortController()
    let busy = false
    const refresh = async () => {
      if (busy) return
      busy = true
      try {
        const [h, n, a] = await Promise.all([
          new TelemetryApi(apiBase).health(controller.signal),
          read(apiBase, '/api/network', controller.signal),
          read(apiBase, '/api/alerts', controller.signal),
        ])
        parseMatchingGraph(n)
        if (!Array.isArray(n.routes) || !Array.isArray(a))
          throw new Error('Некорректные данные схемы')
        if (!controller.signal.aborted) {
          setHealth(h)
          setNetwork(n)
          setAlerts(a)
          setError('')
        }
      } catch (e) {
        if (!controller.signal.aborted)
          setError(e instanceof Error ? e.message : 'Нет связи с сервером')
      } finally {
        busy = false
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [mode])

  async function openRecording(file?: File) {
    if (!file) return
    try {
      if (file.size > 100 * 1024 * 1024)
        throw new Error('Размер записи больше 100 МБ')
      const text = await file.text()
      const next = parseRecording(text, file.name)
      let n: Network | null = null
      try {
        n = JSON.parse(text).network ?? null
      } catch {
        n = null
      }
      if (n) parseMatchingGraph(n)
      setRecordedNetwork(n)
      setRecording(next)
      setMode('replay')
      setRouteId('all')
      setSection('overview')
      setError('')
      stream.seek(0)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось прочитать запись')
    }
  }

  async function downloadRecording() {
    try {
      const data = await read(apiBase, '/api/recording')
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(data)], { type: 'application/json' }),
      )
      const a = document.createElement('a')
      a.href = url
      a.download = 'to-the-moon-recording.json'
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      setError('Не удалось сохранить запись')
    }
  }

  return (
    <div className="dispatch-shell">
      <aside className="dispatch-sidebar">
        <div className="dispatch-brand">
          <span>
            <MoonMark />
          </span>
          <div>
            To the Moon<small>Городской транспорт</small>
          </div>
        </div>
        <nav aria-label="Разделы">
          {(['overview', 'history', 'scenarios'] as const).map((name) => (
            <button
              key={name}
              className={section === name ? 'active' : ''}
              onClick={() => setSection(name)}
            >
              <SectionIcon name={name} />
              {
                {
                  overview: 'Обзор',
                  history: 'История',
                  scenarios: 'Рекомендации',
                }[name]
              }
            </button>
          ))}
        </nav>
        <div className="dispatch-routes">
          <small>Транспорт под наблюдением</small>
          <button
            className={routeId === 'all' ? 'active' : ''}
            onClick={() => {
              setRouteId('all')
              setSection('overview')
            }}
          >
            Все ТС <span>{views.length}</span>
          </button>
          {views.map((v) => (
            <button
              key={v.vehicle.unit_id}
              className={
                routeId === `unit-${v.vehicle.unit_id}` ? 'active' : ''
              }
              onClick={() => {
                setFilter('all')
                setSearch('')
                selectVehicle(v.vehicle.unit_id)
              }}
            >
              <BusIcon />
              {v.label}
            </button>
          ))}
        </div>
        <div className="dispatch-sidebar-footer">
          <i className={connected ? 'online-dot' : 'offline-dot'} />
          {mode === 'replay'
            ? 'Просмотр записи'
            : connected
              ? 'Соединение установлено'
              : 'Ожидаем подключение'}
        </div>
      </aside>
      <main className="dispatch-main">
        <header className="dispatch-header">
          <div>
            <h1>Дашборд</h1>
            <p>Прогноз изменений в графике транспорта</p>
          </div>
          <div className="dispatch-clock">
            <strong>{clock(stream.at)}</strong>
            <small>МСК</small>
            <span className="dispatch-tag">
              {mode === 'replay'
                ? 'Запись'
                : health?.source?.mode === 'replay'
                  ? 'Воспроизведение NDTP'
                  : health?.source?.mode.startsWith('emulator')
                    ? 'Эмулятор NDTP'
                    : 'Онлайн'}
            </span>
          </div>
        </header>
        <div className="dispatch-source">
          <span className="dispatch-source-label">
            {mode === 'replay'
              ? 'ЗАПИСЬ'
              : health?.source?.mode === 'replay'
                ? 'REPLAY'
                : 'NDTP'}
          </span>
          <div>
            <strong>
              {mode === 'replay'
                ? recording?.name
                : (
                    health?.source?.label ?? 'Подключение к потоку телеметрии'
                  ).replaceAll('\u00b7', '—')}
            </strong>
            <small>
              {mode === 'replay'
                ? 'Состояния, координаты и прогнозы из сохранённой записи'
                : health?.source?.mode === 'emulator'
                  ? `Автогенерация — ${health.emulator?.active_units ?? 0} устройств — интервал ${(health.emulator?.interval_ms ?? 5000) / 1000} с`
                  : health?.source?.mode === 'emulator_replay'
                    ? `Траектории из validate — ${health.emulator?.configured_units ?? 0} устройств в фрагменте — скорость 1× — время сдвинуто к текущему`
                    : 'Координаты поступают по NDTP — прогнозы рассчитывают модели CatBoost'}
            </small>
          </div>
          <span>Горизонт 10–15 мин</span>
        </div>
        {mode === 'live' &&
          health?.schedule?.status !== 'loaded' &&
          health?.schedule && (
            <div className="dispatch-notice" role="status">
              <strong>
                {health.schedule.status === 'expired'
                  ? 'Расписание завершилось'
                  : 'Расписание не подключено'}
              </strong>
              <span>
                Координаты поступают в реальном времени. Для прогноза задержки
                нужен план остановок этого потока. Автогенерация эмулятора не
                содержит расписания.
              </span>
            </div>
          )}
        {mode === 'live' && health?.emulator?.status === 'unavailable' && (
          <div className="dispatch-error" role="alert">
            Эмулятор недоступен. Показаны последние полученные позиции;
            соединение восстанавливается автоматически.
          </div>
        )}
        {mode === 'live' && health?.ml_status === 'unavailable' && (
          <div className="dispatch-error" role="alert">
            ML-сервис недоступен. Телеметрия продолжает поступать; прогнозы
            возобновятся после восстановления связи с моделями.
          </div>
        )}
        {(error || stream.error) && (
          <div className="dispatch-error" role="alert">
            {error || stream.error}
            <button onClick={stream.reconnect}>Переподключить</button>
          </div>
        )}
        <div className="dispatch-stats">
          {(
            [
              {
                id: 'all',
                label: 'Транспорт под наблюдением',
                detail: showUnscheduled
                  ? 'Включая ТС без расписания'
                  : 'ТС с плановым расписанием',
              },
              {
                id: 'risk',
                label: 'Риск задержки',
                detail: `Актуальных прогнозов: ${views.filter((v) => state(v).fresh).length}`,
              },
              {
                id: 'attention',
                label: 'Требуют внимания',
                detail: 'Отклонение от графика',
              },
              {
                id: 'offline',
                label: 'Нет свежих данных',
                detail: 'Показана последняя позиция',
              },
            ] as const
          ).map((s) => (
            <button
              key={s.id}
              className={`dispatch-stat tone-${s.id}`}
              onClick={() => {
                setFilter(s.id)
                if (s.id === 'all') setRouteId('all')
                setSection('overview')
              }}
            >
              <span>
                <i />
                {s.label}
              </span>
              <strong>
                {counts[s.id]} <small>ТС</small>
              </strong>
              <small>{s.detail}</small>
            </button>
          ))}
        </div>
        {section === 'scenarios' ? (
          <DecisionSupport
            item={selected}
            vehicles={views}
            onSelect={setSelectedId}
            at={stream.at}
            connected={connected}
          />
        ) : (
          <>
            <div className="dispatch-toolbar">
              <div className="dispatch-filters">
                {(['all', 'risk', 'attention', 'offline'] as const).map((f) => (
                  <button
                    className={filter === f ? 'active' : ''}
                    key={f}
                    onClick={() => {
                      setFilter(f)
                      if (f === 'all') setRouteId('all')
                    }}
                  >
                    {
                      {
                        all: 'Все ТС',
                        risk: 'Риск задержки',
                        attention: 'Внимание',
                        offline: 'Нет связи',
                      }[f]
                    }{' '}
                    <span>{counts[f]}</span>
                  </button>
                ))}
              </div>
              <input
                aria-label="Поиск транспорта"
                placeholder="Номер ТС или устройства"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <div className="dispatch-scope">
              <label>
                <input
                  type="checkbox"
                  checked={showUnscheduled}
                  onChange={(e) => {
                    setShowUnscheduled(e.target.checked)
                    setRouteId('all')
                  }}
                />
                Показать ТС без расписания ({unscheduledCount})
              </label>
              <small>
                {showUnscheduled
                  ? 'Отображаются все наблюдаемые ТС'
                  : 'На схеме и в списке — ТС с расписанием'}
              </small>
            </div>
            <div className="dispatch-workspace">
              <div className="dispatch-primary">
                {section === 'overview' && (
                  <RouteDiagram
                    key={routeId}
                    vehicles={visible}
                    selected={selected?.vehicle.unit_id ?? null}
                    onSelect={selectVehicle}
                    network={currentNetwork}
                    routeId={
                      routeId === 'all'
                        ? 'all'
                        : `schedule-${visible[0]?.vehicle.tr_id}`
                    }
                    at={stream.at}
                    connected={connected}
                  />
                )}
                <div className="dispatch-panel dispatch-playback">
                  <div>
                    <strong>
                      {mode === 'live'
                        ? 'Непрерывный поток'
                        : 'Просмотр записи'}
                    </strong>
                    <small>
                      {mode === 'live'
                        ? `ML каждые ${health?.ml_interval_s ?? 5} с — ${health?.ml_status === 'ready' ? 'модели подключены' : 'ожидание моделей'}${health?.source?.mode === 'emulator_replay' ? ` — цикл ${health.source.cycle ?? 1}, ${age(health.source.elapsed_s ?? 0)} / ${age(health.source.duration_s ?? 0)}` : ''}`
                        : `${age(stream.offset)} / ${age(stream.duration)}`}
                    </small>
                  </div>
                  {mode === 'replay' && (
                    <>
                      <button
                        aria-label={
                          stream.playing
                            ? 'Пауза записи'
                            : 'Воспроизвести запись'
                        }
                        onClick={stream.play}
                      >
                        {stream.playing ? 'Ⅱ' : '▶'}
                      </button>
                      <input
                        aria-label="Время записи"
                        type="range"
                        min={0}
                        max={stream.duration}
                        step={1}
                        value={stream.offset}
                        onChange={(e) => stream.seek(Number(e.target.value))}
                      />
                      <select
                        aria-label="Скорость записи"
                        value={stream.speed}
                        onChange={(e) =>
                          stream.setSpeed(Number(e.target.value))
                        }
                      >
                        {[1, 2, 5, 10].map((s) => (
                          <option value={s} key={s}>
                            {s}×
                          </option>
                        ))}
                      </select>
                      <button onClick={() => setMode('live')}>К потоку</button>
                    </>
                  )}
                  {mode === 'live' && (
                    <button onClick={() => void downloadRecording()}>
                      Сохранить запись
                    </button>
                  )}
                  <label className="dispatch-upload">
                    Открыть запись
                    <input
                      type="file"
                      accept=".json,.jsonl,.ndjson"
                      onChange={(e) => {
                        void openRecording(e.target.files?.[0])
                        e.target.value = ''
                      }}
                    />
                  </label>
                </div>
                <section className="dispatch-panel">
                  <div className="dispatch-panel-heading">
                    <h2>Рейсы под наблюдением</h2>
                    <small>Выберите транспорт, чтобы открыть карточку</small>
                  </div>
                  <div className="dispatch-table-head">
                    <span>Транспорт</span>
                    <span>Ситуация</span>
                    <span>Прогноз</span>
                    <span>Телеметрия</span>
                  </div>
                  {!visible.length && (
                    <div className="dispatch-empty">
                      <BusIcon />
                      <p>
                        {views.length
                          ? 'Нет транспорта с выбранным фильтром'
                          : allViews.length
                            ? 'Нет ТС с расписанием. Включите показ транспорта без расписания.'
                            : 'Ожидаем первые пакеты NDTP'}
                      </p>
                    </div>
                  )}
                  {visible.map((v) => {
                    const p = state(v)
                    return (
                      <button
                        key={v.vehicle.unit_id}
                        className={`dispatch-row ${v.vehicle.unit_id === selected?.vehicle.unit_id ? 'selected' : ''}`}
                        onClick={() => selectVehicle(v.vehicle.unit_id)}
                      >
                        <span className="dispatch-bus-id">
                          <BusIcon />
                          <span>
                            <strong>
                              {v.vehicle.tr_id ?? v.vehicle.unit_id}
                            </strong>
                            <small>Устройство {v.vehicle.unit_id}</small>
                          </span>
                        </span>
                        <span className={`dispatch-situation tone-${p.tone}`}>
                          <i />
                          {p.label}
                        </span>
                        <span className={`tone-${p.tone}`}>
                          {p.fresh
                            ? delayLabel(p.prediction?.predicted_delay_s)
                            : 'Нет актуального прогноза'}
                        </span>
                        <span className="dispatch-row-age">
                          {age(v.positionAge)} назад <b>›</b>
                        </span>
                      </button>
                    )
                  })}
                </section>
                <section className="dispatch-panel dispatch-alerts">
                  <div className="dispatch-panel-heading">
                    <h2>
                      {section === 'history'
                        ? 'История предупреждений'
                        : 'Предупреждения диспетчеру'}
                    </h2>
                    <small>
                      {mode === 'replay'
                        ? 'На выбранный момент записи'
                        : 'Последние события'}
                    </small>
                  </div>
                  {mode === 'replay'
                    ? visible
                        .filter((v) => state(v).tone === 'risk')
                        .map((v) => (
                          <div
                            className="dispatch-alert"
                            key={v.vehicle.unit_id}
                          >
                            <i />
                            <div>
                              <strong>
                                ТС {v.vehicle.tr_id} — риск задержки
                              </strong>
                              <p>
                                Прогноз{' '}
                                {delayLabel(
                                  v.vehicle.prediction?.predicted_delay_s,
                                )}{' '}
                                — вероятность{' '}
                                {Math.round(
                                  (v.vehicle.prediction?.late_probability ??
                                    0) * 100,
                                )}
                                %
                              </p>
                            </div>
                          </div>
                        ))
                    : alerts
                        .slice(0, section === 'history' ? 100 : 5)
                        .map((a) => (
                          <button
                            key={a.id}
                            className="dispatch-alert"
                            onClick={() => {
                              setSelectedId(a.unit_id)
                              setSection('overview')
                            }}
                          >
                            <i />
                            <div>
                              <strong>
                                ТС {a.tr_id} —{' '}
                                {a.early_risk_alert
                                  ? 'раннее предупреждение'
                                  : 'риск задержки'}
                              </strong>
                              <p>
                                {a.target_name} —{' '}
                                {delayLabel(a.predicted_delay_s)} — вероятность{' '}
                                {Math.round(a.late_probability * 100)}%
                              </p>
                              {a.segment && (
                                <p>
                                  Подход к цели: {a.segment.from_name} →{' '}
                                  {a.segment.to_name}
                                </p>
                              )}
                              {a.forecast_at && (
                                <small>
                                  Горизонт при расчёте:{' '}
                                  {Math.round(
                                    (Date.parse(a.target_time_begin) -
                                      Date.parse(a.forecast_at)) /
                                      60000,
                                  )}{' '}
                                  мин
                                </small>
                              )}
                              {a.explanations.slice(0, 2).map((e) => (
                                <small key={e}>{e}</small>
                              ))}
                              {!!a.explanations.length && (
                                <small>
                                  Факторы оценки модели; причина задержки не
                                  установлена.
                                </small>
                              )}
                              {a.recommendations?.[0] && (
                                <p>Действие: {a.recommendations[0].title}</p>
                              )}
                            </div>
                            <time>{clock(a.at)}</time>
                          </button>
                        ))}
                  {((mode === 'live' && !alerts.length) ||
                    (mode === 'replay' &&
                      !visible.some((v) => state(v).tone === 'risk'))) && (
                    <div className="dispatch-empty compact">
                      Предупреждений пока нет
                    </div>
                  )}
                </section>
              </div>
              <VehicleCard
                item={selected}
                network={currentNetwork}
                at={stream.at}
                connected={connected}
              />
            </div>
          </>
        )}
        <footer className="dispatch-footer">
          {currentNetwork?.routes.length
            ? 'Геометрия по историческому GPS — пунктир — плановая связь — прибытия оцениваются по GPS'
            : 'Положение устройств по исходному GPS — маршруты появятся при подключении расписания'}
        </footer>
      </main>
    </div>
  )
}

function RouteDiagram({
  vehicles,
  selected,
  onSelect,
  network,
  routeId,
  at,
  connected,
}: {
  vehicles: VehicleView[]
  selected: number | null
  onSelect: (id: number) => void
  network: Network | null
  routeId: string
  at: number
  connected: boolean
}) {
  const [showRaw, setShowRaw] = useState(false)
  const [zoom, setZoom] = useState(1)
  const [focus, setFocus] = useState<number[] | null>(null)
  const routes =
    network?.routes.filter((r) => routeId === 'all' || r.id === routeId) ?? []
  const trIds = new Set(routes.map((r) => r.tr_id))
  const segments =
    network?.features.filter((f) => trIds.has(f.properties.tr_id)) ?? []
  const tones = segmentTones(network, vehicles, at, connected)

  const coords = [
    ...routes.flatMap((r) => r.stops.map((s) => [s.lon, s.lat])),
    ...segments.flatMap((s) => s.geometry.coordinates),
  ]
  if (!coords.length) coords.push([37.48, 55.68], [37.63, 55.83])
  const minX = Math.min(...coords.map((c) => c[0])),
    maxX = Math.max(...coords.map((c) => c[0]))
  const minY = Math.min(...coords.map((c) => c[1])),
    maxY = Math.max(...coords.map((c) => c[1]))
  const factor = Math.cos((((minY + maxY) / 2) * Math.PI) / 180)
  const scale =
    zoom *
    Math.min(
      760 / Math.max(0.001, (maxX - minX) * factor),
      370 / Math.max(0.001, maxY - minY),
    )
  const xy = (lon: number, lat: number) => [
    470 + (lon - (focus?.[0] ?? (minX + maxX) / 2)) * factor * scale,
    260 - (lat - (focus?.[1] ?? (minY + maxY) / 2)) * scale,
  ]
  const selectedTr = vehicles.find((v) => v.vehicle.unit_id === selected)
    ?.vehicle.tr_id
  const names = new Set<string>()
  const stops = routes
    .flatMap((r) => r.stops)
    .filter((s) => {
      const key = `${s.lon.toFixed(5)},${s.lat.toFixed(5)}`
      if (names.has(key)) return false
      names.add(key)
      return true
    })
  return (
    <section className="dispatch-panel">
      <div className="dispatch-panel-heading">
        <div className="dispatch-title-row">
          <h2>{routes.length ? 'Схема маршрутов' : 'Положение транспорта'}</h2>
          <span className="dispatch-tag">{vehicles.length} ТС</span>
        </div>
        <label className="dispatch-raw">
          <input
            type="checkbox"
            checked={showRaw}
            onChange={(e) => setShowRaw(e.target.checked)}
          />
          Исходный GPS
        </label>
      </div>
      <div className="dispatch-map-controls">
        <button
          aria-label="Приблизить схему"
          onClick={() => setZoom(Math.min(16, zoom * 1.5))}
        >
          +
        </button>
        <button
          aria-label="Отдалить схему"
          onClick={() => setZoom(Math.max(0.125, zoom / 1.5))}
        >
          −
        </button>
        <button
          onClick={() => {
            setFocus(null)
            setZoom(1)
          }}
        >
          Общий вид
        </button>
        <button
          onClick={() => {
            const p = vehicles.find((v) => v.vehicle.unit_id === selected)
              ?.vehicle.position
            if (p?.lon != null && p.lat != null) {
              setFocus([p.lon, p.lat])
              setZoom(Math.max(zoom, 3))
            }
          }}
        >
          К выбранному ТС
        </button>
      </div>
      <div className="dispatch-diagram">
        <svg
          viewBox="0 0 940 520"
          aria-label="Схема плановых остановок и положения автобусов"
          role="img"
        >
          <defs>
            <pattern
              id="dispatch-grid"
              width="40"
              height="40"
              patternUnits="userSpaceOnUse"
            >
              <path
                d="M40 0H0V40"
                fill="none"
                stroke="var(--grid-line)"
                strokeWidth=".6"
              />
            </pattern>
          </defs>
          <rect width="940" height="520" fill="url(#dispatch-grid)" />
          {segments.map((s) => {
            const tone = tones.get(
              s.properties.physical_id ?? s.properties.segment_id,
            )
            const color = tone
              ? {
                  risk: 'var(--risk)',
                  attention: 'var(--attention)',
                  normal: 'var(--normal)',
                }[tone]
              : 'var(--route-line)'
            const c = s.geometry.coordinates
              .map((p) => xy(p[0], p[1]).join(','))
              .join(' ')
            return (
              <polyline
                key={s.properties.segment_id}
                points={c}
                fill="none"
                stroke={color}
                strokeWidth={
                  tone ? 7 : s.properties.tr_id === selectedTr ? 4 : 2
                }
                strokeDasharray={
                  s.properties.geometry_source === 'historical_gps'
                    ? undefined
                    : '6 5'
                }
                data-segment={s.properties.segment_id}
                data-risk={tone ?? 'unknown'}
                strokeLinecap="round"
                strokeLinejoin="round"
                opacity={0.8}
              >
                <title>
                  {s.properties.from_name} → {s.properties.to_name} —{' '}
                  {tone === 'risk'
                    ? 'Высокий риск на подходе к цели'
                    : tone === 'attention'
                      ? 'Требует внимания'
                      : tone === 'normal'
                        ? 'Низкий риск на подходе к цели'
                        : 'Нет актуального прогноза'}{' '}
                  —{' '}
                  {s.properties.geometry_source === 'historical_gps'
                    ? 'Историческая GPS-траектория'
                    : 'Схематичная связь'}
                </title>
              </polyline>
            )
          })}
          {stops.map((s, i) => {
            const [x, y] = xy(s.lon, s.lat)
            return (
              <g key={s.id}>
                <circle
                  cx={x}
                  cy={y}
                  r={4}
                  fill="var(--background)"
                  stroke="var(--route-stop)"
                  strokeWidth={1.8}
                />
                <title>{s.name}</title>
                {routes.length === 1 &&
                  i % Math.max(1, Math.ceil(stops.length / 7)) === 0 && (
                    <text x={x + 12} y={y - 14} className="dispatch-stop-name">
                      {s.name.length > 30 ? `${s.name.slice(0, 29)}…` : s.name}
                    </text>
                  )}
              </g>
            )
          })}
          {vehicles
            .filter((v) => v.vehicle.position)
            .sort(
              (a, b) =>
                Number(a.vehicle.unit_id === selected) -
                Number(b.vehicle.unit_id === selected),
            )
            .map((v) => {
              const raw = v.vehicle.position!
              const pos = showRaw ? raw : (v.matching.position ?? raw)
              const [x, y] = xy(pos.lon!, pos.lat!)
              const p = predictionView(v, at, connected)
              return (
                <g
                  key={v.vehicle.unit_id}
                  className={`dispatch-marker tone-${p.tone} ${selected === v.vehicle.unit_id ? 'selected' : ''}`}
                  transform={`translate(${x} ${y})`}
                  data-unit={v.vehicle.unit_id}
                  data-status={p.tone}
                  role="button"
                  tabIndex={0}
                  aria-label={`${v.label}: ${p.label}`}
                  onClick={() => onSelect(v.vehicle.unit_id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      onSelect(v.vehicle.unit_id)
                    }
                  }}
                >
                  <title>
                    {v.label}: {p.label}
                  </title>
                  {vehicles.length > 12 && selected !== v.vehicle.unit_id ? (
                    <circle
                      r="9"
                      fill="var(--surface-raised)"
                      stroke="currentColor"
                      strokeWidth="3"
                    />
                  ) : (
                    <>
                      <rect
                        className="dispatch-marker-plate"
                        x="-58"
                        y="-19"
                        width="116"
                        height="38"
                        rx="10"
                      />
                      <g
                        className="dispatch-marker-icon"
                        transform="translate(-51 -13) scale(1.08)"
                      >
                        <BusGlyph />
                      </g>
                      <text x="11" y="5">
                        {v.vehicle.tr_id ?? v.vehicle.unit_id}
                      </text>
                    </>
                  )}
                  <title>
                    {v.label} — {p.label} — GPS {raw.lat?.toFixed(5)},{' '}
                    {raw.lon?.toFixed(5)}
                  </title>
                </g>
              )
            })}
        </svg>
        {!vehicles.length && (
          <div className="dispatch-diagram-empty">
            {network
              ? 'Нет транспорта с выбранным фильтром'
              : 'Ожидаем схему и телеметрию'}
          </div>
        )}
        <div className="dispatch-diagram-caption">
          {routes.length
            ? 'Цвет участка — риск на подходе к целевой остановке. Пунктир — нет исторической траектории.'
            : 'Исходные GPS-позиции'}
        </div>
      </div>
      <div className="dispatch-legend">
        {(['risk', 'attention', 'normal', 'pending', 'offline'] as const).map(
          (t) => (
            <span className={`tone-${t}`} key={t}>
              <i />
              {
                {
                  risk: 'Риск задержки',
                  attention: 'Внимание',
                  normal: 'Низкий риск',
                  pending: 'Нет прогноза',
                  offline: 'Нет сигнала',
                }[t]
              }
            </span>
          ),
        )}
      </div>
    </section>
  )
}

function VehicleCard({
  item,
  network,
  at,
  connected,
}: {
  item: VehicleView | null
  network: Network | null
  at: number
  connected: boolean
}) {
  if (!item)
    return (
      <aside className="dispatch-panel dispatch-card">
        <div className="dispatch-panel-heading">
          <h2>Карточка рейса</h2>
        </div>
        <div className="dispatch-empty">Выберите транспорт на схеме</div>
      </aside>
    )
  const v = item.vehicle,
    state = predictionView(item, at, connected),
    p = state.prediction
  const stop = network?.routes
    .flatMap((r) => r.stops)
    .find((s) => s.id === p?.target_stop_id)
  const reasons: Record<string, string> = {
    no_schedule: 'Для этого ТС не подключено плановое расписание',
    no_target: 'Нет планового прибытия в окне 10–15 минут',
    missing_vehicle_mapping:
      'Устройство ещё не связано с транспортным средством',
    ml_unavailable: 'Нет связи с ML-сервисом',
    calculating: 'Рассчитываем прогноз',
    not_connected: 'ML пока не подключён',
    ambiguous_vehicle_mapping: 'Несколько устройств связаны с одним ТС',
  }
  return (
    <aside className="dispatch-panel dispatch-card">
      <div className="dispatch-panel-heading">
        <h2>Карточка рейса</h2>
        <small>ТС {v.tr_id ?? '—'}</small>
      </div>
      <div className="dispatch-card-body">
        <div className="dispatch-card-title">
          <span className={`dispatch-avatar tone-${state.tone}`}>
            <BusIcon />
          </span>
          <div>
            <h3>{item.label}</h3>
            <small>{state.label}</small>
          </div>
        </div>
        <div className={`dispatch-prediction tone-${state.tone}`}>
          <small>
            {p
              ? `Прогноз на ${clock(p.target_time_begin).slice(0, 5)}`
              : 'Прогноз задержки'}
          </small>
          <strong>
            {p
              ? delayLabel(p.predicted_delay_s)
              : v.prediction_status === 'no_schedule'
                ? 'Нет расписания'
                : 'Ожидаем данные'}
          </strong>
          <span>
            {p
              ? state.fresh
                ? 'Отклонение на целевой остановке'
                : 'Последний прогноз — не актуален'
              : (reasons[v.prediction_status ?? 'not_connected'] ??
                'Нет актуального прогноза')}
          </span>
        </div>
        {p && (
          <div className="dispatch-probability">
            <div>
              <span>
                Вероятность опоздания &gt; {p.delay_threshold_s / 60} мин
              </span>
              <strong>{Math.round(p.late_probability * 100)}%</strong>
            </div>
            <div className="dispatch-meter">
              <i style={{ width: `${p.late_probability * 100}%` }} />
            </div>
            <small>
              {p.early_risk_alert
                ? 'Раннее предупреждение'
                : 'Оценка на целевое прибытие'}{' '}
              — порог {Math.round(p.alert_threshold * 100)}%
            </small>
          </div>
        )}
        <dl className="dispatch-details">
          {p?.segment && (
            <div className="wide">
              <dt>Прогнозируемый участок подхода к цели</dt>
              <dd>
                {p.segment.from_name} → {p.segment.to_name}
              </dd>
            </div>
          )}
          {p && (
            <div className="wide">
              <dt>Горизонт при расчёте</dt>
              <dd>
                {(
                  (Date.parse(p.target_time_begin) - Date.parse(p.T)) /
                  60000
                ).toFixed(1)}{' '}
                мин
              </dd>
            </div>
          )}
          <div className="wide">
            <dt>Целевая остановка</dt>
            <dd>{stop?.name ?? '—'}</dd>
          </div>
          <div>
            <dt>По расписанию</dt>
            <dd>{p ? clock(p.target_time_begin).slice(0, 5) : '—'}</dd>
          </div>
          <div>
            <dt>Ожидаемое прибытие</dt>
            <dd>
              {p
                ? clock(
                    Date.parse(p.target_time_begin) +
                      p.predicted_delay_s * 1000,
                  ).slice(0, 5)
                : '—'}
            </dd>
          </div>
          <div>
            <dt>Текущее отклонение</dt>
            <dd>{delayLabel(v.arrival?.cur_dev_s)}</dd>
          </div>
          <div>
            <dt>Скорость</dt>
            <dd>{v.latest.speed == null ? '—' : `${v.latest.speed} км/ч`}</dd>
          </div>
          <div className="wide">
            <dt>Последнее прибытие по GPS</dt>
            <dd>
              {v.arrival
                ? `${clock(v.arrival.event_time)} — оценка прибытия`
                : 'Прибытия ещё не подтверждены'}
            </dd>
          </div>
        </dl>
        <div className="dispatch-explanation">
          <h4>Рекомендуемые действия</h4>
          <RecommendationList
            items={currentRecommendations(item, at, connected)}
          />
        </div>
        {p && <FactorPanel prediction={p} />}
        <div className="dispatch-card-status">
          <i className={state.signal ? 'online-dot' : 'offline-dot'} />
          <div>
            Координаты {age(item.positionAge)} назад
            <small>
              {!state.signal
                ? 'Сохранена последняя достоверная позиция'
                : item.matching.applied
                  ? 'Позиция привязана к плановому участку'
                  : item.matching.state === 'unmatched'
                    ? 'Позиция не привязана к маршруту — исходный GPS'
                    : item.matching.state === 'ambiguous'
                      ? 'Неоднозначная привязка — исходный GPS'
                      : 'Показана исходная GPS-позиция'}
            </small>
          </div>
        </div>
        <details className="dispatch-technical">
          <summary>Данные измерения</summary>
          <p>Устройство: {v.unit_id}</p>
          <p>
            GPS:{' '}
            {v.position
              ? `${v.position.lat?.toFixed(6)}, ${v.position.lon?.toFixed(6)}`
              : 'Нет координат'}
          </p>
          <p>Привязка: {item.matching.label}</p>
          <p>Точек в истории: {v.history_count}</p>
          <p>Прогноз: {p ? `${clock(p.T)}, ${age(state.age)} назад` : '—'}</p>
        </details>
      </div>
    </aside>
  )
}
