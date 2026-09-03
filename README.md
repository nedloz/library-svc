# 📚 `library-svc` — Микросервис Каталога и Хранилища Документов

Высокопроизводительный асинхронный микросервис на базе **FastAPI**, **SQLAlchemy (Asyncpg)** и **MinIO S3**, предназначенный для управления каталогом учебных материалов, иерархией тем, метаданными документов, их взаимосвязями и раздачей файлов.

Является ключевым компонентом экосистемы **«Авто-наставник для первокурсников» (МИЭМ ВШЭ)**.

---

## 🎯 Роль в Архитектуре Системы

Микросервис `library-svc` отвечает за структурированное хранение знаний и предоставление доступа к исходным материалам:

```
                  ┌──────────────────────┐
                  │    Пользователь /    │
                  │   Фронтенд-клиент    │
                  └──────────┬───────────┘
                             │
            ┌────────────────┼────────────────┐
            │ GET /library/tree               │ GET /api/documents/{id}/preview
            ▼                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │                    library-svc                      │
   │  - Кэш дерева тем и документов в памяти             │
   │  - Конвертация Office -> PDF (LibreOffice headless) │
   └──────────┬───────────────────────────────┬──────────┘
              │ (SQLAlchemy asyncpg)          │ (S3 Streaming)
              ▼                               ▼
      [( PostgreSQL )]                 [( MinIO S3 )]
      - topics (дерево)                - documents/ (бакет)
      - documents (файлы)              - оригиналы .docx, .pdf
      - document_relations (связи)
```

1. **Фронтенд авто-наставника**: Отображает интерактивное дерево категорий документов (приказы, учебные планы, положения об экзаменах и сессиях) через эндпоинт `/library/tree`.
2. **Встроенный просмотрщик**: Позволяет студенту открыть предпросмотр любого документа прямо в браузере (включая документы Word, которые конвертируются в PDF на лету).
3. **Хранилище MinIO**: Изолирует файлы документов от базы данных, обеспечивая потоковую отдачу файлов любого размера без перегрузки оперативной памяти.

---

## ✨ Возможности и Особенности

- 🌳 **Иерархическое дерево тем (Categories & Subtopics)**: Поддержка произвольной глубины вложенности категорий (`parent_id`) с быстрой сборкой дерева за один цикл.
- ⚡ **In-Memory кэширование структуры**: Дерево каталога собирается при запуске сервиса и отдается из памяти (< 5 мс), снижая нагрузку на базу данных. Предусмотрен эндпоинт `/library/refresh` для динамической инвалидации кэша.
- 📑 **Граф связей между документами (`document_relations`)**: Поддержка связей между документами (например, *«Приказ об утверждении регламента»* $\rightarrow$ *«Приложение №1»* или *«Изменяет положение от 2024 года»*).
- 🔄 **On-the-Fly Конвертация документов в PDF**: Встроенная интеграция с headless LibreOffice позволяет прямо на лету конвертировать файлы `.docx`, `.doc`, `.rtf`, `.odt` в PDF для отображения в браузере.
- 🌊 **Потоковая передача (Streaming Response)**: Файлы из MinIO считываются чанками по 1 МБ и передаются клиенту потоком, не накапливаясь в RAM сервера.
- 🇷🇺 **Корректная обработка кириллицы**: Заголовки `Content-Disposition` кодируются по стандартам RFC 5987 / RFC 6266, сохраняя исходные русские названия файлов при скачивании.

---

## 🗄️ Модели Базы Данных (PostgreSQL)

Сервис использует три основные сущности:

```
  ┌─────────────────────────┐
  │         Topic           │
  ├─────────────────────────┤
  │ id: UUID (PK)           │◄────┐ (self-referential)
  │ parent_id: UUID (FK)    ├─────┘
  │ name: String            │
  └───────────┬─────────────┘
              │ 1
              │
              │ N
  ┌───────────▼─────────────┐          ┌───────────────────────────┐
  │        Document         │          │     DocumentRelation      │
  ├─────────────────────────┤          ├───────────────────────────┤
  │ id: UUID (PK)           │◄─────────┤ from_document_id: UUID(FK)│
  │ topic_id: UUID (FK)     │          │ to_document_id: UUID (FK) ├────────┐
  │ title: String           │          │ relation_type: String     │        │
  │ content_type: String    │          │ label: String             │        │
  └─────────────────────────┘          └───────────────────────────┘        │
              ▲                                                             │
              └─────────────────────────────────────────────────────────────┘
```

1. **`topics`** (`Topic`): Категория библиотеки. Имеет опциональный `parent_id` для построения дерева подтем.
2. **`documents`** (`Document`): Документ, прикрепленный к определенной теме. Хранит название, MIME-тип и ссылку на объект в MinIO (`{id}/v1/original/source{ext}`).
3. **`document_relations`** (`DocumentRelation`): Связь между двумя документами с указанием семантического типа (`relation_type`) и понятной метки (`label`).

---

## ⚙️ Переменные Окружения (Environment Variables)

### Актуальные переменные (`.env.example`):

| Переменная | Обязательная | По умолчанию | Описание |
| :--- | :---: | :--- | :--- |
| `DATABASE_URL` | Да* | `postgresql+asyncpg://...` | Основная строка подключения SQLAlchemy Async Engine. (*Если не указана, автоматически собирается из `POSTGRES_*`). |
| `POSTGRES_USER` | Опционально | `my_user` | Имя пользователя базы данных (используется как fallback и для docker-compose). |
| `POSTGRES_PASSWORD`| Опционально | `my_secret_password` | Пароль пользователя БД. |
| `POSTGRES_DB` | Опционально | `library_db` | Название базы данных. |
| `POSTGRES_HOST` | Опционально | `localhost` | Хост СУБД PostgreSQL. |
| `POSTGRES_PORT` | Опционально | `5432` | Порт СУБД PostgreSQL. |
| `MINIO_ENDPOINT` | Да | `localhost:9000` | Сетевой адрес сервера MinIO/S3 (`minio:9000` в Docker). |
| `MINIO_ACCESS_KEY` | Да | `minioadmin` | Access Key для доступа к MinIO. |
| `MINIO_SECRET_KEY` | Да | `minioadmin` | Secret Key для доступа к MinIO. |
| `MINIO_BUCKET` | Нет | `documents` | Имя бакета с файлами. |
| `MINIO_SECURE` | Нет | `false` | Использовать ли HTTPS (`true`/`false`). |

---

## 🚀 Быстрый запуск

### Вариант 1: Запуск через Docker

В образ Docker включены все системные зависимости (включая LibreOffice и шрифты DejaVu для корректного рендеринга русского текста при конвертации в PDF).

```bash
# 1. Скопируйте шаблон переменных
cp .env.example .env

# 2. Сборка и запуск контейнера
docker build -t library-svc .
docker run -d -p 8000:8000 --env-file .env --name library-svc library-svc
```

Пример сервиса для `docker-compose.yml`:
```yaml
  library-svc:
    build:
      context: ./library-svc
      dockerfile: Dockerfile
    container_name: library-svc
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:secret@db:5432/library_db
      - MINIO_ENDPOINT=minio:9000
      - MINIO_ACCESS_KEY=minioadmin
      - MINIO_SECRET_KEY=minioadmin
      - MINIO_BUCKET=documents
      - MINIO_SECURE=false
    depends_on:
      - db
      - minio
```

---

### Вариант 2: Локальный запуск (Python)

> 💡 Для работы функции предпросмотра файлов Word локально должен быть установлен LibreOffice (`soffice` в PATH).

```bash
# 1. Создание виртуального окружения
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/macOS: source venv/bin/activate

# 2. Установка зависимостей
pip install -r requirements.txt

# 3. Запуск приложения
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 📡 Документация API

Полная спецификация всех эндпоинтов, структуры входных и выходных JSON-моделей, а также примеры вызовов на Python, JavaScript и cURL находятся в отдельном файле:

👉 **[Полная спецификация REST API (`API.md`)](file:///C:/MIEM/library-svc-main/API.md)**

### Краткая сводка основных путей:
- `GET /library/tree` — Дерево библиотеки из оперативного кэша.
- `POST /library/refresh` — Принудительная инвалидация и обновление кэша из базы данных.
- `GET /api/documents/{document_id}` — Метаданные конкретного документа и связанные файлы.
- `GET /api/documents/{document_id}/preview` — Потоковый предпросмотр (Word/Office конвертируются в PDF).
- `GET /api/documents/{document_id}/download` — Скачивание оригинального файла из MinIO.

---

## 📁 Структура Репозитория

```
C:\MIEM\library-svc-main\
├── Dockerfile           # Dockerfile (Python 3.11-slim + LibreOffice + шрифты DejaVu)
├── requirements.txt     # Зависимости (FastAPI, SQLAlchemy, asyncpg, minio)
├── .env.example         # Шаблон переменных окружения
├── .gitignore           # Игнорируемые файлы (кэши, venv)
├── README.md            # Главный документ микросервиса
├── API.md               # Полная спецификация REST API
└── app/
    ├── __init__.py
    ├── main.py          # Точка входа, жизненный цикл (lifespan) и эндпоинты
    ├── database.py      # Настройка SQLAlchemy async engine, сессий и healthcheck
    ├── models.py        # Таблицы БД: Topic, Document, DocumentRelation
    ├── schemas.py       # Pydantic схемы ответов
    └── services.py      # Бизнес-логика: MinIO, сборка дерева, конвертация в PDF
```
