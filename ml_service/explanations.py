"""Individual classifier attribution and measured inputs, without causal claims."""

import math
import re

from catboost import Pool

from shared.contracts import PredictionFactor


LABELS = {
    "cur_dev_s": ("Отклонение на последнем подтверждённом прибытии", "с"),
    "cur_dev_missing": ("Прибытие ещё не подтверждено", "признак"),
    "horizon_s": ("Время до целевой остановки", "с"),
    "hour_sin": ("Время суток, циклический признак sin", ""),
    "hour_cos": ("Время суток, циклический признак cos", ""),
    "target_lat": ("Широта целевой остановки", "°"),
    "target_lon": ("Долгота целевой остановки", "°"),
    "last_lat": ("Последняя широта GPS", "°"),
    "last_lon": ("Последняя долгота GPS", "°"),
    "last_packet_age_s": ("Возраст последнего пакета", "с"),
    "gps_age_s": ("Возраст последнего GPS", "с"),
    "gps_missing": ("Нет координат GPS", "признак"),
    "gps_stale": ("Устаревший GPS", "признак"),
    "last_speed": ("Последняя скорость", "км/ч"),
    "distance_to_target_m": ("Расстояние до цели по прямой", "м"),
    "required_straight_speed_kmh": ("Необходимая скорость по прямой до цели", "км/ч"),
    "heading_alignment": ("Согласованность курса с направлением на цель", ""),
    "approach_speed_mps": ("Скорость приближения к цели по прямой", "м/с"),
    "speed_change_60_vs_300": ("Изменение средней скорости: 1 мин к 5 мин", "км/ч"),
    "planned_arrivals_until_target": ("Плановых прибытий до цели", "шт."),
    "since_previous_planned_s": ("Время от предыдущего планового прибытия", "с"),
    "until_next_planned_s": ("Время до следующего планового прибытия", "с"),
    "n_packets": ("Пакетов", "шт."),
    "n_valid": ("Достоверных координат", "шт."),
    "invalid_fraction": ("Доля недостоверных координат", "доля"),
    "speed_mean": ("Средняя скорость по измерениям", "км/ч"),
    "speed_time_mean": ("Средняя скорость по времени", "км/ч"),
    "speed_median": ("Медианная скорость", "км/ч"),
    "speed_std": ("Разброс скорости", "км/ч"),
    "speed_p10": ("10-й процентиль скорости", "км/ч"),
    "speed_p90": ("90-й процентиль скорости", "км/ч"),
    "coverage_fraction": ("Покрытие окна измерениями", "доля"),
    "stopped_fraction": ("Доля стоянки в наблюдаемом времени", "доля"),
    "speed_distance_m": ("Путь по измерениям скорости", "м"),
    "gps_displacement_m": ("Смещение GPS", "м"),
}


def feature_label(name):
    match = re.fullmatch(r"(.+)_(60|180|300|600)s", name)
    base, suffix = (match[1], f" за {int(match[2]) // 60} мин") if match else (name, "")
    label, unit = LABELS.get(base, (base, ""))
    return label + suffix, unit


def measured_observations(row):
    result = []
    def known(key):
        return math.isfinite(float(row.get(key, float("nan"))))
    if known("cur_dev_s"):
        result.append(f"Отклонение на последнем GPS-прибытии: {row['cur_dev_s']:+.0f} с.")
    else:
        result.append("Прибытие ещё не подтверждено: текущее отклонение неизвестно.")
    if known("speed_time_mean_60s"):
        result.append(f"Средняя скорость за минуту: {row['speed_time_mean_60s']:.1f} км/ч; "
                      f"наблюдения покрывают {row['coverage_fraction_60s']:.0%} минуты.")
    if known("stopped_fraction_180s") and row["coverage_fraction_180s"] >= .25:
        result.append(f"Стоянка (≤1 км/ч): {row['stopped_fraction_180s']:.0%} наблюдаемого времени за 3 мин.")
    if known("speed_change_60_vs_300") and row["coverage_fraction_300s"] >= .25:
        result.append(f"Средняя скорость за минуту относительно 5 минут: {row['speed_change_60_vs_300']:+.1f} км/ч.")
    if known("gps_age_s"):
        result.append(f"Последняя GPS-точка получена {row['gps_age_s']:.0f} с назад.")
    return result


def explain_batch(predictor, features, probabilities):
    names = predictor.risk.meta["feature_names"]
    values = predictor.risk.model.get_feature_importance(
        Pool(features[names]), type="ShapValues", thread_count=2)
    calibration = predictor.risk.meta["calibration"]
    output = []
    for index, (_, row) in enumerate(features.iterrows()):
        contributions = values[index, :-1] * calibration["coefficient"]
        baseline = float(values[index, -1] * calibration["coefficient"] + calibration["intercept"])
        chosen = sorted(range(len(names)), key=lambda j: abs(contributions[j]), reverse=True)[:5]
        factors, texts = [], []
        for j in chosen:
            if abs(contributions[j]) < 1e-9:
                continue
            label, unit = feature_label(names[j])
            value = float(row[names[j]])
            value = value if math.isfinite(value) else None
            direction = "increases_risk" if contributions[j] > 0 else "decreases_risk"
            factors.append(PredictionFactor(feature=names[j], label=label, value=value, unit=unit,
                                           contribution=float(contributions[j]), direction=direction))
            measured = "нет данных" if value is None else f"{value:.2f} {unit}".strip()
            texts.append(f"{label}: {measured} — {'повышает' if contributions[j] > 0 else 'снижает'} оценку риска моделью.")
        probability = float(probabilities[index])
        total = math.log(probability / (1 - probability))
        output.append({"factors": factors, "explanations": texts,
                       "observations": measured_observations(row), "explanation_method": "tree_shap",
                       "risk_baseline_log_odds": baseline,
                       "risk_other_contribution": total - baseline - sum(f.contribution for f in factors)})
    return output
