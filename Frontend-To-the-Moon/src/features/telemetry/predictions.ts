import type { VehicleView } from './fleet.ts'

export function predictionView(
  item: VehicleView,
  now: number,
  connected = true,
) {
  const p = item.vehicle.prediction
  const age = p ? Math.max(0, (now - Date.parse(p.T)) / 1000) : null
  const fresh =
    !!p &&
    age! <= 30 &&
    Date.parse(p.target_time_begin) > now &&
    !p.telemetry_stale &&
    item.vehicle.prediction_status !== 'ml_unavailable'
  const signal = connected && item.status === 'fresh'
  const tone = !signal
    ? 'offline'
    : !fresh
      ? 'pending'
      : p.risk_alert
        ? 'risk'
        : Math.abs(p.predicted_delay_s) > 60
          ? 'attention'
          : 'normal'
  const labels = {
    offline: 'Нет свежего сигнала',
    pending:
      item.vehicle.prediction_status === 'no_schedule'
        ? 'Нет расписания'
        : 'Ожидаем прогноз',
    risk: 'Риск задержки',
    attention:
      p && p.predicted_delay_s < 0
        ? 'Опережение графика'
        : 'Отклонение от графика',
    normal: 'Низкий риск задержки',
  }
  return { prediction: p, fresh, signal, tone, label: labels[tone], age }
}

export function delayLabel(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  const absolute = Math.abs(Math.round(seconds))
  const sign = seconds < 0 ? '−' : seconds > 0 ? '+' : ''
  return absolute < 60
    ? `${sign}${absolute} с`
    : `${sign}${Math.floor(absolute / 60)} мин ${absolute % 60} с`
}
