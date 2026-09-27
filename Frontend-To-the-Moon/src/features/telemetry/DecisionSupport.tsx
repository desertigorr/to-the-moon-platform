import { useState } from 'react'
import type { Prediction, Recommendation } from './contract'
import type { VehicleView } from './fleet'
import { delayLabel, predictionView } from './predictions'
import { reserveScenario, currentRecommendations } from './decisions'

const time = (value: number) =>
  new Date(value).toLocaleTimeString('ru-RU', { timeZone: 'Europe/Moscow' })

export function FactorPanel({ prediction }: { prediction: Prediction }) {
  const factors = prediction.factors ?? []
  const largest = Math.max(
    ...factors.map((f) => Math.abs(f.contribution)),
    0.001,
  )
  return (
    <section className="dispatch-explanation">
      <h4>Что повлияло на оценку риска</h4>
      <p className="dispatch-muted">
        Вклад признаков в этот прогноз классификатора. Причина задержки не
        установлена.
      </p>
      {factors.map((f) => (
        <div className="dispatch-factor" key={f.feature}>
          <div>
            <span>{f.label}</span>
            <strong>
              {f.value == null
                ? 'нет данных'
                : f.unit === 'доля'
                  ? `${Math.round(f.value * 100)}%`
                  : `${f.value.toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ${f.unit}`}
            </strong>
          </div>
          <div
            className={`dispatch-factor-impact ${f.direction === 'increases_risk' ? 'up' : 'down'}`}
          >
            <span>
              {f.direction === 'increases_risk'
                ? '↑ Повышает риск'
                : '↓ Снижает риск'}
            </span>
            <i
              style={{
                width: `${(Math.abs(f.contribution) / largest) * 100}%`,
              }}
            />
          </div>
        </div>
      ))}
      {!factors.length && <p>Вклады признаков недоступны для этой записи.</p>}
      <details>
        <summary>Как рассчитаны факторы</summary>
        <p>
          Tree SHAP для классификатора с учётом калибровки. Вклады измеряются в
          логарифме шансов, не в процентных пунктах. Показаны пять наибольших по
          модулю вкладов. Факторы не доказывают причинную связь и не объясняют
          регрессионную оценку секунд.
        </p>
      </details>
      <h4>Наблюдения к моменту прогноза</h4>
      {(prediction.observations ?? []).map((s) => (
        <p key={s}>{s}</p>
      ))}
    </section>
  )
}

export function RecommendationList({ items }: { items: Recommendation[] }) {
  return (
    <div className="dispatch-recommendations">
      {items.map((item) => (
        <article key={item.code} className={`priority-${item.priority}`}>
          <strong>{item.title}</strong>
          <p>{item.rationale}</p>
        </article>
      ))}
      <small>
        Рекомендации для проверки диспетчером. Команды водителю автоматически не
        отправляются.
      </small>
    </div>
  )
}

export default function DecisionSupport({
  item,
  vehicles,
  onSelect,
  at,
  connected,
}: {
  item: VehicleView | null
  vehicles: VehicleView[]
  onSelect: (id: number) => void
  at: number
  connected: boolean
}) {
  const [preparation, setPreparation] = useState('')
  const [travel, setTravel] = useState('')
  const state = item ? predictionView(item, at, connected) : null
  const p = state?.prediction
  const calculation =
    p && state?.fresh && state.signal && preparation.trim() && travel.trim()
      ? reserveScenario(
          at,
          p.target_time_begin,
          p.predicted_delay_s,
          Number(preparation),
          Number(travel),
        )
      : null
  return (
    <section className="dispatch-panel dispatch-decisions">
      <div className="dispatch-panel-heading">
        <h2>Поддержка решения диспетчера</h2>
        <select
          aria-label="ТС для рекомендации"
          value={item?.vehicle.unit_id ?? ''}
          onChange={(e) => onSelect(Number(e.target.value))}
        >
          {!item && <option value="">Нет ТС</option>}
          {vehicles.map((v) => (
            <option key={v.vehicle.unit_id} value={v.vehicle.unit_id}>
              {v.label}
            </option>
          ))}
        </select>
      </div>
      <div className="dispatch-decisions-grid">
        <div>
          <h3>Рекомендуемые действия</h3>
          {item ? (
            <RecommendationList
              items={currentRecommendations(item, at, connected)}
            />
          ) : (
            <p>Ожидаем телеметрию.</p>
          )}
          {p?.segment && (
            <p>
              Подход к целевой остановке:{' '}
              <strong>
                {p.segment.from_name} → {p.segment.to_name}
              </strong>
            </p>
          )}
        </div>
        <div className="dispatch-reserve">
          <h3>Сценарий прибытия резерва</h3>
          <p>
            Введите оценку готовности и времени пути доступного резервного ТС до
            целевой остановки.
          </p>
          <label>
            Подготовка и выпуск, мин
            <input
              type="number"
              min="0"
              max="120"
              step="0.5"
              placeholder="Например, 2"
              value={preparation}
              onChange={(e) => setPreparation(e.target.value)}
            />
          </label>
          <label>
            Путь до целевой остановки, мин
            <input
              type="number"
              min="0"
              max="240"
              step="0.5"
              placeholder="Например, 8"
              value={travel}
              onChange={(e) => setTravel(e.target.value)}
            />
          </label>
          {!p || !state?.fresh || !state.signal ? (
            <p role="status">
              Для сравнения нужен актуальный прогноз и свежий сигнал.
            </p>
          ) : calculation ? (
            <div className="dispatch-scenario-result" role="status">
              <p>
                Прибытие резерва: <strong>{time(calculation.reserveAt)}</strong>
              </p>
              <p>
                Отклонение резерва от плана:{' '}
                <strong>{delayLabel(calculation.reserveDeviationS)}</strong>
              </p>
              <p>
                Прогноз прибытия основного ТС:{' '}
                <strong>{time(calculation.busAt)}</strong>
              </p>
              <strong>
                {calculation.earlierByS > 0
                  ? `Резерв может прибыть раньше на ${delayLabel(calculation.earlierByS).replace('+', '')}`
                  : 'По введённым условиям резерв не прибудет раньше'}
              </strong>
            </div>
          ) : (
            <p role="status">
              Заполните оба поля: подготовка 0–120 мин, путь 0–240 мин.
            </p>
          )}
          <small>
            Расчёт времени по вашим допущениям, не новый ML-прогноз. Он не
            оценивает вместимость, пассажиропоток или влияние резерва на
            движение основного ТС. Выпуск согласует диспетчер.
          </small>
        </div>
      </div>
    </section>
  )
}
