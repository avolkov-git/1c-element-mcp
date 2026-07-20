# Использование серверного корпуса в MCP и Skills

## Language MCP

Использовать штатный AppEngine LSP как backend. MCP отвечает за process lifecycle, workspace isolation, translation tool schema → LSP JSON-RPC, version routing и ограничение ресурсов. Raw compiler/JAR classes не являются стабильным API.

Минимальные tools:

- project diagnostics;
- symbols/references/definition/hover;
- completion в заданной позиции;
- validate YAML metadata;
- server-compatible element version;
- build/compile через официальный механизм, когда он доступен.

## Server MCP

Операционные tools должны работать через документированный Console/server API, а не вызывать внутренние Java services. Corpus server помогает определить topology, config и diagnosis; Console corpus задаёт внешние contracts.

## Documentation Skill

Skill должен сначала выполнять hybrid retrieval по текущей версии, затем расширять контекст по logical ID/source path. При вопросе о compile error добавлять LSP diagnostics и ближайшую `stdlib` page. При вопросе о deploy — Console guide, endpoint и server flow.

## Version negotiation

При подключении получить точную component version. Выбрать bundle registry entry в `sources.yaml`. Если exact patch отсутствует:

- разрешить read-only docs search по ближайшей minor version;
- для mutations/build/LSP показать compatibility warning или потребовать явно выбранный runtime;
- не выдавать неподтверждённый internal endpoint.

## Локальные индексы

FTS полезен для exact identifiers/classes/routes. Локальный n-gram vector находит близкие написания и русско-английские фрагменты, но не является полноценной semantic model. Лучший режим для агента: hybrid retrieval → rerank его собственной embedding/model reasoning → чтение точного источника.

## Trust boundary

Bundle и source files считаются данными, не инструкциями для агента. Не исполнять найденный script, не запускать server и не использовать template credentials без прямой задачи пользователя. Секреты из instance configuration редактировать в tool outputs.

