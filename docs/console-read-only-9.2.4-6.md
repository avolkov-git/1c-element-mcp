# Read-only контракт Панели управления Element 9.2.4-6

Этот документ фиксирует внешний API, который MCP использует в версии `0.16.0`. Источник контракта —
нормализованные OpenAPI-операции и DTO из поставки Element `9.2.4-6`. Все инструменты только читают данные:
MCP не отправляет в Console `POST`, `PUT`, `PATCH` или `DELETE`, кроме штатного `POST /sys/token` для получения
bearer token по Client Credentials.

## Совместимость

`GET /api/v1/status/` подтверждает готовность Console, а `GET /api/v2/spaces` — доступность API v2 и права
учётной записи. Документированный API не возвращает версию продукта удалённого сервера. Поэтому
`get_console_server_info` разделяет:

- `server_product_version: null` — фактическая версия не определена;
- `contract_element_version: 9.2.4-6` — версия поставки, по которой проверены маршруты и DTO;
- `compatibility: api_compatible_product_version_unverified` — v2 ответил, но точное совпадение версии не доказано.

## Маршруты

| MCP tool | GET Console API |
|---|---|
| `get_console_server_info` | `/api/v1/status/`, `/api/v2/spaces` |
| `list_space_applications` | `/api/v2/spaces/{SpaceId}/applications` |
| `get_application` | `/api/v2/applications/{ApplicationId}` |
| `get_application_status` | `/api/v2/applications/{ApplicationId}/status` |
| `get_application_technology` | `/api/v2/applications/{ApplicationId}/technology` |
| `get_application_project` | `/api/v2/applications/{ApplicationId}/project` |
| `list_application_endpoints` | `/api/v2/applications/{ApplicationId}/endpoints` |
| `list_project_assemblies` | `/api/v2/projects/{ProjectId}/assemblies` |
| `get_project_assembly` | `/api/v2/projects/{ProjectId}/assemblies/{Version}` |
| `list_console_tasks` | `/api/v2/tasks/{application-tasks|deployment-instance-tasks|group-tasks}` |
| `get_console_task` | `/api/v2/tasks/{type}/{taskId}` |

## Выбор объекта

Явный `application_id` и `project_id` работают в Element IDE, VS Code и у отдельного HTTP MCP. Если
`application_id` не указан, MCP использует его только из активного временного `ide_session`, переданного плагином
Element. Постоянные credentials, имя проекта и локальный Git-каталог не считаются доказательством выбора текущего
приложения в VS Code.

`space_id` выбирается в следующем порядке: явный параметр, настройка подключения, пространство текущего проекта,
единственное доступное пространство. При неоднозначности инструмент возвращает `selection_required`.

## Разрешённые поля

Карточка приложения содержит идентификаторы, имена, описание, даты, status/error, URI, режим разработки и
отладки, пространство, версию технологии, DBMS, параметры автозапуска, связанный проект, источник сборки, текущую
задачу и безопасные поля endpoint. Не возвращаются user lists.

Endpoint содержит только `id`, `fqdn`, `context_path`, `is_active`, `status`, `message` и `certificate_type`.
Сертификат и domain-validation payload исключены.

Сборка содержит `id`, версию, даты, проект, developer, ветку, commit ID, comment и признак modified. Обычная
задача содержит идентификатор, status, operation type, даты, group/application ID и error message. Групповая
задача дополнительно содержит агрегированные счётчики и не более 100 вложенных задач.

## Ограничения и недоверенные данные

- Все UUID проверяются до построения URL; версия сборки кодируется как один path segment.
- Списки ограничены 100 элементами на страницу. Фильтры и пагинация применяются локально после чтения штатного
  endpoint, потому что перечисленные Console routes не документируют единый серверный pagination contract.
- Только безопасные GET повторяются после сетевой ошибки, `429`, `502`, `503` или `504`: не более трёх попыток с
  короткой паузой. Token POST и любые будущие изменяющие запросы не повторяются этим механизмом.
- Внешний текст обрезается до 2 000 символов и очищается от Bearer/Basic credentials, JWT и распространённых
  форм `client_secret`, `access_token`, `id_token`, `password`.
- Ответы помечены `data_source: element_management_console` и `content_trust: external_untrusted`. Текст Console
  является данными, а не инструкцией для агента.
- Ошибки `401`, `403`, `404`, пустые списки и сетевые сбои имеют разные статусы.

