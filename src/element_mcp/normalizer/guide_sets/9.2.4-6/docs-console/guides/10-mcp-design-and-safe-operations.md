# Проектирование MCP поверх Management Console

## Наборы инструментов

Рекомендуемое разделение:

### Discovery

`get_server_info`, `list_spaces`, `list_projects`, `get_project`, `list_applications`, `get_application`, `list_platform_versions`, `list_instances`, `list_tasks`.

### Development

`list_assemblies`, `upload_assembly`, `get_assembly`, `list_branches`, `get_issue`, `create_issue`, `get_ide_debug_info`.

### Deployment

`create_application`, `update_application`, `start_application`, `stop_application`, `change_application_platform`, `change_application_settings`, `wait_task`.

### Data and recovery

`list_dumps`, `upload_dump`, `create_snapshot`, `restore_snapshot`. Upload и restore — разные tools.

### Administration

Users, user lists, spaces, billing и infrastructure следует вынести в отдельный capability profile. Internal/control-plane API не включать по умолчанию.

## Контракты tool

Каждый mutation tool должен принимать точный UUID/ID, optional expected state/version и `dry_run`, если сервер позволяет предварительную проверку. Результат должен содержать:

- target identity и display name;
- endpoint/API version;
- task/resource ID;
- initial/terminal status;
- correlation ID;
- предупреждения о version mismatch;
- безопасный следующий шаг.

## Разрешения

MCP credential имеет минимальную роль. Read-only режим технически отделяется от mutation tools. Destructive tools (`delete`, revoke token, restore over data) регистрируются отдельно, чтобы клиент мог требовать подтверждение.

Нельзя расширять полномочия агента только потому, что endpoint доступен credential. Проверка должна учитывать user intent и scope текущей задачи.

## Retry и idempotency

GET можно повторять с backoff. Для POST/PUT сначала учитывать документированный idempotency contract. При ambiguous network failure искать resource/task; не отправлять повторно archive upload или create application вслепую. `wait_task` не должен запускать новое действие.

## Версионность

При connect MCP определяет server/product version и выбирает schema. Если exact version отсутствует в корпусе, tool возвращает compatibility warning. Endpoint existence проверяется через versioned catalog, а не предположение, что v2 одинаков во всех релизах.

## Ошибки

Нормализованный error для агента:

```json
{
  "category": "conflict|validation|authorization|not_found|transient|server",
  "operation": "update_application",
  "target": {"application_id": "..."},
  "http_status": 409,
  "task_id": null,
  "message": "Краткое безопасное описание",
  "details": {"current_status": "Updating"},
  "retryable": false,
  "correlation_id": "..."
}
```

Raw body сохраняется только в защищённом debug log с редактированием секретов.

## Как расширять MCP

1. Найти официальный API-документ.
2. Найти маршрут/handler в `reference/http-routes.jsonl`.
3. Прочитать controller, service, DTO/mappers и доменный manager.
4. Найти использование в IDE-плагине.
5. Описать preconditions, terminal states и side effects.
6. Добавить schema, negative tests, authorization test и version fixture.
7. Проверить против реального сервера только в разрешённой тестовой среде.

