# 📚 library-svc — каталог и хранилище документов

`library-svc` — микросервис проекта «Автонаставник», отвечающий за каталог учебных материалов, метаданные документов, связи между документами, предпросмотр и выдачу файлов из MinIO.

Сервис построен на:

* FastAPI
* SQLAlchemy + asyncpg
* PostgreSQL
* MinIO / S3
* LibreOffice для предпросмотра Office-документов
* Docker Compose

---

## 🎯 Назначение сервиса

`library-svc` отвечает за следующие задачи:

1. построение дерева библиотеки;
2. получение информации о документах;
3. получение связанных документов;
4. предпросмотр файлов;
5. конвертацию Office-документов в PDF;
6. скачивание оригинальных файлов из MinIO;
7. обновление кэша дерева библиотеки.

Файлы документов физически хранятся в MinIO, а PostgreSQL содержит метаданные и информацию, необходимую для поиска объектов.

---

# 🏗️ Архитектура

В общей архитектуре проекта пользователь не обращается к `library-svc` напрямую.

Используется API Gateway на базе nginx.

```text
                        ┌──────────────────┐
                        │     Frontend     │
                        └────────┬─────────┘
                                 │
                                 │ JWT
                                 ▼
                        ┌──────────────────┐
                        │      nginx       │
                        │   API Gateway    │
                        └────────┬─────────┘
                                 │
                         auth_request
                                 │
                                 ▼
                        ┌──────────────────┐
                        │    auth-svc      │
                        │   JWT validation │
                        └────────┬─────────┘
                                 │
                              X-User-Id
                                 │
                                 ▼
                        ┌──────────────────┐
                        │   library-svc    │
                        └───────┬─────┬────┘
                                │     │
                                ▼     ▼
                         PostgreSQL   MinIO
```

`auth-svc` отвечает за проверку JWT.

`nginx` после успешной проверки получает идентификатор пользователя и передаёт его в `library-svc` через заголовок `X-User-Id`.

`library-svc` дополнительно проверяет наличие и корректность `X-User-Id`.

В текущей инфраструктуре `library-svc` не находится в общей сети сервисов и доступен для gateway через отдельную Docker-сеть.

---

# 🔐 Авторизация

## Общий принцип

`library-svc` не разбирает JWT самостоятельно.

Проверка JWT выполняется только в `auth-svc` через nginx.

Схема:

```text
Browser
   │
   │ Authorization: Bearer <access-token>
   ▼
nginx
   │
   │ auth_request /auth/validate
   ▼
auth-svc
   │
   │ JWT valid
   │
   │ X-User-Id
   ▼
nginx
   │
   │ X-User-Id
   ▼
library-svc
```

---

## Проверка X-User-Id

В `library-svc` используется dependency:

`app.auth.get_gateway_user_id`

Она:

1. получает заголовок `X-User-Id`;
2. проверяет, что заголовок присутствует;
3. проверяет, что значение является корректным UUID;
4. передаёт UUID endpoint'у.

Если заголовок отсутствует:

```text
401 Unauthorized
```

Если значение не является UUID:

```text
401 Unauthorized
```

Важно: эта проверка не заменяет JWT validation.

Она защищает сам backend от выполнения endpoint'а без установленного user context.

---

# 🌐 CORS

`library-svc` принимает запросы с любых origin, но не поддерживает credentialed-запросы
браузера (`allow_credentials=False`). Это соответствует CORS-спецификации и безопасно
для текущей схемы авторизации: JWT передаётся в заголовке `Authorization` через gateway,
а не в cookie.

---

# 🌐 Docker Network Isolation

`library-svc` не подключён к общей `app-network`.

Для него используются две отдельные сети.

## library-gateway

В эту сеть входят:

```text
nginx
library-svc
```

Она предназначена для доступа gateway к библиотечному сервису.

## library-backend

В эту сеть входят:

```text
library-svc
PostgreSQL
MinIO
```

Она предназначена для внутренних запросов библиотеки к базе данных и объектному хранилищу.

Схема:

```text
                  library-gateway
             ┌────────────────────────┐
             │                        │
           nginx                 library-svc
                                      │
                                      │
                               library-backend
                                 /          \
                                /            \
                               ▼              ▼
                         PostgreSQL         MinIO
```

Другие контейнеры, находящиеся только в `app-network`, не должны иметь прямого сетевого доступа к `library-svc`.

При этом PostgreSQL и MinIO остаются подключёнными к `app-network`, поэтому существующие сервисы проекта продолжают использовать их без изменений.

---

# 📡 API

## Дерево библиотеки

### GET `/library/tree`

Возвращает текущее дерево библиотеки из memory cache.

Требует авторизации.

Через gateway:

```text
GET /api/library/tree
```

---

## Обновление дерева

### POST `/library/refresh`

Полностью пересобирает дерево библиотеки из PostgreSQL и обновляет memory cache.

Требует авторизации.

Через gateway:

```text
POST /api/library/refresh
```

Endpoint защищён, поэтому пользователь без JWT не может инициировать полную перестройку дерева.

---

## Информация о документе

### GET `/api/documents/{document_id}`

Возвращаемая информация включает:

* идентификатор документа;
* название;
* MIME type;
* описание;
* имя файла;
* bucket MinIO;
* object key;
* размер файла;
* режим предпросмотра;
* URL просмотра;
* URL скачивания;
* тему;
* связанные документы.

Endpoint требует авторизации и доступен только через API-префикс gateway.

---

# 👁️ Предпросмотр документов

### GET `/api/documents/{document_id}/preview`

Предназначен для отображения документа непосредственно в браузере.

В зависимости от типа файла используется один из режимов:

```text
PDF
IMAGE
TEXT
AUDIO
VIDEO
OFFICE
```

---

## Office-документы

Поддерживаются:

```text
.doc
.docx
.rtf
.odt
```

Office-документ сначала загружается из MinIO, после чего передаётся LibreOffice в headless-режиме.

Схема:

```text
MinIO
  ↓
Office file
  ↓
LibreOffice
  ↓
PDF
  ↓
Browser
```

Конвертация выполняется вне event loop FastAPI через thread pool.

Количество одновременных конвертаций ограничивается semaphore, чтобы несколько тяжёлых файлов не привели к чрезмерному потреблению CPU и памяти.

---

## PDF, изображения, текст, аудио и видео

Для этих типов файлов файл сначала полностью подготавливается из MinIO во временный файл.

Только после успешного завершения получения объекта формируется HTTP-ответ.

---

# 📥 Скачивание файлов

### GET `/api/documents/{document_id}/download`

Endpoint возвращает оригинальный файл документа.

Для скачивания и предпросмотра сервис получает только данные, необходимые для
выдачи файла, одним запросом к БД. Связанные документы для этих операций не
запрашиваются: они нужны только в ответе endpoint'а с метаданными документа.

Скачивание проходит через `library-svc`, а не напрямую из MinIO.

Схема:

```text
Browser
   ↓
nginx
   ↓
auth-svc
   ↓
X-User-Id
   ↓
library-svc
   ↓
PostgreSQL
   ↓
MinIO
   ↓
temporary file
   ↓
validation
   ↓
FileResponse
   ↓
Browser
```

---

# 🛡️ Защита от усечённых файлов

Ранее файл из MinIO передавался напрямую через `StreamingResponse`.

При обрыве соединения происходило следующее:

```text
HTTP 200
   ↓
часть файла
   ↓
ошибка MinIO
   ↓
stream завершается
```

В результате пользователь получал повреждённый файл со статусом `200 OK`.

В новой реализации HTTP-ответ не начинается до полного получения объекта.

Алгоритм:

```text
1. Получить metadata MinIO
2. Получить ожидаемый размер
3. Получить ETag
4. Скачать объект во временный файл
5. Проверить количество полученных байт
6. Проверить размер файла на диске
7. Только после этого создать FileResponse
```

Если получение файла завершилось ошибкой:

```text
HTTP 502 Bad Gateway
```

Если фактически полученный размер не совпадает с размером объекта:

```text
HTTP 502 Bad Gateway
```

Таким образом, клиент не получает усечённый файл как успешный HTTP `200`.

---

# 📦 Временные файлы

Для подготовки download/preview используются временные файлы.

Принцип работы:

```text
MinIO
  ↓
/tmp/library-minio-*
  ↓
FileResponse
  ↓
клиент
  ↓
удаление временного файла
```

После завершения выдачи файл удаляется.

Также временный файл удаляется при ошибке его подготовки.

Количество одновременных загрузок ограничивается отдельным semaphore.

Это защищает файловую систему контейнера от большого количества одновременно загружаемых крупных документов.

---

# 🗄️ PostgreSQL

`library-svc` использует PostgreSQL для хранения метаданных библиотеки.

Основные сущности:

```text
topics
documents
document_relations
document_files
```

## topics

Хранят иерархическую структуру библиотеки.

Поддерживается связь:

```text
parent_id → topics.id
```

что позволяет строить дерево любой глубины.

---

## documents

Хранят информацию о документах:

* название;
* тема;
* MIME type;
* описание;
* статус;
* данные, связанные с объектом хранения.

---

## document_relations

Хранят связи между документами.

Например:

```text
Документ A
   │
   └──→ изменяет
             │
             ▼
         Документ B
```

---

## document_files

Содержит информацию о физическом файле:

* документ;
* bucket;
* storage key;
* имя файла;
* MIME type;
* размер;
* другие данные хранения.

`library-svc` использует эту информацию для получения объекта из MinIO.

---

# 🪣 MinIO

MinIO используется как объектное хранилище документов.

Обычно используется bucket:

```text
documents
```

Пример object key:

```text
<document_id>/v1/original/source.pdf
```

или:

```text
<document_id>/v1/original/source.docx
```

Прямой публичный доступ к bucket не используется.

Файлы выдаёт только `library-svc` после прохождения авторизации.

---

# ⚙️ Конфигурация

Основные переменные окружения:

## PostgreSQL

```env
DATABASE_URL=postgresql+asyncpg://user:password@postgres:5432/library_db
```

Дополнительные fallback-параметры:

```env
POSTGRES_USER=my_user
POSTGRES_PASSWORD=my_secret_password
POSTGRES_DB=library_db
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
```

---

## MinIO

```env
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_BUCKET=documents
MINIO_SECURE=false
```

Для production S3/MinIO с HTTPS:

```env
MINIO_SECURE=true
```

---

## Preview

Ограничение времени конвертации:

```env
PREVIEW_CONVERT_TIMEOUT_SEC=120
```

Максимальный размер исходного Office-файла:

```env
PREVIEW_MAX_SOURCE_BYTES=52428800
```

Максимальное количество параллельных конвертаций:

```env
PREVIEW_MAX_CONCURRENCY=2
```

---

## Download

Максимальное количество одновременных загрузок из MinIO во временные файлы:

```env
MINIO_DOWNLOAD_MAX_CONCURRENCY=4
```

При отсутствии переменной используется значение `4`.

---

# 🚀 Запуск

## Docker

В общей архитектуре проекта запуск выполняется из репозитория `pochemuchnic-miem-prj`.

`library-svc` не публикует порт на хост:

```text
library-svc:8000
```

доступен только внутри Docker network.

Внешний доступ осуществляется через nginx.

---

## Локальный запуск

Создать виртуальное окружение:

```bash
python -m venv venv
```

Linux/macOS:

```bash
source venv/bin/activate
```

Windows:

```powershell
venv\Scripts\activate
```

Установить зависимости:

```bash
pip install -r requirements.txt
```

Запустить:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Для предпросмотра Office-файлов должен быть доступен LibreOffice.

В Docker-образе LibreOffice устанавливается автоматически.

---

# 🐳 Docker-образ

Используется:

```text
python:3.11-slim
```

В образ устанавливаются:

```text
LibreOffice
LibreOffice Writer
fontconfig
fonts-dejavu
```

Это необходимо для конвертации Office-документов в PDF и корректного отображения текста.

Приложение запускается на порту:

```text
8000
```

---

# 📁 Структура проекта

```text
library-svc/
│
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
├── API.md
│
└── app/
    ├── __init__.py
    ├── main.py
    ├── auth.py
    ├── database.py
    ├── models.py
    ├── schemas.py
    └── services.py
```

---

# 📌 Назначение файлов

## `app/main.py`

Основная точка входа FastAPI.

Содержит:

* lifecycle приложения;
* endpoints библиотеки;
* endpoints документов;
* preview;
* download;
* авторизационные dependencies;
* подготовку файлов перед выдачей.

---

## `app/auth.py`

Содержит проверку `X-User-Id`, полученного от gateway.

Основная функция:

```text
get_gateway_user_id()
```

---

## `app/database.py`

Настройка SQLAlchemy Async Engine и получение database session.

---

## `app/models.py`

SQLAlchemy-модели базы данных.

---

## `app/schemas.py`

Pydantic-модели API response.

---

## `app/services.py`

Бизнес-логика:

* работа с PostgreSQL;
* работа с MinIO;
* построение дерева библиотеки;
* определение типа файла;
* определение режима preview;
* скачивание объектов;
* Office → PDF.

---

# 🔒 Безопасность

## Пользовательская авторизация

JWT проверяется только через `auth-svc`.

`library-svc` принимает только user context от gateway:

```text
X-User-Id
```

Без него защищённые endpoint'ы возвращают:

```text
401 Unauthorized
```

---

## Защита MinIO

Пользователь не получает прямой URL объекта MinIO.

Файл выдаётся через:

```text
nginx → library-svc → MinIO
```

Это позволяет контролировать доступ перед каждым download/preview.

---

## Защита от обхода gateway

`library-svc` не находится в общей `app-network`.

Доступ к нему предоставляет отдельная:

```text
library-gateway
```

сеть между nginx и `library-svc`.

Подключение `library-svc` к PostgreSQL и MinIO выполняется через:

```text
library-backend
```

---

# ⚠️ Ограничения

## Авторизация

`library-svc` не выполняет самостоятельную JWT validation.

Для production-сценария сервис должен использоваться внутри текущей gateway-архитектуры.

Запуск `library-svc` с опубликованным наружу портом без gateway не следует использовать как production-конфигурацию.

---

## Кэш дерева

Дерево библиотеки хранится в памяти процесса.

При нескольких экземплярах `library-svc` каждый экземпляр имеет собственный cache.

`POST /library/refresh` обновляет cache только конкретного экземпляра.

---

## Изменение документов

Документы и темы могут изменяться через другие сервисы проекта, например `db-svc`.

`library-svc` не получает отдельного события об изменении каждой записи.

Поэтому после изменений дерева может потребоваться:

```text
POST /api/library/refresh
```

---

## CORS

Текущая конфигурация CORS предназначена для работы frontend-клиента.

Для production рекомендуется использовать явный список разрешённых frontend origin вместо универсального `*`.

---

# 🧪 Проверка работоспособности

## Проверка без авторизации

Запрос:

```bash
curl http://library-svc:8000/library/tree
```

без `X-User-Id` должен завершаться:

```text
401 Unauthorized
```

---

## Проверка через gateway

Запрос:

```text
GET /api/library/tree
```

с действующим JWT должен пройти:

```text
Browser
 ↓
nginx
 ↓
auth-svc
 ↓
X-User-Id
 ↓
library-svc
```

и вернуть дерево библиотеки.

---

## Проверка download

```text
GET /api/documents/{document_id}/download
```

При корректном документе и доступном MinIO возвращается полный файл.

---

## Проверка ошибки MinIO

Если MinIO недоступен либо объект не удалось полностью получить:

```text
HTTP 502
```

Усечённый файл со статусом `200` не должен возвращаться.

---

## Проверка preview

Для Office-документа:

```text
DOCX
 ↓
MinIO
 ↓
LibreOffice
 ↓
PDF
 ↓
Browser
```

Для PDF:

```text
PDF
 ↓
MinIO
 ↓
temporary file
 ↓
Browser
```

---

# 🔄 Потоки основных операций

## Получение библиотеки

```text
GET /api/library/tree
        ↓
nginx
        ↓
auth-svc
        ↓
X-User-Id
        ↓
library-svc
        ↓
memory cache
        ↓
response
```

## Обновление библиотеки

```text
POST /api/library/refresh
        ↓
nginx
        ↓
auth-svc
        ↓
X-User-Id
        ↓
library-svc
        ↓
PostgreSQL
        ↓
library_tree_cache
```

## Скачивание файла

```text
GET /api/documents/{id}/download
        ↓
nginx
        ↓
auth-svc
        ↓
library-svc
        ↓
PostgreSQL
        ↓
MinIO
        ↓
temporary file
        ↓
validation
        ↓
FileResponse
        ↓
Browser
```

## Предпросмотр Office

```text
GET /api/documents/{id}/preview
        ↓
auth
        ↓
library-svc
        ↓
MinIO
        ↓
LibreOffice
        ↓
PDF
        ↓
Browser
```

---

# ✅ Изменения безопасности

В новой версии сервиса исправлены следующие проблемы:

### Открытый library API

Ранее endpoint'ы не проверяли пользователя.

Теперь защищённые endpoint'ы требуют `X-User-Id`.

### Незащищённый `/library/refresh`

Ранее любой клиент с доступом к сервису мог инициировать полную перестройку дерева.

Теперь endpoint требует авторизацию.

### Прямой доступ из общей Docker-сети

Ранее `library-svc` находился в общей сети сервисов.

Теперь используется отдельная `library-gateway` network.

### Усечённый файл с HTTP 200

Ранее ошибка чтения MinIO могла произойти после начала `StreamingResponse`.

Теперь файл сначала полностью подготавливается и проверяется, а HTTP `200` формируется только после успешного получения объекта.

---

# 📝 API-поверхность

Защищённые endpoint'ы:

```text
GET  /library/tree
POST /library/refresh

GET  /api/documents/{document_id}

GET  /api/documents/{document_id}/preview

GET  /api/documents/{document_id}/download
```

Через основной gateway используются:

```text
GET  /api/library/tree
POST /api/library/refresh

GET  /api/documents/{document_id}
GET  /api/documents/{document_id}/preview
GET  /api/documents/{document_id}/download
```

---

# 📚 Связь с остальными сервисами проекта

`library-svc` работает как отдельный сервис хранения и выдачи документов.

```text
auth-svc
   │
   └── authentication

db-svc
   │
   └── управление библиотечными данными

library-svc
   │
   ├── чтение PostgreSQL
   ├── чтение MinIO
   ├── preview
   └── download

chat-svc
   │
   └── использует библиотечные данные
       через существующие API проекта

ingest-worker
   │
   └── обрабатывает документы после их
       появления в document_files
```

Основные данные библиотеки и сами файлы не копируются между микросервисами.

---

# 📄 Лицензия

Сервис является частью учебного проекта «Автонаставник для первокурсников» МИЭМ НИУ ВШЭ.
