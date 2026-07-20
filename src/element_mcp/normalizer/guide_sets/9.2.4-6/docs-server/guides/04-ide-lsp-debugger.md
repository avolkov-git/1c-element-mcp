# IDE, LSP и debugger

## Theia IDE

Поставляемая IDE построена на Eclipse Theia и содержит browser/backend bundles, VS Code-compatible plugins и собственные плагины 1C. Большая часть `ide` — vendor dependencies; для понимания интеграции важны `@1c-appengine-plugin`, LSP, debugger и dbeng.

## AppEngine plugin

Plugin версии 9.2.4 регистрирует языки `.yaml`, `.xbsl`, `.xbql`, `.xbsl-expression`, breakpoints, debug adapter и команды metadata/UI/team. Из source map восстановлено 66 внутренних TypeScript-файлов.

`PaasClient` реализует token, applications/projects, assemblies, docs, IDE support, branches/issues/comments/developers, dumps, tasks, user settings и mobile debug. Settings включают `1C.server`, external URI, application/project IDs и client credentials.

## LSP

Автономный fat JAR `com.e1c.g5rt.lsp.server.appengine-9.2.4-1.jar` имеет main class `AppEngineServerMain` и launchers для stdio/socket. Он объединяет:

- XBSL compiler/parser и binary Xtext grammar `Bsl.xtextbin`;
- YAML grammar и модели metadata;
- compile-time modules прикладных объектов/stdlib;
- project model и resource service provider;
- LSP dispatcher и GLSP dispatcher для graphical editors;
- XCore/Ecore models и predefined project examples.

Для MCP предпочтительнее адаптировать штатный LSP, чем реализовывать parser заново. Обёртка должна поддерживать initialize/workspace folders, didOpen/didChange/didClose, diagnostics, hover, definition, references, completion, document/workspace symbols и custom requests, которые обнаруживаются в IDE plugin.

## Custom integration

Plugin отправляет custom LSP notifications/requests, получает `versions/elementVersion`, взаимодействует с GLSP и metadata editors. При разработке MCP custom method names следует извлекать из восстановленных TypeScript и проверять runtime trace — стандартный LSP покрывает не всю платформенную функциональность.

## Debugger

Debugger состоит из adapter/client/protocol/server/agent. Template server debug endpoint — порт 8080; IDE default URI использует WebSocket debug. Приложение должно быть запущено и иметь доступный debug endpoint. Нельзя публиковать debug port наружу без authentication/network controls.

## Версионная связка

LSP/compile-time libraries должны соответствовать platform/project version. Использование LSP из более новой поставки может принять синтаксис/метаданные, недоступные старому серверу. MCP хранит mapping server version → LSP executable/JAR и не смешивает workspaces разных несовместимых версий в одном process.

