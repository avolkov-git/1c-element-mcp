# Пространства, пользователи, доступ и биллинг

## Многоуровневая tenancy-модель

Console различает subscribers, cloud services/subscriptions, spaces, PaaS users, application user lists, service users и enrollment. Их нельзя объединять в абстрактного `user` или `tenant`:

- Subscriber представляет клиента/организацию и состояние обслуживания.
- Space задаёт область ресурсов и доступа.
- Cloud service/subscription связывает доступные услуги и тариф.
- PaaS user работает с Console и проектами.
- User list задаёт пользователей конкретного приложения/набора приложений.
- Service user и access token предназначены для машинной интеграции.
- Enrollment управляет приглашением/подключением и approval.

## Проверка доступа

Права вычисляются на уровне типов и объектов. Team роли, project groups, spaces, service users, issues reviewers и user lists участвуют в разных access domains. Миграции `Project.xbsl` регулярно вызывают recompute permissions после изменения модели — признак того, что права являются материализованным/вычисляемым состоянием, а не только динамической проверкой.

При добавлении новой связи нужно проверить:

1. права типа;
2. права конкретных объектов;
3. доступ удалённых/API users;
4. поведение privileged maintenance;
5. миграционный пересчёт существующих данных.

## Access tokens и client credentials

API содержит управление machine credentials и tokens user lists. Секрет обычно показывается только при создании. Корпус индексирует имена полей, но значения секретов не должны попадать в generated docs. MCP должен отделять metadata credential от secret value и поддерживать ротацию/отзыв как явные destructive actions.

## Billing

`Billing` — самая большая по числу файлов подсистема. Она моделирует subscriptions, pricing plans, billing services, application/background/parallel work options, cloud services и связанные UI/processes. Billing state влияет на разрешённые операции и resource limits.

Изменение application option может запустить инфраструктурную переконфигурацию, а не только изменить цену. Поэтому billing tool должен показывать effective plan/options, будущий эффект и task, если изменение асинхронно.

## Enrollment и уведомления

Enrollment может требовать approval, иметь rate/auto-accept scheduling и создавать пользователя/подписчика/space. `Notifications`, SMS gate и subscription settings доставляют статусы процесса. Не считать отправленное уведомление доказательством завершения enrollment.

## Удалённые пользователи

Некоторые v2 handlers обёрнуты `RemoteAccessUsersUtils.WithAccessCheck`. Это дополнительная граница к обычному `PermitAuthenticated`. При создании нового endpoint следует выбрать существующий pattern аналогичного ресурса, а не ограничиваться декларацией YAML.

