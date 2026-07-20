# Задания, workflows, журналирование и мониторинг

## Почему операции асинхронны

Создание приложения, update, start/stop, relocation, restore, platform change и массовые операции затрагивают несколько внешних компонентов. Console фиксирует intent и прогресс как task, а выполнение разбивает на steps/actions.

`TaskManagement` включает:

- `Tasks`, `DomainTasks`, `GroupTasks` и subjects;
- producers для доменных и групповых задач;
- scheduling, priority и runtime window;
- execution capacity и locks;
- простой step flow;
- полноценный workflow с action/condition/finish nodes;
- retry policies, exception info и user notifications;
- internal managers состояния и выполнения.

## Состояния и переходы

Разные домены имеют свои status enums. Универсальное правило: queued/registered → running/in progress → completed либо failed/cancelled; промежуточные scheduling/creating/cancelling возможны для групп. Нельзя определять terminal state по строке сообщения — использовать enum/поле API.

Workflow action должен быть повторяемым в рамках заданной retry policy либо иметь компенсацию. Subject lock не даёт параллельно менять один объект несовместимыми задачами. Execution capacity ограничивает общий параллелизм.

## Polling клиента

Рекомендуемый MCP pattern:

1. mutation tool возвращает task ID и initial state;
2. `get_task` возвращает status, progress, current step, timestamps и sanitized error;
3. `wait_task` использует bounded exponential backoff и timeout;
4. cancel доступен только для cancellable state;
5. timeout ожидания не отменяет серверную задачу автоматически.

## Logging

`Logging` и доменные managers записывают механизм/операцию. API exception wrappers формируют ответ и логируют error. Correlation полезна по task ID, application/project ID и logging event constant. Секреты, raw tokens, DB credentials и персональные данные должны редактироваться до записи.

## Monitoring

`Monitoring` управляет sources, targets, external target states, collectors/exporters и metrics endpoint. Application registration in monitoring является отдельным workflow. Отсутствие метрик может означать незарегистрированную цель, задержку exporter или недоступный instance — это не всегда падение приложения.

`Incidents` и support integration строятся поверх эксплуатационных сигналов. Автоматическое создание инцидента/тикета должно быть идемпотентным и сохранять обратную ссылку.

## Scheduled jobs

Console использует платформенные scheduled jobs для регулярной синхронизации, cleanup, scheduling и миграционных пересчётов. Ключ задания должен быть стабильным; publication/restart strategy и repeats-on-error определяют конкурентное поведение. При обновлении удалённые job keys следует явно очистить, как это делает корневой migration code.

