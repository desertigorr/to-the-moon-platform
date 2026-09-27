"""Dispatcher decision support. Rules suggest checks, never issue driving commands."""

from shared.contracts import Recommendation


def recommendations(vehicle, now):
    def item(code, title, rationale, priority='medium'):
        return Recommendation(code=code, title=title, rationale=rationale, priority=priority)
    if vehicle.is_stale or not vehicle.latest.location_valid or vehicle.position_age_s is None or vehicle.position_age_s > 30:
        return [item('restore_signal', 'Проверить связь с ТС',
                     'Свежая достоверная позиция отсутствует. Уточните положение через доступный канал связи.', 'high')]
    p = vehicle.prediction
    if not p or (now-p.T).total_seconds() > 30 or p.target_time_begin <= now or p.telemetry_stale or vehicle.prediction_status == 'ml_unavailable':
        return [item('check_inputs', 'Проверить готовность прогноза',
                     'Проверьте расписание, целевую остановку и состояние ML. Без актуального прогноза оценка риска недоступна.', 'low')]
    result = []
    if p.risk_alert:
        result.append(item('verify_delay', 'Уточнить ситуацию у водителя',
            f'Риск опоздания более {p.delay_threshold_s:.0f} с на целевой остановке — {p.late_probability:.0%}. '
            'Факторы модели не устанавливают причину задержки.', 'high'))
        result.append(item('check_reserve', 'Проверить возможность подмены или резерва',
            'Сопоставьте ETA резерва с целевым прибытием в разделе рекомендаций. '
            'Решение требует данных о доступном ТС, пассажирах и интервалах.'))
    elif p.predicted_delay_s < -60:
        result.append(item('early_departure', 'Проверить опережение расписания',
            'Уточните выполнение планового времени на ближайшей разрешённой контрольной остановке. '
            'Не назначайте остановку или ожидание на проезжей части.'))
    elif p.predicted_delay_s > 60:
        result.append(item('monitor_deviation', 'Наблюдать динамику отклонения',
            'Прогноз показывает задержку, но порог риска классификатора не превышен. Проверьте следующий расчёт.'))
    else:
        result.append(item('monitor', 'Продолжить наблюдение', 'По актуальному прогнозу порог риска не превышен.', 'low'))
    if not vehicle.map_match or vehicle.map_match.status != 'matched':
        result.append(item('verify_position', 'Уточнить участок движения',
            'GPS не удалось однозначно привязать. Не используйте предположительное положение для команды водителю.'))
    return result


def network_risk(network, vehicles, now):
    rank = {'unknown': 0, 'normal': 1, 'attention': 2, 'high': 3}
    features = {f['properties']['segment_id']: f for f in network['features']}
    levels = {}
    for vehicle in vehicles:
        p = vehicle.prediction
        if (not p or not p.segment or p.segment.graph_version != network['graph_version'] or vehicle.is_stale
                or not vehicle.latest.location_valid or vehicle.position_age_s is None or vehicle.position_age_s > 30
                or p.telemetry_stale or not 0 <= (now-p.T).total_seconds() <= 30 or p.target_time_begin <= now
                or vehicle.prediction_status == 'ml_unavailable'):
            continue
        feature = features.get(p.segment.segment_id)
        if not feature:
            continue
        level = 'high' if p.risk_alert else 'attention' if abs(p.predicted_delay_s) > 60 else 'normal'
        key = feature['properties']['physical_id']
        entry = levels.setdefault(key, {'risk_level': level, 'risk_vehicles': [], 'late_probability': 0.0})
        if rank[level] > rank[entry['risk_level']]:
            entry['risk_level'] = level
        entry['risk_vehicles'].append(vehicle.unit_id)
        entry['late_probability'] = max(entry['late_probability'], p.late_probability)
    for feature in network['features']:
        properties = feature['properties']
        properties.update(levels.get(properties['physical_id'], {'risk_level': 'unknown', 'risk_vehicles': [], 'late_probability': None}))
        properties['risk_basis'] = 'target_approach'
    return network
