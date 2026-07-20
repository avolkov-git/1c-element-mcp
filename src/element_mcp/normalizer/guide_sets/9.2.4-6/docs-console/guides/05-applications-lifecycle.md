# Приложения и их жизненный цикл

## Модель

`Applications` — крупнейшая доменная подсистема. Она связывает project/assembly, версию платформы, DBMS, instance/cluster placement, object storage, endpoints, user lists, billing services, monitoring, dumps и scheduled resources.

`ApplicationStatus` отражает низкоуровневые стадии (`Configuring`, `Ready`, `UPDATING`, `CONVERTING`, `STARTING`, ошибки и т.д.). `ApplicationTotalStatus` — агрегированное состояние для пользователя/API: `Initializing`, `Running`, `Stopped`, `Error`, `UpdateRequired`, `Updating`, `Migrating`, `NeedsConversion`, `Suspended`, `Restoring`, `TechnologyUpdating` и другие. Клиент должен опираться на документированный total status, но сохранять underlying status для диагностики.

## Создание

Create management резервирует/проверяет ресурсы, связывает сборку и user lists, создаёт доменные записи, планирует размещение, конфигурирует instance/DBMS и регистрирует приложение. Ошибка посередине не равна простому отсутствию приложения: остаётся task и промежуточное состояние, которое должен обработать workflow/cleanup.

## Запуск и остановка

Start реализован как workflow с шагами запуска, настройки прав и post-start действий. Stop — отдельный manager. Возможны переходные `Starting`/`Stopping`, ошибки и блокировки конкурирующих операций. Повторный вызов допустим только после проверки текущего статуса и активной task.

## Обновление

Update связывает приложение с новой assembly/project version. Процесс может включать pre-update, остановку/подготовку, применение метаданных, конвертацию данных, post-update и повторный запуск. Состояния `UpdateRequired`, `NeedsConversion`, `DataConverting`, `Updating`, `Error` нельзя схлопывать в один boolean.

Подсистема поддерживает managed/group updates. Групповая операция имеет собственный status и может планироваться. MCP должен возвращать отдельные application task IDs и group task ID.

## Остальные операции

Исходники выделяют managers для:

- delete/register/unregister;
- suspend/unsuspend;
- relocation и fast moving;
- platform/technology change;
- context path change;
- application settings;
- access control recomputation;
- log shrinking;
- monitoring registration;
- snapshots/dumps/restore;
- массового start/stop/delete/relocate и object-storage операций.

Каждая операция имеет собственные prerequisites и cleanup. Универсальный MCP tool `operate_application(action=...)` скроет слишком много различий; предпочтительны типизированные tools с отдельной входной схемой.

## Конкуренция и задания

Application task lock manager и subject locks предотвращают несовместимые операции. Перед мутацией проверить текущие tasks и total status. После запуска:

1. сохранить task ID;
2. опрашивать status с ограниченным backoff;
3. показывать stage/progress/error;
4. при terminal failure возвращать диагностические identifiers и безопасные next steps;
5. не запускать автоматический rollback, если конкретный workflow его не гарантирует.

## Удаление

Удаление затрагивает данные, размещение, endpoint, monitoring, storage и billing links. Оно асинхронно и потенциально необратимо. Инструмент должен требовать exact application ID, отображать имя/space/status перед выполнением и не трактовать HTTP 2xx как окончательное физическое удаление до terminal task state.

