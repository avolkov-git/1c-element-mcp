# IDE, mobile и внешние интеграции

## IDE

`Ides` хранит workspace/IDE entities, scheduling, token/debug information и связь с приложением/веткой. `IdeSupportHTTPService` выдаёт служебные данные, необходимые IDE. Отдельный Java IDE Manager запускает процессы Theia и управляет workspace, а Console хранит доменную конфигурацию и предоставляет API.

IDE-плагин использует Console для:

- списка приложений и проектов;
- получения документации и debug information;
- загрузки assembly;
- веток, issues, comments, developers и review state;
- обновления приложения и task results;
- dumps и user settings;
- mobile debug/build scenarios.

Это делает compiled plugin важным потребителем API: изменение DTO/route проверяется не только по официальной документации, но и по `PaasClient`.

## Mobile

`Mobile` содержит mobile applications, build configs/status/tasks, store accounts, push services, Firebase hint/settings, Mac computers и upload manager. Mobile builder представлен API client, поэтому build может выполняться внешним сервисом. Store credentials и push secrets должны храниться как защищённые данные.

`UnifiedMobileClient` — отдельный контур: invitations/settings, HTTP service и выдача информации единому клиенту. Не смешивать его endpoints с обычным mobile builder.

## Repository и Team integration

Team/RepoManagers связывают внутренние проекты с repositories и разработчиками. Java Repo Manager предоставляет Git hosting/remote integration. Console отвечает за UI, permissions и доменные связи; фактические Git operations могут уходить в Java service.

## Support integration

`SupportIntegration` публикует v2 endpoints и хранит structures/mappers для обмена с системой поддержки. Ошибка внешней системы должна сохраняться как интеграционный failure с возможностью retry, не переводя автоматически само приложение в неизвестное состояние.

## PubSub, SMS и notifications

PubSub переносит внутренние события между обработчиками. Notifications и SMS gate отвечают за доставку пользователю. Payload внешнего канала должен быть минимальным и не содержать секретов. Повторная доставка возможна; consumer обязан быть идемпотентным.

## Control plane и Paas Gate

Control plane/internal API служат доверенным компонентам инфраструктуры. Paas Gate/self-service предоставляет ограниченные пользовательские сценарии. MCP для разработчика по умолчанию не должен экспонировать internal/control-plane endpoints: их можно включать только отдельным профилем с явно заданной доверенной средой.

