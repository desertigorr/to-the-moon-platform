# Документация To the Moon

## Запуск и проверка

- [README: установка, Docker Compose, входные файлы и адреса сервисов](../README.md).
- [Архитектура и интеграция трёх прикладных модулей](integration.md).
- [Как исторические координаты проходят через официальный эмулятор](emulator-scenario.md).
- [Метрики ML, производительность и ограничения](metrics.md).
- [Проверки сборки, чистого запуска и восстановления после отказов](verification.md).

## API: OpenAPI / Swagger

- [Описание HTTP, WebSocket и ML-контрактов](api.md).
- [Сохранённая OpenAPI-схема backend](openapi.json).
- [Сохранённая OpenAPI-схема ML](ml-openapi.json).

После запуска контейнеров интерактивный Swagger backend доступен по адресу <http://localhost:8000/docs>, схема — <http://localhost:8000/openapi.json>. При изменении API_PORT используйте свой порт. ML API доступен внутри Compose-сети по адресу `http://ml:8001/docs`; для чтения его контракта запуск не нужен — JSON выше включён в репозиторий.

## Документация кода: PyDoc

Готовые HTML-страницы находятся в [code/](code/), точка входа — [index.html](code/index.html). GitHub показывает HTML как исходный текст. Для просмотра с оформлением откройте <http://localhost:8000/documentation/> после запуска backend либо скачайте репозиторий и откройте `docs/code/index.html` в браузере. Ссылки на Swagger требуют запущенного backend.

Задокументированы приём и декодирование NDTP, состояние ТС, расписание, обработка потока, геометрия, сценарий эмулятора, рекомендации, ML API, объяснения и общие контракты.

Повторная генерация PyDoc и обеих OpenAPI-схем из корня проекта в PowerShell:

```powershell
docker compose run --rm --no-deps -v "${PWD}:/app" -w /app ml python -m scripts.build_docs
```

В Linux/macOS:

```sh
docker compose run --rm --no-deps -v "$PWD:/app" -w /app ml python -m scripts.build_docs
```
