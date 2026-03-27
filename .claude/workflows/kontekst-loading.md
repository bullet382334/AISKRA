## Маршрутизация контекста по типу входа

Не читать всё подряд. Загружать только нужное для текущей задачи.

### Вход: «Заказчик ответил на вопросы серии N»
1. `_sostoyaniye.md` — где мы
2. `karta-idej/digest.md` — навигатор (→ нужный digest-файл по теме)
3. `katalog.yaml` — для gating (куда размещать новое)
4. `intervyu/analiz/` → последний файл анализа (предыдущие паттерны)
5. `intervyu/workflow-adaptivnogo-intervyu.md` — как анализировать
6. `.claude/workflows/gating-rules.md` — как размещать новую информацию
7. Если про главный проект → `project/` (соответствующий файл, если есть)

НЕ читать: все прошлые анализы, все карточки, весь project/.

### Вход: «Спонтанное голосовое»
1. `_sostoyaniye.md`
2. `karta-idej/digest.md` → нужный digest-файл
3. `katalog.yaml` — для gating
4. Шаблон по типу из `shablony/`

НЕ читать: intervyu/, analitika/.

### Вход: «Вопрос по собранному»
1. `karta-idej/digest.md` → нужный digest-файл
2. `katalog.yaml` — найти файлы по теме вопроса
3. Конкретные файлы из каталога (не grep по всему проекту)

НЕ читать: sostoyaniye, workflow, шаблоны.

### Вход: «Что спросить дальше?»
1. `karta-idej/digest.md` → `digest-idei.md` (мотивы, гипотезы)
2. `intervyu/analiz/` → последний анализ
3. `intervyu/workflow-adaptivnogo-intervyu.md`

### Вход: «Конкретная задача / расчёт / бизнес-план»
1. `realizaciya/index.md` — что уже исследовано
2. Конкретный файл задачи из `realizaciya/`
3. `karta-idej/digest-proekty.md` — факты и цифры
4. skill `workflow-realizaciya` — как исследовать
5. Если про главный проект → `project/` (соответствующий файл, если есть)

НЕ читать: intervyu/, analitika/, шаблоны.

### Вход: через бот (push)
Контекст уже в промпте (state, digest, katalog.yaml, index.yaml). Не тратить tool calls на чтение этих файлов.
1. Работать с транскрипциями из промпта
2. Применять gating rules из промпта (размещение новой информации)
3. При необходимости — читать конкретные файлы по `katalog.yaml`

НЕ читать: то, что уже в промпте. НЕ трогать realizaciya/ без явного запроса.

### Вход: техническая задача (бот, скрипты)
1. `bot/` → Telegram-бот, транскрипция
2. `notion/` → синхронизация с Notion
3. Соответствующий `.env.example`

НЕ читать: контент-файлы, digest, карточки.
