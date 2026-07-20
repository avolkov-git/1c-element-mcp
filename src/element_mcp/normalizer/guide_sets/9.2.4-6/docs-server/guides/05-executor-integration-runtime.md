# Executor, ESB и прикладные runtime-сервисы

## Executor

`executor` — отдельный комплект для выполнения XBSL-сценариев. Он имеет boot/app/core/runtime/compiler/client/agent modules и объекты environment, script, TCP/ECS. README требует Java 11+, в то время как основной server launcher требует Java 17.

Executor полезен для изолированных scripts/tools, но не является полной заменой AppEngine server: у него своя доступная поверхность объектов и окружение. Перед выполнением сгенерированного скрипта агент должен сверить документацию Executor и не предполагать доступность project metadata/runtime services.

## ESB и integration bus

ESB модули содержат common/designtime/runtime, XBSL compile-time/runtime, standalone, service/binding/config. Integration bus template использует порт 6698, TCP по умолчанию выключен, отдельные pools для external I/O и internal VM clients.

Включение TCP создаёт новую сетевую поверхность. Необходимо authentication/TLS/firewall и нагрузочные лимиты. Для локального server composition внутренний VM transport предпочтительнее.

## Background и scheduled jobs

Сервисы backgroundjob, scheduledjob, jobs config и lifecycle исполняют прикладные задания. Lock manager предотвращает конфликтующий доступ, numbering выдаёт последовательности, event log фиксирует события. Ошибки job следует искать по job/task key и application context.

## Data services

DB connections, datalayer, TreeSQL/XBQL, lock manager, FTS, temp storage, text extractor и object storage образуют инфраструктуру данных. Прикладной код должен пользоваться платформенными API, а не внутренними Java packages. Внутренние классы могут изменяться даже в patch release.

## Documents и media

В runtime включены библиотеки PDFBox, docx4j и FOP; logging template снижает их уровень до ERROR. Это косвенно подтверждает document conversion functionality. Большие документы и media требуют stream/temp storage и контроля ресурсов.

## Mobile builder и PaaS gate

Server service mobilebuilder соединяет Console с mobile build infrastructure. PaaS gate service/api/webapp обслуживает self-service/integration surface. Эти endpoints имеют иной trust boundary, чем основной Console API.

