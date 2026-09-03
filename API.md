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

| Метод | Путь | Описание | Формат ответа |
| :--- | :--- | :--- | :--- |
| `GET` | `/library/tree` | Получение иерархического дерева тем и документов (из кэша) | `application/json` |
| `POST` | `/library/refresh` | Принудительное обновление кэша дерева библиотеки из БД | `application/json` |
| `GET` | `/documents/{id}` | Полные метаданные документа и связанные материалы | `application/json` |
| `GET` | `/documents/{id}/preview` | Потоковый предпросмотр файла (inline) с авто-конвертацией в PDF | `application/pdf`, медиа, текст |
| `GET` | `/documents/{id}/download`| Скачивание оригинального файла документа (attachment) | `application/octet-stream` |

> 💡 **Примечание**: Для эндпоинтов документов поддерживаются как пути `/documents/...`, так и префиксы `/api/documents/...`.

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

### 3.3 `GET /documents/{document_id}` — Метаданные документа

Возвращает расширенную информацию о конкретном документе: размер файла, режим предпросмотра, параметры размещения в MinIO, тему и список связанных документов.

- **Метод**: `GET`
- **Путь**: `/documents/{document_id}` или `/api/documents/{document_id}`
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

### 3.4 `GET /documents/{document_id}/preview` — Предпросмотр документа

Стримит контент файла для отображения прямо в браузере (заголовок `Content-Disposition: inline`).

#### ⚡ Особенность: On-the-fly конвертация Office в PDF:
Если документ является файлом Microsoft Office / OpenOffice (`.doc`, `.docx`, `.rtf`, `.odt`), сервис:
1. Загружает файл из MinIO.
2. В изолированном временном каталоге запускает LibreOffice в headless-режиме (`soffice --headless --convert-to pdf`).
3. Возвращает готовый PDF-поток с заголовком `Content-Type: application/pdf`.
4. Студент видит документ прямо на веб-странице без необходимости скачивать и открывать Word!

- **Метод**: `GET`
- **Путь**: `/documents/{document_id}/preview` или `/api/documents/{document_id}/preview`
- **Коды ответов**:
  - `200 OK` — Потоковая отдача файла (StreamingResponse).
  - `404 Not Found` — Документ не найден в БД или отсутствует объект в MinIO.
  - `415 Unsupported Media Type` — Предпросмотр для формата не поддерживается.
  - `500 Internal Server Error` — Ошибка конвертации или сбой связи с MinIO.

---

### 3.5 `GET /documents/{document_id}/download` — Скачивание файла

Стримит исходный неизмененный файл документа из MinIO в режиме скачивания (заголовок `Content-Disposition: attachment`).

- **Метод**: `GET`
- **Путь**: `/documents/{document_id}/download` или `/api/documents/{document_id}/download`
- **Особенность**: Корректно кодирует русскоязычные имена файлов по стандарту RFC 5987 / RFC 6266 (`filename*=UTF-8''...`), поэтому при скачивании в браузере файл сохраняет оригинальное русское название.

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
