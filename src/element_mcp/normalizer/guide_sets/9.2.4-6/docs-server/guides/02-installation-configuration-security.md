# Запуск, конфигурация и безопасность

## Instance root

Launcher использует `${instance.root}` для PID, логов, work/temp, dumps, данных и security stores. `instance-template` — шаблон начального instance, а не production-ready конфигурация. Создавать отдельный instance directory и не править файлы поставки на месте: это облегчает обновление и rollback.

## Сетевые endpoints по умолчанию

- основной HTTP: `127.0.0.1:9090`;
- debug service: включён, порт `8080`;
- integration bus: `6698`, внешний TCP выключен;
- IDE Manager: `127.0.0.1`;
- JMX: выключен; при включении template ports `9700/9701`.

Bind на loopback безопасен для локальной разработки. Для удалённого доступа рекомендуется reverse proxy/TLS и целевая firewall policy. Замена address на `0.0.0.0` открывает интерфейс всем сетевым картам и требует отдельной оценки exposure.

## Критичные template credentials

`security.yml` и `management.yml` содержат демонстрационные пароли keystore/truststore/PFX/JKS и JMX users. Это значения для локального шаблона. В любой общей или production-среде их необходимо заменить, создать новые ключи/сертификаты и хранить секреты вне репозитория.

Management Console включена. Debug также включён. Перед внешней публикацией проверить необходимость debug, JMX, IDE и management endpoints.

## Логи

Template создаёт `server.log`, `clients.log`, `unclosed_resources.log`, `debugger.log`, опциональный `access.log`; ротация обычно 10×10 MB. Основной уровень INFO, специализированные loggers позволяют включить XBSL engine, TreeSQL, data layer, Spring, jobs и AppManager debug.

TRACE/DEBUG может раскрывать данные запросов и существенно увеличить I/O. Включать на ограниченное время и для конкретного logger, затем возвращать уровень. Correlation доступны через traceId, userId, appId и requestId.

## JVM

Launcher включает heap dump при OOM. Dump может содержать secrets и персональные данные; каталог dumps должен быть защищён. Размер heap явно не задан template — JVM ergonomics используют доступные ресурсы. Для ограниченного контейнера задавать memory policy осознанно и тестировать GC/DirectBuffer usage.

## Безопасная процедура обновления

1. Сохранить instance config/data backup.
2. Установить новый bundle отдельно.
3. Сравнить template/config schemas и manifests.
4. Перенести только осознанные overrides.
5. Проверить Java version и native libraries.
6. Запустить на копии instance, проверить migrations Console/applications.
7. Проверить authentication, IDE, build/deploy, jobs и logs.
8. Переключить traffic; старый bundle сохранять до подтверждения.

