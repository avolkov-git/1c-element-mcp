# Проекты, сборки, версии и Team

## Домен проекта

`Projects` хранит проекты разных видов, их настройки, зависимости, releases и assemblies. Отдельные модели описывают kind, availability, assembly kind/format/state/status/access level, required platform version и данные V8 assemblies.

Проект — исходная единица разработки. Сборка (assembly) — загруженный/сформированный версионированный артефакт, пригодный для создания или обновления приложения. Release группирует опубликованные сборки и правила их доступности.

## Загрузка сборки

Основной маршрут — `POST /v2/projects/{ProjectId}/assemblies`. IDE-плагин:

1. собирает исходные каталоги проекта;
2. создаёт ZIP;
3. передаёт тело запроса;
4. добавляет `required-technology-version` и при наличии Git-контекста `commit-id`, `commit-message`, `branch-name`, `modified`;
5. получает созданную версию сборки.

Дополнительно может передаваться `space-id`. MCP должен валидировать project ID, размер/тип архива и явно сообщать, что незакоммиченные изменения помечают assembly как modified.

## Версии и зависимости

API позволяет получать/удалять versions и assemblies. Исходники содержат отображение зависимостей, сборки библиотек, access keys для consumers и required platform version. Перед deployment необходимо проверить:

- совместимость technology/platform version;
- доступность всех библиотек и зависимостей;
- состояние и вид assembly;
- права приложения/пространства на сборку;
- protected/release status перед удалением.

## Team

`Team` реализует repositories, developers, branches, issues, comments, labels, reviewers и review states. Routes проектов и отдельные `/v2/branches`, `/v2/issues` предоставляют операции чтения/создания/изменения/удаления. IDE-плагин использует их для branch, merge, rebase, reset, issues и review workflow.

Связь Git-состояния со сборкой важна: assembly сохраняет commit/branch metadata, но загрузка изменённого workspace не равна Git commit. Для воспроизводимого deployment агент должен предпочитать чистую ветку и фиксированный commit.

## Безопасные MCP tools

Разделять read и mutation tools:

- `list_projects`, `get_project`, `list_assemblies`, `get_assembly`;
- `build_or_upload_assembly` с явным путём и project ID;
- `list_branches`, `list_issues`, `get_issue`;
- удаление project/version/assembly — отдельные destructive tools с точным target и подтверждением на уровне клиента.

Ответ загрузки должен включать project, generated version, commit metadata, required technology version и следующий допустимый шаг. Не запускать update приложения автоматически, если tool был только про сборку.

