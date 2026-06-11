# План интеграции Langfuse для трейсинга эволюции и цепочек

## Обзор

Langfuse позволяет отслеживать все LLM вызовы, включая:
- Промпты и ответы
- Токены и стоимость
- Время выполнения
- Метаданные (experiment_id, iteration, step_number)
- Трассировку цепочек (parent-child связи)

## Архитектура интеграции

### 1. Точки интеграции

#### A. Цепочки (CARL Chains)
**Файл**: `master_api/src/folder_constructor/validate_templates/chain/helper.py`

**Текущая реализация**:
- Используется `OpenAI` client напрямую (строка 50-54)
- `ReasoningChain` из `mmar_carl` выполняет цепочку
- LLM вызовы происходят внутри `ReasoningContext`

**Интеграция**:
- Добавить Langfuse callback в `OpenAI` client
- Передать callback в `ReasoningContext` (если поддерживается)
- Обернуть `chain.execute(ctx)` для трейсинга всей цепочки

#### B. Эволюция (gigaevo-core)
**Файл**: `runner_api/src/services/gigavolve_service.py`

**Текущая реализация**:
- gigaevo-core использует `MultiModelRouter` из `gigaevo.llm.models`
- LLM вызовы происходят через LangChain/LangGraph
- Переменные окружения передаются в subprocess

**Интеграция**:
- Добавить переменные окружения Langfuse в `_setup_experiment_environment`
- Передать Langfuse callback через Hydra config или env vars
- gigaevo-core должен использовать callback при инициализации LLM

## 2. Что можно отслеживать

### Для цепочек (CARL Chains):

1. **Trace уровня эксперимента**:
   - `experiment_id`: ID эксперимента
   - `iteration`: Номер итерации (если есть)
   - `chain_config`: Конфигурация цепочки (JSON)
   - `dataset_size`: Размер датасета
   - `target_column`: Целевая колонка

2. **Trace уровня цепочки**:
   - `chain_id`: Уникальный ID цепочки
   - `base_chain_config`: Базовая конфигурация
   - `frozen_steps`: Замороженные шаги
   - `evolution_mode`: Режим эволюции

3. **Span уровня шага**:
   - `step_number`: Номер шага
   - `step_type`: Тип шага (LLM, TOOL, TRANSFORM, etc.)
   - `step_title`: Название шага
   - `dependencies`: Зависимости шага
   - `input_mapping`: Маппинг входов
   - `execution_time`: Время выполнения
   - `success`: Успешность выполнения

4. **Generation уровня LLM вызова**:
   - `model`: Модель LLM
   - `prompt`: Промпт (system + user messages)
   - `response`: Ответ модели
   - `tokens`: Использование токенов (input/output/total)
   - `cost`: Стоимость запроса
   - `latency`: Задержка запроса
   - `temperature`, `top_p`, `max_tokens`: Параметры модели

### Для эволюции (gigaevo-core):

1. **Trace уровня эксперимента**:
   - `experiment_id`: ID эксперимента
   - `generation`: Номер поколения
   - `program_id`: ID программы (мутанта)
   - `parent_program_ids`: ID родительских программ
   - `mutation_type`: Тип мутации

2. **Trace уровня программы**:
   - `program_id`: Уникальный ID программы
   - `fitness`: Фитнес программы
   - `stage`: Этап выполнения (InsightsStage, LineageStage, etc.)
   - `dag_id`: ID DAG выполнения

3. **Span уровня этапа**:
   - `stage_name`: Название этапа
   - `stage_type`: Тип этапа
   - `execution_time`: Время выполнения
   - `success`: Успешность выполнения

4. **Generation уровня LLM вызова**:
   - Аналогично цепочкам, но с дополнительными метаданными:
   - `agent_type`: Тип агента (InsightsAgent, LineageAgent, etc.)
   - `program_code`: Код программы (для InsightsStage)
   - `context`: Контекст выполнения

## 3. План реализации

### Этап 1: Настройка Langfuse

1. **Добавить зависимости**:
   ```toml
   # pyproject.toml (runner_api и master_api)
   langfuse = "^2.0.0"
   ```

2. **Добавить переменные окружения**:
   ```bash
   # .env
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_HOST=https://cloud.langfuse.com  # или self-hosted
   ```

3. **Создать утилиту для инициализации Langfuse**:
   ```python
   # common/langfuse_helper.py
   from langfuse import Langfuse
   from langfuse.callback import CallbackHandler
   import os
   
   def get_langfuse_client() -> Langfuse:
       return Langfuse(
           secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
           public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
           host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
       )
   
   def get_langfuse_callback(experiment_id: str, **metadata) -> CallbackHandler:
       client = get_langfuse_client()
       return CallbackHandler(
           public_key=client.public_key,
           secret_key=client.secret_key,
           host=client.host,
           session_id=experiment_id,
           user_id=experiment_id,
           tags=["evolution"] if "generation" in metadata else ["chain"],
           metadata=metadata,
       )
   ```

### Этап 2: Интеграция для цепочек

1. **Модифицировать `helper.py`**:
   ```python
   # В _get_llm_api() добавить Langfuse callback
   from common.langfuse_helper import get_langfuse_callback
   
   def _get_llm_api(experiment_id: str = None, **metadata) -> Any:
       # ... существующий код ...
       
       # Добавить Langfuse callback
       callbacks = []
       if experiment_id:
           langfuse_callback = get_langfuse_callback(
               experiment_id=experiment_id,
               component="chain",
               **metadata
           )
           callbacks.append(langfuse_callback)
       
       # Передать callbacks в OpenAI client (если поддерживается)
       # Или обернуть вызовы LLM
   ```

2. **Модифицировать `_run_split()`**:
   ```python
   def _run_split(chain_config: Dict[str, Any], df: pd.DataFrame, 
                  target_column: str, experiment_id: str = None) -> List[Dict[str, Any]]:
       # Создать trace для всей цепочки
       if experiment_id:
           langfuse = get_langfuse_client()
           trace = langfuse.trace(
               name="chain_execution",
               session_id=experiment_id,
               metadata={
                   "chain_config": chain_config,
                   "dataset_size": len(df),
                   "target_column": target_column,
               }
           )
       
       # Для каждого шага создавать span
       for step in chain_config.get("steps", []):
           if experiment_id:
               span = trace.span(
                   name=f"step_{step['number']}",
                   metadata={
                       "step_number": step.get("number"),
                       "step_type": step.get("step_type"),
                       "step_title": step.get("title"),
                   }
               )
       
       # ... существующий код выполнения ...
   ```

### Этап 3: Интеграция для эволюции

1. **Модифицировать `gigavolve_service.py`**:
   ```python
   # В _setup_experiment_environment() добавить Langfuse env vars
   def _setup_experiment_environment(self, ...):
       # ... существующий код ...
       
       # Langfuse configuration
       env["LANGFUSE_SECRET_KEY"] = os.getenv("LANGFUSE_SECRET_KEY", "")
       env["LANGFUSE_PUBLIC_KEY"] = os.getenv("LANGFUSE_PUBLIC_KEY", "")
       env["LANGFUSE_HOST"] = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
       env["LANGFUSE_ENABLED"] = "1" if os.getenv("LANGFUSE_SECRET_KEY") else "0"
       env["EXPERIMENT_ID"] = experiment_id  # Для передачи в gigaevo-core
   ```

2. **Создать патч для gigaevo-core** (в `_patch_engine_diagnostics`):
   ```python
   def _patch_langfuse_integration(self, clone_path: Path) -> None:
       """Add Langfuse callback to gigaevo-core LLM initialization."""
       # Найти файл инициализации LLM (gigaevo/llm/models.py)
       # Добавить Langfuse callback при создании MultiModelRouter
   ```

### Этап 4: Расширенное трейсинг

1. **Трейсинг инструментов (TOOL steps)**:
   - Создавать span для каждого TOOL вызова
   - Логировать входные параметры и результаты
   - Отслеживать время выполнения и ошибки

2. **Трейсинг валидации**:
   - Создавать span для валидации результатов
   - Логировать метрики (accuracy, f1, etc.)
   - Связывать с соответствующими LLM вызовами

3. **Трейсинг мутаций**:
   - Создавать trace для каждой мутации
   - Логировать тип мутации и изменения
   - Связывать с родительскими программами

## 4. Метаданные для фильтрации

### Общие метаданные:
- `experiment_id`: Фильтрация по эксперименту
- `component`: "chain" или "evolution"
- `model`: Модель LLM
- `timestamp`: Время выполнения

### Для цепочек:
- `chain_id`: ID цепочки
- `iteration`: Номер итерации
- `step_number`: Номер шага
- `step_type`: Тип шага

### Для эволюции:
- `generation`: Номер поколения
- `program_id`: ID программы
- `stage_name`: Название этапа
- `agent_type`: Тип агента

## 5. Примеры использования

### Просмотр всех вызовов эксперимента:
```python
from langfuse import Langfuse

langfuse = Langfuse()
traces = langfuse.fetch_traces(
    session_id=experiment_id,
    limit=100
)
```

### Анализ стоимости:
```python
# Получить все generation для эксперимента
generations = langfuse.fetch_generations(
    session_id=experiment_id
)

total_cost = sum(g.cost for g in generations if g.cost)
total_tokens = sum(g.total_tokens for g in generations if g.total_tokens)
```

### Сравнение версий цепочек:
```python
# Получить traces для разных итераций
trace_v1 = langfuse.fetch_trace(trace_id="iteration_1")
trace_v2 = langfuse.fetch_trace(trace_id="iteration_2")

# Сравнить latency, tokens, cost
```

## 6. Приоритеты реализации

1. **Высокий приоритет**:
   - Базовый трейсинг LLM вызовов в цепочках
   - Передача experiment_id и базовых метаданных
   - Трейсинг на уровне шагов цепочки

2. **Средний приоритет**:
   - Интеграция с gigaevo-core для эволюции
   - Трейсинг TOOL шагов
   - Расширенные метаданные

3. **Низкий приоритет**:
   - Визуализация в UI
   - Автоматический анализ производительности
   - Интеграция с метриками экспериментов

## 7. Тестирование

1. **Unit тесты**:
   - Проверка создания Langfuse client
   - Проверка передачи callbacks
   - Проверка метаданных

2. **Integration тесты**:
   - Запуск цепочки с трейсингом
   - Проверка создания traces в Langfuse
   - Проверка корректности метаданных

3. **E2E тесты**:
   - Полный цикл эксперимента с трейсингом
   - Проверка всех уровней трейсинга
   - Проверка производительности (overhead)

## 8. Документация

1. **README обновление**:
   - Инструкции по настройке Langfuse
   - Примеры использования
   - Troubleshooting

2. **API документация**:
   - Описание метаданных
   - Примеры запросов к Langfuse API
   - Интеграция с существующими метриками
