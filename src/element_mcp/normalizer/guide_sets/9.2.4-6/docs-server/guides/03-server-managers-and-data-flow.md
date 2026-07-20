# Server managers и потоки данных

## Application Manager

Семейство `appmanager` включает domain, database/datalayer, repositories instance/external, cache/aggregator, lifecycle, cluster/raft, long operations, archive, notifications, settings facade, system API и webapp. Оно управляет развёрнутыми приложениями и состоянием их конфигураций/данных.

Management Console инициирует доменную операцию, server PaaS/AppManager service исполняет её, а status/task возвращается в Console. Поэтому поиск ошибки update должен охватывать Console task, AppManager log и конкретный instance.

## PaaS Manager

`server.paasmanager` — мост к прикладной Management Console. JAR содержит небольшой Java bootstrap и 4 283 ресурса конфигурации `e1c/console`. Настройки включаются через `management-console.enabled`.

## Repo Manager

`repomanager` имеет app/api/domain/config/webapp. По class/package inventory видны операции с hosted/remote repositories, branches, tags, commits, deploy keys, archives, cherry-pick и secret encryption. Console Team хранит доменные связи и UI; Repo Manager выполняет Git/HTTP работу.

## IDE Manager

`idemanager` управляет lifecycle IDE process, workspace, gateway/authentication, collaboration и Git context. Theia frontend находится в `ide/theia`; AppEngine plugin подключает LSP, debugger, metadata editors и Console client. IDE Manager привязан к localhost по template, а внешний маршрут обычно проходит через серверный gateway.

## Authentication и User Manager

Authentication поддерживает общий server/client contract, OIDC, CAS, ESIA и negotiate providers. User Manager хранит пользователей и системные API, имеет repository/storage layers и webapp auth. Пустые YAML секции в template означают использование defaults/auto-configuration, а не отключение защиты.

## Поток build/deploy

```text
IDE workspace
  → AppEngine plugin ZIP/upload
  → Console /projects/{id}/assemblies
  → project/assembly storage
  → Console application update task
  → Application Manager / target instance
  → compile/migrate/start
  → status + logs + debugger endpoint
```

При сбое идентификаторы project, assembly version, application, task и instance должны сохраняться вместе: каждый указывает на свой участок трассы.

## Documentation service

`server.docs.app`, authentication client и docs webapp публикуют статический комплект документации. IDE получает docs information через Console/Paas client и открывает нужную тему. Версия документации может быть общей для minor-линии (9.2), тогда как сервер имеет build version 9.2.4-6.

