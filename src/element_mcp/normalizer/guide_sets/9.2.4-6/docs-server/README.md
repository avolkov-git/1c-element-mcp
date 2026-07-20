# Серверный бандл «1С:Предприятие.Элемент» 9.2.4-6

Корпус описывает полную локальную поставку `1c-enterprise-element-server-with-ide` версии `9.2.4-6`: 19 285 файлов общим объёмом около 2,91 ГБ.

## Что проиндексировано

- SHA-256, размер, расширение и путь каждого файла бандла;
- 1 496 JAR из runtime, Executor и AppEngine IDE plugin;
- 264 817 Java-классов, агрегированных по 16 127 пакетам;
- manifest/main class, ресурсы и package map каждого JAR;
- launcher и instance-template configuration;
- 66 восстановленных TypeScript-исходников IDE plugin;
- 1 372 текстовых LSP-ресурса: XCore/Ecore-модели, грамматики и эталонные XBSL/YAML/XBQL-проекты;
- релевантные официальные темы о сервере, IDE, deployment и администрировании.

## Навигация

- `guides` — архитектура, запуск, подсистемы и эксплуатация.
- `versions/9.2.4-6/inventory/files.jsonl` — полный file manifest.
- `inventory/jars.jsonl` — JAR catalog.
- `inventory/jar-packages.jsonl` — пакеты и образцы классов.
- `modules/*.md` — отдельная карточка каждого JAR со всеми пакетами.
- `normalized/bundle-files` — launcher/config/readme.
- `ide-plugin-sources` — исходники, восстановленные из source map.
- `lsp-resources` — модели и кодовые примеры из автономного LSP.
- `corpus` — JSONL, SQLite FTS и векторы.

Бандл содержит compiled implementation, поэтому автоматически созданные карточки JAR описывают наблюдаемую структуру, а не выдумывают внутреннюю семантику методов. Для поведенческого вывода используются имена packages/classes, конфигурация, официальная документация, IDE client и исходники Console; такие выводы отмечены как аналитические.

