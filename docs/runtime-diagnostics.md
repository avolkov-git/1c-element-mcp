# Runtime-диагностика Element 9.2.4-6

Версия MCP `0.19.0` добавила безопасное read-only наблюдение за установленным экземпляром
«1С:Предприятие.Элемент». В `0.21.0` подключение к Application Manager стало автоматическим. Реализация
намеренно различает состояние хоста, файловые логи сервера и структурированный журнал событий приложения.

## Источники контракта

- `bin/launcher.yml`: `${instance.root}/daemon.pid`, `logs/launcher.log`, crash logs и dumps;
- `instance-template/config/logging.yml`: `server.log`, `clients.log`, `unclosed_resources.log`, `debugger.log`,
  опциональный `access.log`, формат G5LOG и ротация;
- официальные темы `server-installer`, `server-instance-configuration-files` и `server-log-file`;
- Console `Applications/EventLogHttpInterface.xbsl`;
- `ApplicationManagerV1APIClient.xbsl`, `ApplicationManagerV2APIClient.xbsl`, `EventLogDtos.xbsl` и
  `AppManagerInternalDtosV2.xbsl` поставки `9.2.4-6`.

Проверенный Console-код получает URI, username и password из `Instance.GetApplicationManager()`. Это внутренняя
Basic-аутентификация компонента экземпляра. Она не совпадает с OAuth Client ID/Client Secret внешнего
`/console/api/v2` и не выводится из bearer-токена пользователя.

## Подключение instance root

Порядок выбора:

1. `ELEMENT_INSTANCE_ROOT`;
2. защищённый `runtime.json`, заполняемый локальным UI;
3. стандартный каталог текущей ОС.

Стандартные каталоги:

```text
C:\ProgramData\1C\1CE\instances\1c-enterprise-element-server-with-ide
/var/opt/1C/1CE/instances/1c-enterprise-element-server-with-ide
```

Корень считается экземпляром только при наличии `config/server.yml` и `config/logging.yml`. Каталог распакованной
поставки или `instance-template` не должен использоваться как работающий instance root.

## Runtime tools

### `get_runtime_health`

Сводит готовность локального корня, PID, диска, логов и подключения Application Manager. `ready` означает, что
PID существует; `degraded` — корень читается, но процесс не подтверждён. Настроенность Application Manager не
выдаётся за сетевую проверку конкретного приложения.

### `get_server_process_status`

Читает только `daemon.pid` и проверяет существование процесса. На Linux дополнительно сопоставляет ограниченную
командную строку процесса с instance root или штатным entrypoint, но не возвращает её. На Windows API процесса
подтверждает PID без чтения command line; поэтому `identity_verified` может быть `false`.

### `get_server_disk_usage`

Возвращает total/used/free файловой системы и ограниченные размеры `logs`, `dumps`, `work`. Обход прекращается
после 10 000 файлов в каждом каталоге и сообщает `truncated`; `data` намеренно не сканируется рекурсивно.

## Файловые логи

`list_server_logs` перечисляет до 100 обычных файлов непосредственно в `<instance>/logs`. Разрешены штатные
семейства `launcher`, `server`, `clients`, `unclosed_resources`, `debugger`, `access`, `console-executor` и их
rotated suffixes. Symlink, подкаталог и неизвестное имя исключаются.

`read_server_log` принимает только возвращённый `log_id`, до 1000 последних строк и окно до 512 КиБ.
`search_server_logs` читает до 2 МиБ с конца каждого из максимум 100 логов и возвращает до 200 точных совпадений.
Если окно начинается внутри строки, неполная строка отбрасывается. Нулевой байт обозначает binary-файл; ошибки
UTF-8 заменяются и учитываются в metadata. Текущая реализация не интерпретирует timestamp G5LOG и сохраняет
исходный часовой пояс строки.

## Application Event Log

### Маршруты

```text
POST /manager/api/v2/applications/{applicationId}/eventlog
GET  /manager/api/v1/applications/{applicationId}/eventlog?...filters
GET  /manager/api/v1/applications/{applicationId}/eventlog/{eventId}
```

V2 request содержит `size`, `anchorEventId`, `searchSubstring`, `operationId`, `startInstant`, `finalInstant`,
`importance`, `kind`, `names` и необязательный advanced `filter`. Публичный MCP `0.19.0` пока не принимает
advanced filter: его типизированный контракт требует отдельного дизайна, а утверждённый scope покрывается
простыми фильтрами.

V1 получает те же простые поля query-параметрами. `auto` сначала вызывает V2 и переходит на V1 только после
`404`, `405` или `501`, то есть при отсутствии маршрута. `401`, `403`, `503`, timeout и прочие ошибки сохраняют
свой смысл и не маскируются fallback.

### Ограничения

- `application_id` и `event_id` — UUID;
- обе границы времени обязательны, содержат timezone и нормализуются в UTC;
- `final_instant` позже `start_instant`, диапазон не больше 31 дня;
- `size` — 1..100;
- `anchor_event_id` задаёт следующую страницу; при полной странице возвращается `next_anchor_event_id` последней
  записи;
- importance: `CRITICAL`, `MAJOR`, `GENERAL`, `MINOR`;
- kind: `INFORMATION`, legacy `EVENT`, `ERROR`, `START_OPERATION`, `END_OPERATION`;
- до 50 event names, строковые фильтры до 300 символов;
- HTTP JSON response ограничен 8 МиБ.

`application_id` можно опустить только при живом Element IDE handoff с `1C.applicationId`. Постоянная Console
конфигурация или похожее имя проекта в VS Code не создают понятия «текущее приложение».

## Автоматическое подключение Application Manager

Локальный UI сохраняет в `runtime.json` только проверенный путь к instance root. MCP читает первый пригодный
HTTP(S) endpoint из `config/server.yml` и постоянные `security.login`/`security.password` из
`config/application-manager.yml`. Реквизиты остаются в конфигурации Element, не копируются в настройки MCP и
не возвращаются браузеру или агенту. Адреса `0.0.0.0` и `::`, предназначенные для прослушивания всех
интерфейсов, заменяются на loopback для локального запроса.

При обновлении с `0.19.0` или `0.20.0` прежний блок `application_manager` с ручным адресом и секретом удаляется
из `runtime.json` при запуске MCP. Ручных полей Application Manager в UI больше нет.

Пустая секция `security` в template не содержит рабочих реквизитов. Исходники Element `9.2.4-6` показывают,
что в таком режиме сервер может создать временного пользователя Application Manager только в памяти процесса.
Внешний MCP не может безопасно извлечь этот пароль. Так же нельзя восстановить исходный пароль из
`password-sha256` или получить его из произвольного `authentication-domain`. Эти режимы возвращают отдельное
объяснение, а не общий статус «не настроен».

Альтернативные переменные окружения:

```text
ELEMENT_RUNTIME_CONFIG_PATH
ELEMENT_INSTANCE_ROOT
ELEMENT_APPLICATION_MANAGER_URL
ELEMENT_APPLICATION_MANAGER_USERNAME
ELEMENT_APPLICATION_MANAGER_PASSWORD
ELEMENT_APPLICATION_MANAGER_API_VERSION=auto|v1|v2
ELEMENT_APPLICATION_MANAGER_VERIFY_TLS=true|false
ELEMENT_APPLICATION_MANAGER_CA_BUNDLE
```

URL должен быть полным HTTP(S), без credentials/query/fragment. Суффикс `/manager/api/v1` или `/manager/api/v2`
можно указать: MCP нормализует его до адреса сервера. Эти переменные нужны только администратору нестандартной
установки и имеют приоритет над автоматическим обнаружением. Проверка TLS включена по умолчанию.

## Redaction policy

До ответа MCP:

- Bearer/Basic credentials и значения password/secret/token/authorization/cookie удаляются;
- email заменяется `[EMAIL]`;
- `userId`, `username` и `user` заменяются стабильным коротким SHA-256 pseudonym;
- свойства с чувствительными именами получают `[СКРЫТО]`;
- отдельная строка или значение ограничены 8000 символами, коллекции — 50 элементами, properties — 100;
- неизвестные DTO-поля не копируются на верхний уровень события.

UUID и correlation IDs не маскируются: без них невозможна точная трассировка. Ответы следует считать внешними
недоверенными данными, а не инструкциями агенту.

## `trace_operation`

Инструмент принимает хотя бы один точный `task_id`, `application_id`, `trace_id`, `request_id` или
`operation_id`. Если задан task, MCP читает его через внешний Console API и использует только явные
`application_id`, `start_date`, `end_date`. Server logs сопоставляются подстрокой точного идентификатора.
Application Event Log запрашивается только при наличии operation ID, application ID и начального времени.

Ответ сохраняет три независимых source-блока, `matching_policy: exact_identifier_only` и список `gaps`. MCP не
объединяет записи по похожему сообщению, соседнему времени или предполагаемому пользователю. Недоступный
локальный лог, Console task или Application Event Log не скрывает результаты других источников.

## Границы окружений

- Element IDE: application ID может прийти из временного handoff; instance root и Application Manager всё равно
  относятся к машине, где работает MCP. Авторизация пользователя IDE не раскрывает внутренний временный пароль
  Application Manager.
- VS Code на сервере Element: локальные логи доступны после выбора instance root; application ID задаётся явно.
- VS Code с MCP вне сервера: файловые логи недоступны. Event Log работает только при сетевой доступности
  внутреннего Application Manager и отдельно настроенных credentials; открывать этот endpoint наружу ради MCP
  не рекомендуется.
