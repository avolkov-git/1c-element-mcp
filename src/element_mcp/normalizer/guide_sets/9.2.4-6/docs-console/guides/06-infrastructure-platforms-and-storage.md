# Инфраструктура: instances, платформы, DBMS и хранилища

## Разделение сущностей

- Application — пользовательское приложение и его желаемое состояние.
- Platform/technology version — программная версия runtime.
- Instance — конкретная единица развёртывания/управления платформой.
- Ecrun cluster/host — вычислительная среда, где размещаются компоненты.
- Resource scheduling — резервирование и занятие вычислительных ресурсов.
- DBMS — провайдер и база данных приложения.
- Object storage — внешнее хранилище артефактов/данных операций.
- Reverse proxy/domain — внешний маршрут к endpoint приложения.

Нельзя подменять одну сущность другой в API tool names: application ID, platform instance ID, deployment instance ID, cluster ID и host ID принадлежат разным пространствам идентификаторов.

## Instances и размещение

`Instances` содержит регистрацию, конфигурацию, quotas, debug endpoints, deployment management и синхронизацию состояния. `ApplicationClusterPlacementsManager` и scheduling связывают application с доступной инфраструктурой. Placement имеет собственные состояния: placing, placed, reconcile/displace и failures.

Изменение desired state инициирует задачу; фактическое состояние подтверждается внешней системой. При рассинхронизации Console должна reconciliate, а не просто переписать статус.

## Платформы и дистрибутивы

`G5Platforms` и `LandscapeManagement` управляют technology versions, distributions, доступностью версий и assembly/landscape operations. Обновление технологии приложения отлично от обновления прикладной assembly. Оно требует проверки совместимости, наличия дистрибутива на target infrastructure и возможности конвертации.

## DBMS

`Dbms` описывает серверы/провайдеры, настройки и операции над базами. Данные доступа должны проходить через платформенные credential/access-key механизмы. MCP не должен читать или возвращать пароль базы даже если внутренний DTO его содержит. Безопасные read tools возвращают provider, logical name, status и quota, но редактируют connection secrets.

## Object storage и dumps

`ObjectStorages` хранит конфигурацию внешних хранилищ и управляет длительными операциями/очисткой expired operations. `Dumps` загружает и предоставляет дампы, `ApplicationSnapshots` создаёт/восстанавливает снимки. Это разные артефакты и lifecycle.

Перед restore необходимо проверить application state, format/version, целевую DBMS и доступное место. Upload dump не должен автоматически запускать restore. Large payload должен передаваться stream/file, а не помещаться в JSON или prompt.

## Reverse proxy и домены

Application context path, endpoint assignment, reverse proxy и domain ownership verification образуют цепочку публикации. Смена пути запускается отдельной application operation. Для custom domain требуется доказательство владения и обновление маршрутизации; успешное сохранение домена не гарантирует немедленную доступность DNS/TLS.

## Квоты и ограничения

Billing, license restrictions, instance quotas и scheduling capacity могут независимо запретить операцию. Ошибку «нет ресурсов» нельзя автоматически трактовать как transient: агент должен вывести конкретный limiting subsystem и идентификатор запроса/task.

