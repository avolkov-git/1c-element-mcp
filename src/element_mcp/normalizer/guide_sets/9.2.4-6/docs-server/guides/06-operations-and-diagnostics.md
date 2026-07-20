# Эксплуатация и диагностика сервера

## Проверка старта

1. Проверить Java 17 и доступность native libraries.
2. Проверить instance root и права на logs/work/dumps/data.
3. Запустить через `element-server.sh`, не напрямую main class без необходимости.
4. Смотреть сначала launcher log, затем server log.
5. Проверить bind `127.0.0.1:9090` и health/availability endpoint поставки.
6. Проверить authentication и открытие Management Console.
7. Проверить background jobs и отсутствие migration failures.

## Уровни диагностики

| Симптом | Первичные источники |
|---|---|
| Процесс не запускается | launcher log, Java version, module/native path |
| HTTP не отвечает | server endpoint config, server log, bind/port |
| Вход не работает | authentication/User Manager logs и providers |
| Console не загружается | PaaS Manager, management-console flag, migrations |
| Приложение не обновляется | Console task → AppManager → instance logs |
| IDE не открывается | IDE Manager process/workspace/gateway и Theia logs |
| Нет completion | plugin → LSP process → workspace/project diagnostics |
| Debug не подключается | application state, port 8080, debugger server/agent |
| Deadlock/timeout | LockService.Deadlock/Timeout/Abort loggers |
| Нет метрик | metrics/exporter/monitoring registration |

## Correlation

Формат логов включает traceId, userId, appId и requestId. Console background operation добавляет task ID и доменный subject. Собирайте эти значения до увеличения log level: они позволяют найти цепочку без глобального TRACE.

## Ротация и диски

Помимо rotating logs место потребляют heap/core dumps, application dumps, temp/work, IDE workspaces и repository caches. OOM/crash dumps не ротируются настройкой logging. Нужен отдельный мониторинг filesystem и политика удержания.

## Debug logging

Template предлагает отдельные loggers для библиотек 1C, DMF, TreeSQL, data layer, Spring, XBSL engine, Jobs, access log и AppManager. Включать минимальный logger на минимальный период. После воспроизведения вернуть уровень и зафиксировать временное изменение конфигурации.

## Backup boundary

Backup одного каталога logs недостаточен. В зависимости от сценария нужны instance data/config/security stores, application DBMS, object storage, repositories и Console data. Restore проверяется на совместимой версии runtime и с согласованным набором хранилищ.

