# Исходная и доменная модель Console

## Соглашение пары YAML/XBSL

Большинство элементов представлены парой `Name.yaml` + `Name.xbsl`. YAML определяет метаобъект, XBSL — код. В срезе преобладают:

- 684 `InterfaceComponent`;
- 534 `CommonModule`;
- 356 `Structure`;
- 189 `Enumeration`;
- 172 `Catalog`;
- 62 `InformationRegister`;
- 45 `HttpService`;
- 35 `ServiceContract`;
- 19 `StorableStructure`.

Также используются global client events, type/entity contracts, virtual tables, commands, access keys, scheduled jobs, documents, event log events, settings storage и navigation commands.

## Роли файлов

- `Subsystem.yaml` — зависимости подсистемы.
- `Project.yaml`/`Project.xbsl` — корень приложения и миграции.
- `*.Object.xbsl` — объектная логика записи каталога/документа.
- `*Manager.xbsl` — доменная операция или facade.
- `*Structures.xbsl`, `*Dtos*.xbsl`, `*Mappers.xbsl` — API boundary.
- `*HttpController.yaml/.xbsl`, `*HTTPService.*` — маршруты и handlers.
- `*Form.yaml/.xbsl`, `*Page.*`, `*Component.*` — UI.
- `*TaskProducer.*`, `*TaskManager.*`, `*Workflow.*`, `*Action.*` — фоновые процессы.
- `*.xbql` — переиспользуемый запрос/виртуальная таблица.
- `Resources` — изображения и упакованные шаблоны.

## Каталоги и регистры

Хранимые доменные объекты часто оформлены каталогами с object module. Регистры используются для связей, состояний, отображений и быстрых выборок. Права доступа вычисляются как для самого типа, так и для объектов (`RecomputeAccessPermissions`, `RecomputeAccessPermissionsForObjects`). Агент не должен обходить этот слой прямым неограниченным запросом.

## Контракты и DTO

Внутренние ссылки платформы не должны утекать через HTTP. Контроллер вызывает service, service строит DTO через structures/mappers, затем сериализатор применяет JSON-аннотации и naming contract. При добавлении поля нужно проверить:

1. структуру входа/выхода;
2. mapper в обе стороны;
3. optional/`Undefined` поведение;
4. официальную API-схему и пример;
5. обратную совместимость клиента IDE;
6. фильтрацию чувствительных полей.

## Связи и поиск impact radius

`reference/imports.jsonl` показывает статические импорты, но вызов может идти через callback `&Module.Method`, контракт или платформенное событие. Для impact analysis следует объединять:

- поиск имени элемента в YAML/XBSL/XBQL;
- import-граф;
- `reference/methods.jsonl`;
- handlers из `reference/http-routes.jsonl`;
- аннотации обработчиков;
- ссылки на UUID элемента;
- LSP references.

## Права и привилегированный контекст

HTTP YAML содержит `AccessControl`; в XBSL применяются checks и `AccessContext`. `PermitAuthenticated` означает лишь базовую аутентификацию endpoint, доменное право всё равно может проверяться глубже. `AccessContext.Privileged()` используется для ограниченных служебных операций. Расширение его области или добавление endpoint без сопоставимого access check следует рассматривать как высокорисковое изменение.

