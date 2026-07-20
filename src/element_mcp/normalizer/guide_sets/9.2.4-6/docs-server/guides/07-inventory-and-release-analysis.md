# Использование inventory и анализ нового релиза

## Полный file manifest

`inventory/files.jsonl` содержит каждый файл, размер и SHA-256. Это основной источник для точного bundle diff. Он позволяет отличить переупаковку от реального изменения и обнаружить добавленные/удалённые native/resource/config файлы.

## JAR catalog

`inventory/jars.jsonl` содержит manifest fields, число classes/resources/packages и функциональную группу. `jar-packages.jsonl` перечисляет каждый пакет и образцы классов. `modules/*.md` удобны для retrieval по имени класса/подсистемы.

Package/class inventory описывает структуру, но не public API compatibility. Для критической интеграции дополнительно нужен bytecode API diff экспортируемых public/protected signatures.

## Сравнение версий

При новой версии анализировать в порядке:

1. launcher/component/Java requirements;
2. instance-template config keys и defaults;
3. добавленные/удалённые JAR и изменившиеся SHA;
4. package/class count крупных first-party modules;
5. Console source diff по logical element IDs;
6. LSP models/grammar/compile-time libraries;
7. IDE plugin API client и custom requests;
8. official docs routes/content SHA;
9. smoke tests build/deploy/debug.

## Интерпретация суффиксов

Версия `9.2.4-6` — сборка bundle/server components. Часть platform modules имеет `9.2.4-1`, а documentation line — `9.2`. Не заменять все три одним значением. Corpus records сохраняют product и source version отдельно.

## Покрытие

`coverage.json` фиксирует ожидаемые counts. Резкое уменьшение файлов, JAR, Console elements или docs pages считается ошибкой ingestion до ручной проверки. Redirect pages официальной документации сохраняются как redirect records, а не молча отбрасываются.

## Производные guides

Автоматическая пересборка не переписывает `guides`. После diff человек/агент должен обновить их, если меняются architecture, default security, lifecycle или contracts. Guide включается в aggregated corpus и получает SHA, поэтому его изменение тоже видно.

