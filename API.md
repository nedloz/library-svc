# 📖 Спецификация REST API: `library-svc`

Микросервис `library-svc` управляет каталогом учебных материалов, иерархическим деревом тем, метаданными документов, их связями и отдачей контента из объектного хранилища MinIO/S3 (включая конвертацию офисных файлов в PDF на лету).

---

## 1. Общие сведения

- **Базовый URL (Docker)**: `http://library-svc:8000`
- **Базовый URL (локально)**: `http://localhost:8000`
- **Формат данных API**: `application/json` (кодировка `UTF-8`)
- **CORS**: Включен для всех источников (`*`)
- **OpenAPI / Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

---

## 2. Обзор Эндпоинтов

| Метод | Путь                     | Описание                                                                                            | Формат ответа                   |
| :--------- | :--------------------------- | :---------------------------------------------------------------------------------------------------------- | :------------------------------------------ |
| `GET`    | `/library/tree`            | Получение иерархического дерева тем и документов (из кэша) | `application/json`                        |
| `POST`   | `/library/refresh`         | Принудительное обновление кэша дерева библиотеки из БД      | `application/json`                        |
| `GET`    | `/api/documents/{id}`          | Полные метаданные документа и связанные материалы               | `application/json`                        |
| `GET`    | `/api/documents/{id}/preview`  | Потоковый предпросмотр файла (inline) с авто-конвертацией в PDF | `application/pdf`, медиа, текст |
| `GET`    | `/api/documents/{id}/download` | Скачивание оригинального файла документа (attachment)                  | `application/octet-stream`                |

> 💡 **Примечание**: Эндпоинты документов доступны через API-префикс `/api/documents/...`.

Календарь и калькулятор оценок описаны в разделах [3.6](#36-календарь-apiplanner) и [3.7](#37-калькулятор-оценок-apigrades). Они работают с данными пользователя из подписанного `X-User-Context`: чужие записи недоступны, ответ `404`.

| Метод | Путь | Описание |
| :--- | :--- | :--- |
| `GET` | `/api/planner/notes?from=&to=` | Записи календаря, пересекающие период |
| `POST` | `/api/planner/notes` | Создать запись |
| `PATCH` | `/api/planner/notes/{id}` | Изменить запись частично |
| `DELETE` | `/api/planner/notes/{id}` | Удалить запись (`204`) |
| `GET` | `/api/planner/tags` | Теги пользователя |
| `POST` | `/api/planner/tags` | Создать тег (`409`, если имя занято) |
| `PATCH` | `/api/planner/tags/{id}` | Изменить тег |
| `DELETE` | `/api/planner/tags/{id}` | Удалить тег из всех записей (`204`) |
| `GET` | `/api/grades/templates?course=&program_id=` | Шаблоны предметов текущего учебного года |
| `GET`, `POST` | `/api/grades/my-templates` | Свои шаблоны: список, создать |
| `PATCH`, `DELETE` | `/api/grades/my-templates/{id}` | Изменить, удалить (`204`) |
| `GET`, `PUT` | `/api/grades/scores/{template_id}` | Оценки пользователя по шаблону |

---

## 3. Детальное описание эндпоинтов

### 3.1 `GET /library/tree` — Дерево библиотеки

Возвращает полное дерево каталога: корневые топики, вложенные подтопики, прикрепленные документы и список связанных документов. Запрос отдается из оперативного кэша (быстрый отклик < 5 мс).

- **Метод**: `GET`
- **Путь**: `/library/tree`
- **Коды ответов**:
  - `200 OK` — Дерево успешно получено.
  - `503 Service Unavailable` — Кэш еще не прогрет или БД недоступна.

#### Пример ответа (`List[TopicSchema]`):

```json
[
  {
    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "name": "1 курс: Организационные вопросы",
    "documents": [
      {
        "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "title": "Памятка первокурсника МИЭМ 2026",
        "content_type": "application/pdf",
        "view_url": "/file.html?id=7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "download_url": "/api/documents/7c9e6679-7425-40de-944b-e07fc1f90ae7/download",
        "related_documents": [
          {
            "id": "8d1b2233-1111-40de-944b-e07fc1f90bb8",
            "title": "Схема корпуса на Таллинской",
            "relation_type": "annex",
            "label": "Приложение 1",
            "view_url": "/file.html?id=8d1b2233-1111-40de-944b-e07fc1f90bb8",
            "download_url": "/api/documents/8d1b2233-1111-40de-944b-e07fc1f90bb8/download"
          }
        ]
      }
    ],
    "subtopics": [
      {
        "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "name": "Стипендии и матпомощь",
        "documents": [],
        "subtopics": []
      }
    ]
  }
]
```

---

### 3.2 `POST /library/refresh` — Инвалидация и пересборка кэша

Запрашивает свежие данные из PostgreSQL (`topics`, `documents`, `document_relations`), пересобирает дерево в памяти и обновляет кэш. Вызывается админ-панелью или после завершения работы `ingest-worker`.

- **Метод**: `POST`
- **Путь**: `/library/refresh`
- **Коды ответов**: `200 OK`

#### Пример ответа:

```json
{
  "status": "success",
  "message": "Дерево успешно обновлено!"
}
```

---

### 3.3 `GET /api/documents/{document_id}` — Метаданные документа

Возвращает расширенную информацию о конкретном документе: размер файла, режим предпросмотра, параметры размещения в MinIO, тему и список связанных документов.

- **Метод**: `GET`
- **Путь**: `/api/documents/{document_id}`
- **Параметры пути**:
  - `document_id` (`UUID`, обязательный) — Идентификатор документа.

#### Пример ответа (`DocumentDetailSchema`):

```json
{
  "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "title": "Положение о курсовых работах",
  "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "description": "Регламент выполнения и защиты курсовых работ студентами бакалавриата",
  "filename": "Положение о курсовых работах.docx",
  "bucket_name": "documents",
  "object_key": "7c9e6679-7425-40de-944b-e07fc1f90ae7/v1/original/source.docx",
  "size_bytes": 145820,
  "preview_mode": "pdf",
  "view_url": "/file.html?id=7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "preview_url": "/api/documents/7c9e6679-7425-40de-944b-e07fc1f90ae7/preview",
  "download_url": "/api/documents/7c9e6679-7425-40de-944b-e07fc1f90ae7/download",
  "topic": {
    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "name": "Учебный процесс"
  },
  "related_documents": []
}
```

#### Возможные значения `preview_mode`:

- `"pdf"` — Документ отображается через встроенный просмотрщик PDF браузера (исходные PDF или сконвертированные Word/RTF/ODT).
- `"image"` — Изображение (`image/png`, `image/jpeg` и др.).
- `"text"` — Текстовый файл (`text/plain`, Markdown, HTML, CSV).
- `"audio"` / `"video"` — Мультимедийный контент.
- `"download"` — Файл не подлежит предпросмотру в браузере, только скачивание.

---

### 3.4 `GET /api/documents/{document_id}/preview` — Предпросмотр документа

Стримит контент файла для отображения прямо в браузере (заголовок `Content-Disposition: inline`).

#### ⚡ Особенность: On-the-fly конвертация Office в PDF:

Если документ является файлом Microsoft Office / OpenOffice (`.doc`, `.docx`, `.rtf`, `.odt`), сервис:

1. Загружает файл из MinIO.
2. В изолированном временном каталоге запускает LibreOffice в headless-режиме (`soffice --headless --convert-to pdf`).
3. Возвращает готовый PDF-поток с заголовком `Content-Type: application/pdf`.
4. Студент видит документ прямо на веб-странице без необходимости скачивать и открывать Word!

- **Метод**: `GET`
- **Путь**: `/api/documents/{document_id}/preview`
- **Коды ответов**:
  - `200 OK` — Потоковая отдача файла (StreamingResponse).
  - `404 Not Found` — Документ не найден в БД или отсутствует объект в MinIO.
  - `415 Unsupported Media Type` — Предпросмотр для формата не поддерживается.
  - `500 Internal Server Error` — Ошибка конвертации или сбой связи с MinIO.

---

### 3.5 `GET /api/documents/{document_id}/download` — Скачивание файла

Стримит исходный неизмененный файл документа из MinIO в режиме скачивания (заголовок `Content-Disposition: attachment`).

- **Метод**: `GET`
- **Путь**: `/api/documents/{document_id}/download`
- **Особенность**: Корректно кодирует русскоязычные имена файлов по стандарту RFC 5987 / RFC 6266 (`filename*=UTF-8''...`), поэтому при скачивании в браузере файл сохраняет оригинальное русское название.

---

### 3.6 Календарь (`/api/planner`)

Одна сущность — запись на один или несколько дней. Цвет записи задают теги: порядок `tag_ids` — приоритет, цвет записи — цвет первого тега. Дедлайны отдельно не хранятся: это записи, отфильтрованные по тегам. Даты — строки `YYYY-MM-DD` без времени и часового пояса.

Общие правила: ошибка в поле — `422` (`detail[].loc` указывает поле), чужой или несуществующий `id` — `404`, `201` у создания, `204` у удаления.

#### Запись (`PlannerNoteSchema`)

```json
{
  "id": "5d0c…",
  "title": "Отчёт по БД (ЛР5)",
  "start": "2026-11-04",
  "end": "2026-11-06",
  "description": "",
  "color": null,
  "tag_ids": ["a1…", "b2…"],
  "created_at": "2026-10-27T14:05:00Z",
  "updated_at": "2026-10-27T14:05:00Z"
}
```

| Поле | Правило |
| :--- | :--- |
| `title` | 1–80 символов после обрезки пробелов |
| `start`, `end` | обязательны при создании, `end >= start` |
| `description` | до 500 символов, по умолчанию `""` |
| `color` | `#RRGGBB` или `null` (цвет записи без тегов) |
| `tag_ids` | до 20 своих тегов без повторов; `422` по полю `tag_ids`, если тега нет или он чужой |

- `GET /api/planner/notes?from=&to=` — записи с `start <= to AND end >= from`, по возрастанию `start`. Обе границы необязательны; `to < from` — `422`.
- `POST /api/planner/notes` — тело `{ title, start, end, description?, color?, tag_ids? }`.
- `PATCH /api/planner/notes/{id}` — любые поля из тела создания. Перетаскивание шлёт только `start` и `end`. Новые даты проверяются вместе с прежними, поэтому смена одного `start` позже старого `end` — `422` по полю `end`. `tag_ids` заменяет теги целиком, `color: null` сбрасывает цвет; для остальных полей `null` недопустим.
- `DELETE /api/planner/notes/{id}` — `204`.

#### Теги (`PlannerTagSchema`)

```json
{ "id": "a1…", "name": "Дедлайн", "color": "#FF0000", "position": 0 }
```

- `GET /api/planner/tags` — по возрастанию `position`.
- `POST /api/planner/tags` — тело `{ name, color }`, `name` 1–30 символов. `position` назначается следующим по порядку. Такое имя у пользователя уже есть (без учёта регистра) — `409`.
- `PATCH /api/planner/tags/{id}` — `{ name?, color?, position? }`, дубль имени — `409`.
- `DELETE /api/planner/tags/{id}` — `204`, тег пропадает из всех записей. Оставшиеся теги записи сохраняют порядок.

---

### 3.7 Калькулятор оценок (`/api/grades`)

Шаблон предмета — формула из ПУД: компоненты с долями. Оценки везде по 10-балльной шкале.

#### Компонент шаблона

```json
{ "key": "labs", "label": "Лабораторные работы", "short_label": "ЛР", "weight": 0.25,
  "items": { "count": 8, "aggregate": "mean" } }
```

| Поле | Правило |
| :--- | :--- |
| `key` | латиница, цифры, `_`; уникален в шаблоне, по нему хранятся оценки |
| `label` | полное название; `short_label` — короткое, необязательно |
| `weight` | доля от 0 до 1; сумма по компонентам равна 1 (допуск 0.001) |
| `is_exam` | экзамен, не больше одного на шаблон |
| `min_score` | 0–10 или `null`: ниже порога итог неуд |
| `items` | компонент из нескольких оценок: `count` (1–100), `aggregate` (`mean`, `floor`, `floor_below_4`), `names` необязателен, но если задан, то длиной `count` |

Лишние поля в компоненте не принимаются (`422`).

#### Эндпоинты

- `GET /api/grades/templates?course=&program_id=` — шаблоны предметов **текущего учебного года** (с 1 сентября; `GRADES_ACADEMIC_YEAR` переопределяет). Только активные. С `program_id` — для направления и общие (`program_id IS NULL`), без него — только общие. `course` 1–6, необязателен. Таблицу наполняет парсер ПУД, пока она пуста — ответ `[]`. В ответе `id, subject, course, program_id, academic_year, source_url, rounding, components`.
- `GET /api/grades/my-templates` — свои шаблоны, `POST` — `{ name, components }`, `name` 1–60 символов, успех — `201`, повтор имени (без учёта регистра) — `409`.
- `PATCH /api/grades/my-templates/{id}` — `{ name?, components? }`; `DELETE` — `204`, вместе с шаблоном удаляются оценки по нему.
- `GET /api/grades/scores/{template_id}` — `{ "scores": {...}, "target": 8 }`. Если оценок нет, то `{ "scores": {}, "target": null }`. Шаблон должен быть активным шаблоном предмета или своим, иначе `404`.
- `PUT /api/grades/scores/{template_id}` — заменяет оценки целиком. `scores`: `{ "activity": 8, "labs": [9, 7.5, null], "exam": null }`, значения 0–10 или `null`; ключи только из компонентов шаблона, список — только у компонентов с `items` и не длиннее `count`, иначе `422`. `target` — желаемая оценка 1–10 или `null`.

---

## 4. Примеры интеграции в коде

### 4.1 JavaScript / Frontend (Получение дерева библиотеки)

```javascript
async function loadLibrary() {
  const response = await fetch('http://localhost:8000/library/tree');
  if (!response.ok) {
    throw new Error('Ошибка загрузки библиотеки');
  }
  const tree = await response.json();
  console.log('Категории библиотеки:', tree);
  return tree;
}
```

### 4.2 Python (`httpx` асинхронный — Скачивание документа)

```python
import httpx
import asyncio

async def download_file(doc_id: str, save_path: str):
    url = f"http://localhost:8000/api/documents/{doc_id}/download"
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(save_path, "wb") as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)
    print(f"Файл успешно сохранен в: {save_path}")

if __name__ == "__main__":
    asyncio.run(download_file("7c9e6679-7425-40de-944b-e07fc1f90ae7", "document.pdf"))
```

### 4.3 cURL

```bash
# Получить дерево тем
curl -X GET "http://localhost:8000/library/tree"

# Обновить кэш библиотеки
curl -X POST "http://localhost:8000/library/refresh"

# Получить метаданные документа
curl -X GET "http://localhost:8000/api/documents/7c9e6679-7425-40de-944b-e07fc1f90ae7"

# Скачать файл
curl -OJ "http://localhost:8000/api/documents/7c9e6679-7425-40de-944b-e07fc1f90ae7/download"
```
