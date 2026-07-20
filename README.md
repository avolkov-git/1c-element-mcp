# 1c-element-mcp

MCP-сервер предоставляет ИИ-агентам контролируемый доступ к нормализованной документации «1С:Предприятие.Элемент».

Текущая версия: **0.1.0**.

## Возможности 0.1.0

- `get_corpus_info` сообщает версии, состав и состояние корпуса.
- `search_docs` выполняет гибридный поиск по языку, Панели управления и серверу.
- `get_document` читает найденный чанк вместе с соседними фрагментами документа.
- `stdio` подключает локальные клиенты.
- Streamable HTTP обслуживает локальную разработку удалённого режима.

Все инструменты первой версии работают только на чтение.

## Требования

- Python 3.11 или новее;
- нормализованный корпус `codex-docs`;
- SQLite с поддержкой FTS5.

## Установка для разработки

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Запуск через stdio

```bash
element-mcp --transport stdio --corpus-path ../codex-docs
```

Пример конфигурации Codex:

```toml
[mcp_servers.element]
command = "/absolute/path/1c-element-mcp/.venv/bin/element-mcp"
args = ["--transport", "stdio", "--corpus-path", "/absolute/path/codex-docs"]
```

## Streamable HTTP

```bash
element-mcp \
  --transport streamable-http \
  --corpus-path ../codex-docs \
  --host 127.0.0.1 \
  --port 8000
```

Endpoint: `http://127.0.0.1:8000/mcp`.

Версия 0.1.0 разрешает HTTP-привязку только к loopback-интерфейсу, потому что сервер ещё не проверяет токены. Для размещения на отдельном хосте потребуется слой аутентификации или закрытый reverse proxy.

## Конфигурация

Параметры командной строки имеют приоритет над переменными окружения.

| Параметр | Переменная | Значение по умолчанию |
|---|---|---|
| `--corpus-path` | `ELEMENT_DOCS_PATH` | соседний каталог `codex-docs`, если найден |
| `--transport` | `ELEMENT_MCP_TRANSPORT` | `stdio` |
| `--host` | `ELEMENT_MCP_HOST` | `127.0.0.1` |
| `--port` | `ELEMENT_MCP_PORT` | `8000` |

## Проверка

```bash
python -m pytest
ruff check .
```

Правила выпуска описаны в [VERSIONING.md](VERSIONING.md), изменения версий — в [CHANGELOG.md](CHANGELOG.md).
