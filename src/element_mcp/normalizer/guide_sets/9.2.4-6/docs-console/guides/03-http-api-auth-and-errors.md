# HTTP API, аутентификация и ошибки

## Поверхности API

Console содержит несколько семейств:

- `/v1/...` — совместимые endpoints приложений, проектов, пользователей, задач, платформ, DBMS, IDE support и служебных операций;
- `/v2/...` — основной ресурсный API: applications, projects, branches, issues, tasks, spaces, users, user lists, subscribers/cloud services, technologies, hosts, DBMS, enrollment и support integration;
- `/internal/v1` и `/docs-internal/v1` — внутренние контракты, не предназначенные для обычного MCP-клиента;
- `/paas-gate/v2` и `/self-service/v1|v2` — gate/self-service сценарии;
- специализированные endpoints dumps, metrics, reverse proxy, configuration images и monitoring.

Внешняя публикация обычно добавляет базовый путь Console, например `/console/api`. Не конструировать URL только по `RootUrl`: брать base URL из конфигурации сервера/IDE и сверять с официальной страницей endpoint.

## Аутентификация

Официальный service-to-service сценарий использует client credentials. Токен запрашивается через системный token endpoint; client ID и client secret передаются согласно описанию конкретной версии, затем access token используется в последующих запросах. IDE-плагин хранит настройки `1C.server`, `1C.clientId`, `1C.clientSecret`, `1C.applicationId`, `1C.projectId` и автоматически обновляет авторизацию.

Секреты нельзя помещать в корпус, логи MCP, параметры tool result или текст ошибки. MCP должен получать их из secret store/environment, редактировать заголовки в трассировке и не возвращать raw response token агенту без необходимости.

## Путь запроса

Типовой v2 controller:

1. `HttpService` сопоставляет HTTP method/template с handler.
2. Handler извлекает path/query parameters и body.
3. `RemoteAccessUsersUtils.WithAccessCheck` или аналог проверяет контекст.
4. `ServiceUtils.WithExceptionHandling` вызывает `Api*Service` и задаёт success code.
5. Service валидирует DTO, права и состояние объекта.
6. Mapper формирует JSON response или унифицированную ошибку.

Некоторые Team endpoints делегируют `ServiceUtils.ExecuteApiRequest` во внешний manager issues/branches. Это отдельный путь обработки ошибок и логирования.

## HTTP-коды и асинхронность

Создание ресурса/сборки обычно возвращает `201`, чтение и операции — `200`; но завершение HTTP-вызова не всегда означает завершение инфраструктурной операции. Update/start/stop/delete могут создать task. Клиент обязан распознать ID задания и опрашивать task endpoint с backoff до terminal state.

Не повторять автоматически небезопасный POST без idempotency анализа. Для сетевой ошибки после отправки сначала искать созданный resource/task по correlation data.

## Машиночитаемый каталог

`reference/http-routes.jsonl` содержит 466 операций: service, root URL, template, HTTP method, handler и `AccessControl`. Официальные 266 страниц в `normalized/official-api` добавляют параметры, schemas и примеры. При генерации MCP tool contract объединять обе записи по методу и маршруту; официальный контракт определяет внешнюю форму, исходник — реальный handler.

## API v1 и v2

Не считать v2 механическим переименованием v1. В v2 изменены resource boundaries, DTO, pagination и методы некоторых операций. Новый MCP должен предпочитать документированный v2 endpoint, а v1 использовать только там, где capability отсутствует в v2 или этого требует сервер текущей версии.

