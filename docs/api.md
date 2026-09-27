# API 1.0

Backend: http://localhost:8000. Через frontend доступны те же пути `/api/*` на порту 8080. Интерактивная спецификация `/docs`, JSON `/openapi.json`. Сохранённые контракты: openapi.json и ml-openapi.json. Сгенерированный PyDoc: `/documentation/`.

| Метод и путь | Назначение |
|---|---|
| GET /api/health | TCP, счётчики, готовность моделей, расписание, источник, цикл |
| GET /api/vehicles | Все ТС, включая контекстные без расписания |
| GET /api/vehicles/{unit_id} | Последняя телеметрия, позиция, привязка, прогноз, рекомендации |
| GET /api/vehicles/{unit_id}/history?limit=300&until=… | Ограниченная история; until требует часового пояса |
| GET /api/network | GeoJSON, плановые остановки, геометрия, риски участков |
| GET /api/alerts | До 100 последних предупреждений, новые первыми |
| GET /api/replay | Источник, сдвиг времени, цикл и длительность сценария |
| GET /api/recording | До десяти минут snapshot с геометрией для локального просмотра |
| WS /api/stream | Начальный snapshot, затем telemetry и snapshot-обновления |

## Телеметрия и состояние

`unit_id` — устройство, `tr_id` — ТС. NDTP навигация не содержит номера остановки: target_stop_id берётся из расписания и равен tt_action_item_id планового прибытия. `packet_id` передаётся строкой, чтобы JavaScript не потерял int64-точность. Время — ISO 8601 с зоной, числа — конечные, отсутствие — null.

Vehicle содержит `latest`, `position` (последний достоверный исходный GPS), `map_match`, `prediction`, `prediction_status`, `arrival`, `recommendations`. Invalid GPS не заменяет position нулевыми координатами. При stale frontend замораживает последнее положение.

`arrival` — оценка по GPS: stop_id, event_time, received_at, cur_dev_s, method=gps_estimate, valid_until. Последнее поле ограничивает использование оценки пятью минутами от события. После истечения backend возвращает arrival=null, даже если новых пакетов не было.

`map_match.status`: matched / ambiguous / unmatched; null означает, что расчёта нет. Только matched содержит привязанную точку и участок. Проверять `source_packet_id == position.packet_id` и совпадение graph_version с сетью. route_id — локальная нитка расписания; direction — направление участка.

WebSocket сначала выдаёт `{type:"snapshot", sequence, vehicles:[…]}`; новая телеметрия — `{type:"telemetry", sequence, data, vehicle}`. Обогащение привязкой/ML и heartbeat идут snapshot. На reconnect сервер присылает полный снимок. sequence монотонен в процессе; новая сессия соединения допускает сброс после рестарта backend. CORS и WebSocket Origins ограничены конфигурацией.

## Прогноз

`predicted_delay_s` — знаковое отклонение на целевой остановке; минус означает опережение. `late_probability` — вероятность задержки >delay_threshold_s (120). `risk_alert` сравнивает её с порогом из модели; `early_risk_alert` дополнительно требует известного cur_dev_s ≤120. `T`, `target_time_begin`, `generated_at` разделяют момент прогноза, план и генерацию.

`factors`: feature, label, value, unit, contribution, direction, method=tree_shap, scale=calibrated_log_odds. `risk_baseline_log_odds + сумма contribution + risk_other_contribution` равны logit вероятности. `explanations` — текстовые описания факторов; `observations` — измеренные признаки. Никакая из этих строк не устанавливает причину транспортного события.

`segment`: segment_id, graph_version, from_stop_id, to_stop_id, from_name, to_name, basis=target_approach, geometry_source. Это плановый подход к целевой остановке, не утверждение о текущей позиции или месте возникновения сбоя.

`recommendations`: code, title, rationale, priority, requires_dispatcher. У Vehicle список пересчитывается с учётом свежести данных; в prediction и alert сохранён контекст расчёта. Устаревшие рекомендации нельзя выдавать за актуальные.

## Сеть и уведомления

GeoJSON LineString содержит segment_id планового перехода и physical_id направленной пары координат. `geometry_source=historical_gps` — восстановленная траектория, `schedule_chord` — схематичная связь. `risk_level=unknown|normal|attention|high` агрегируется по физическому участку; список risk_vehicles и максимальная late_probability отражают только свежие прогнозы. Без них unknown, не зелёный.

Alert содержит ТС, цель, секунды задержки, вероятность, early flag, время прогноза, участок, объяснения, наблюдения и рекомендации. Уведомления повторно не создаются на каждом ML-тике; повтор одного ТС/цели ограничен 60 секундами. Это история предупреждений, не список текущих активных рисков.

## ML API внутри Docker

`GET http://ml:8001/health`: готовность двух моделей и объяснений.

`POST http://ml:8001/predict`: request_id, points, traffic_buffer, schedule_as_of_T. points — одна строка на ТС с sample_id, tr_id, T, cur_dev_s, target_stop_id, target_time_begin. traffic_buffer — tr_id, event_time, receive_time, location_valid, lat, lon, speed, heading. schedule_as_of_T — tt_action_item_id, tr_id, time_begin, geom. Никаких labels или time_fact_begin.

Ответ: request_id, predictions, elapsed_ms. Признаки считаются один раз на batch; одновременный второй запрос получает HTTP 429. Некорректный контракт — 422. Backend проверяет весь ответ и идентификаторы перед применением. Сеть и рекомендации обогащаются на backend, не внутри моделей.
