import type { Network, Recommendation } from './contract.ts'
import type { VehicleView } from './fleet.ts'
import { predictionView } from './predictions.ts'

export function segmentTones(
  network: Network | null,
  vehicles: VehicleView[],
  at: number,
  connected: boolean,
) {
  const result = new Map<string, 'normal' | 'attention' | 'risk'>()
  const rank = { normal: 1, attention: 2, risk: 3 }
  for (const vehicle of vehicles) {
    const view = predictionView(vehicle, at, connected)
    const segment = view.prediction?.segment
    if (
      !view.fresh ||
      !view.signal ||
      !segment ||
      segment.graph_version !== network?.graph_version
    )
      continue
    const feature = network.features.find(
      (f) => f.properties.segment_id === segment.segment_id,
    )
    if (!feature || !['normal', 'attention', 'risk'].includes(view.tone))
      continue
    const key = feature.properties.physical_id ?? segment.segment_id
    const tone = view.tone as 'normal' | 'attention' | 'risk'
    const old = result.get(key)
    if (!old || rank[tone] > rank[old]) result.set(key, tone)
  }
  return result
}

export function reserveScenario(
  now: number,
  plannedAt: string,
  predictedDelay: number,
  preparationMin: number,
  travelMin: number,
) {
  if (
    ![now, predictedDelay, preparationMin, travelMin].every(Number.isFinite) ||
    preparationMin < 0 ||
    travelMin < 0 ||
    preparationMin > 120 ||
    travelMin > 240
  )
    return null
  const planned = Date.parse(plannedAt)
  if (!Number.isFinite(planned) || planned <= now) return null
  const reserveAt = now + (preparationMin + travelMin) * 60_000
  const busAt = planned + predictedDelay * 1000
  return {
    reserveAt,
    busAt,
    reserveDeviationS: (reserveAt - planned) / 1000,
    earlierByS: (busAt - reserveAt) / 1000,
  }
}

export function currentRecommendations(
  item: VehicleView,
  at: number,
  connected: boolean,
): Recommendation[] {
  const view = predictionView(item, at, connected)
  if (!view.signal)
    return [
      {
        code: 'restore_signal',
        title: 'Проверить связь с ТС',
        rationale:
          'Свежая позиция недоступна. Уточните положение через доступный канал связи.',
        priority: 'high',
        requires_dispatcher: true,
      },
    ]
  if (item.vehicle.prediction && !view.fresh)
    return [
      {
        code: 'refresh_forecast',
        title: 'Дождаться актуального прогноза',
        rationale:
          'Последний прогноз устарел. Действия по нему требуют повторной проверки.',
        priority: 'medium',
        requires_dispatcher: true,
      },
    ]
  return item.vehicle.recommendations ?? []
}
