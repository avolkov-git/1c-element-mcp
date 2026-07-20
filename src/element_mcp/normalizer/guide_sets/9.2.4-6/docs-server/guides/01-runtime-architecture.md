# Архитектура серверного runtime

## Запуск

`element-server.sh` управляет запуском/остановкой и debug mode. `bin/launcher.yml` задаёт модульный Java bootstrap:

- main class `com.e1c.chassis.app.service.internal.Service`;
- минимальная Java 17;
- module path `lib/boot/modules`;
- рабочие слои загружают `lib/chassis/modules`;
- G1 GC, string deduplication, heap dump при OOM и crash dump;
- instance-specific temp, logs, dumps и PID.

Это один процесс-контейнер с большим набором модулей, а не набор независимых Docker-сервисов. Chassis отвечает за bootstrap, конфигурацию, lifecycle, HTTP, security, logging, metrics и management. Функциональные модули платформы подключаются слоями/activators.

## Крупные контуры

```text
Chassis / configuration / lifecycle / HTTP / security
  ├─ AppEngine runtime (metadata, XBSL, UI, data layer)
  ├─ Application Manager (applications, assemblies, instances)
  ├─ PaaS Manager (Management Console application)
  ├─ Repo Manager (Git repositories and collaboration)
  ├─ IDE Manager + Theia (workspaces and browser IDE)
  ├─ Authentication + User Manager
  ├─ Debugger / LSP / documentation
  ├─ Jobs / locks / event log / FTS / temp storage
  └─ ESB / integration bus / mobile builder / object storage
```

## AppEngine

Модули `com.e1c.g5rt.appengine.*` реализуют design-time и runtime прикладных объектов: catalog, document, registers, UI, HTTP/SOAP, ESB, scheduled/background jobs, full-text search, reports и другие. Для многих объектов есть разделение `common`, `designtime`, `runtime`, `xbsl.compiletime`, `xbsl.runtime`.

Compile-time слой предоставляет типы и metaobjects компилятору/LSP. Runtime слой реализует выполнение. Поэтому наличие completion в IDE ещё не гарантирует доступность объекта в выбранном environment — необходима сборка проекта и проверка runtime modules.

## Managers

Application/Repo/IDE/User/PaaS managers имеют повторяющуюся структуру:

- `domain` — сущности и правила;
- `app`/`app.api` — use cases и интерфейсы;
- `repo`/`storage`/`datalayer` — хранение;
- `webapp` и DTO — HTTP boundary;
- `config` и server activators — подключение к runtime;
- `instance.lifecycle` — создание/обновление/остановка instance state.

## Внутрипроцессные сервисы

Background/scheduled jobs, lock manager, event log, numbering, FTS, temp storage, text extractor и DB connections предоставляются как server services. Интеграционная шина использует встроенный broker; конфигурация по умолчанию запрещает внешний TCP, оставляя внутренние VM-взаимодействия.

## Версии компонентов

Бандл 9.2.4-6 включает семейства артефактов с суффиксами `9.2.4-6` и `9.2.4-1`, Chassis `3.3.12-18` и другие независимые версии библиотек. При сравнении релизов нельзя полагаться только на version root: diff JAR manifest/SHA определяет фактическое изменение компонента.

