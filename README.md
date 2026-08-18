# Workflow Runner

Веб-обёртка для очереди ComfyUI-задач: загрузка CSV и входных файлов
через браузер, прогресс с превью, S3-зеркало, табличный builder и
настройки workflow в UI.

## Что умеет

- принимать `jobs.csv` и набор входных изображений через браузер;
- собирать `jobs.csv` из табличного builder на странице `Scenarios`;
- запускать отдельные video-прогоны на странице `/video`;
- регистрировать workflow JSON из `jobs/` и хранить mapping в
  `storage/config.yaml`;
- запускать несколько pod'ов параллельно через общую последовательную очередь;
- реально отменять генерацию (стоп воркеров + `/interrupt` в ComfyUI);
- сохранять результаты и thumbnails в `storage/runs/<run_id>/`, зеркалить в S3;
- отдавать «универсальный» CSV со всеми входами нод (включая lora-слоты Power
  Lora Loader как JSON) — `universal_csv`;
- поддерживать строки с несколькими фото одной персоны через
  `input_images` в Builder и CSV.

## Агенту: как гонять раннер по API

Раннер сам делает весь конвейер ComfyUI (upload кадров → патч графа → submit →
ожидание `/history` → скачивание результата). Агент **не должен** дёргать
ComfyUI `/prompt` напрямую — только REST раннера. Базовый URL = адрес раннера
(на RunPod это внешний proxy-порт, см. ниже), при заданном `WEB_ADMIN_TOKEN`
добавляй `Authorization: Bearer <token>`.

Полная эмуляция video-CSV (проверено end-to-end):

```bash
R=https://<pod>-8085.proxy.runpod.net          # URL раннера, не ComfyUI

# 1. убедиться, что флоу зарегистрирован (Settings → ⚙ CSV-поля → Register)
curl -s "$R/api/workflows" | jq '.[] | {name, type}'

# 2. получить шаблон CSV. universal_csv = все входы нод (колонки <title>[.field]).
#    Минимальный CSV безопаснее full-dump: берёшь только нужные колонки,
#    остальное держит дефолт шаблона. (bool-ячейки безопасны: "false"/"False"
#    читаются как false, а не как непустая строка.)
curl -s "$R/api/workflows/<key>/universal_csv" -o sample.csv

# 3. одна строка: workflow + girl + только меняемые колонки (напр. 4 кадра)
printf 'workflow,girl,Загрузить изображение,Load Last Frame,Load KF 1/3,Load KF 2/3\n<key>,test,a.png,b.png,a.png,b.png\n' > job.csv

# 4. создать video-прогон (csv + фото). Фото с тем же именем, что в колонках.
curl -s -X POST "$R/api/video-runs" \
     -F 'csv_file=@job.csv;type=text/csv' \
     -F 'photos=@a.png' -F 'photos=@b.png'        # → {"run_id": "...", "missing_inputs": []}

# 5. старт (ставит в общую последовательную очередь)
curl -s -X POST "$R/api/video-runs/<run_id>/start"   # → {"ok": true}

# 6. опрос статуса (НЕ ComfyUI /history — его проксирует 403)
curl -s "$R/api/runs/<run_id>/status" | jq '.jobs[0] | {status, duration, files, error, extra}'
```

`status` доходит до `done`, `files[0]` = путь результата по схеме
`<ДД-ММ>/<HHMMSS_workflow>/<girl>[/params]/<girl>_<idx>_seed<seed>_…`.
Image-прогон идентичен, только `POST /api/runs` вместо `/api/video-runs`.

> **Файловые колонки** грузятся автоматически: значение ячейки = имя файла,
> залитого с прогоном — и для фото (`LoadImage`), и для референсного **видео**
> (`LoadVideo`, колонка `input_video`); mp4/webm/mov отправляются через тот же
> `-F photos=@clip.mp4`. «Дружелюбные» поля (`input_image`, `input_image_last`,
> `input_video`, `seed`, `steps`, `denoise`, `scheduler`, `sampler_name`,
> `video_width`, `video_height`, `video_length_seconds`, `lora_name`,
> `lora_strength`, …) и универсальные колонки `<title>` можно мешать. Power Lora
> Loader отдаётся как JSON-ячейка `{"on":true,"lora":"…","strength":0.5}`.
> Пустая ячейка = «оставить значение шаблона».

### RunPod: подводные камни (важно для агентов)

- **Раннер = внутренний порт `8766`**, ComfyUI = внутренний `:8083`/`:8188`.
  nginx на поде фронтит внешние proxy-порты (`8082` ComfyUI, `8085` раннер, …).
- **Никогда** не поднимай раннер с `WEB_PORT=8085` — это порт nginx, заберёшь
  его → лягут все внешние порты пода. Рестарт раннера = убить процесс на `:8766`,
  **не** `pkill main.py` (убьёт ComfyUI).
- ComfyUI `/history` снаружи через proxy отдаёт **403** — внешний наблюдатель
  не прочитает результат напрямую. Поэтому статус смотри через раннер
  (`/api/runs/{id}/status`), а сам раннер должен ходить в ComfyUI изнутри пода.

## Переменные окружения

`.env` рядом с `main.py` загружается автоматически.

| Var | Purpose |
| --- | --- |
| `CONFIG` | путь к основному YAML-конфигу, по умолчанию `storage/config.yaml` |
| `WEB_HOST` | адрес биндинга, по умолчанию `0.0.0.0` |
| `WEB_PORT` | порт веб-интерфейса, по умолчанию `8766` |
| `WEB_ADMIN_TOKEN` | bearer-token для защиты `/api/*` |
| `LOG_LEVEL` | уровень логов, по умолчанию `info` |
| `VIDEO_WAIT_TIMEOUT_S` | сколько ждать готовности video-промта в ComfyUI, по умолчанию `1800` (30 мин) |
| `S3_BUCKET` | bucket для зеркалирования результатов |
| `S3_PREFIX` | prefix внутри bucket, по умолчанию `test/runner/` |
| `S3_REGION` | регион S3/R2, по умолчанию `us-east-1` |
| `S3_ENDPOINT_URL` | endpoint для R2/MinIO/совместимых хранилищ |

Если `S3_BUCKET` не задан, runner попробует взять bucket из
`S3_MODELS_BASE` или `S3_NODES_BASE`.

## Развёртывание на RunPod

Ниже рабочая схема для RunPod, если у тебя есть GPU pod с
постоянным volume. Идея простая: ComfyUI остаётся отдельным сервисом на
`8188`, а этот web-runner запускается рядом и ходит в ComfyUI по HTTP.

### 1. Подготовь pod

- возьми GPU pod с persistent storage;
- если нужна доступность из браузера, пробрось порт `8766` для этого
  приложения;
- для ComfyUI обычно используют `8188` внутри pod'а.

Если ComfyUI уже есть в шаблоне RunPod, достаточно знать его URL. Если
нет, подними ComfyUI отдельно в том же pod'е или в соседнем pod'е.

### 2. Забери репозиторий и поставь зависимости

```bash
cd /workspace
git clone https://github.com/vitaliyga/workflow-runner.git
cd workflow-runner
pip install -e .
```

Если у тебя уже настроен `uv`, можно использовать его вместо `pip`:

```bash
uv venv
uv pip install -e .
```

### 3. Настрой `.env`

Минимальный набор:

```bash
cat > .env <<EOF
WEB_HOST=0.0.0.0
WEB_PORT=8766
WEB_ADMIN_TOKEN=choose-a-long-random-token
EOF
```

S3/R2 предзаполняются из env при наличии. Если нужно переопределить
их вручную:

```bash
S3_BUCKET=...
S3_PREFIX=...
S3_REGION=...
S3_ENDPOINT_URL=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

### 4. Запусти ComfyUI

Если ComfyUI уже крутится в том же pod'е, он должен отвечать по
`http://127.0.0.1:8188`.

Если стартуешь вручную, типичный вариант такой:

```bash
cd /workspace/ComfyUI
python main.py --listen 0.0.0.0 --port 8188
```

Для этого приложения важно только, чтобы ComfyUI был доступен по HTTP
изнутри pod'а. В Settings позже добавишь его URL.

### 5. Запусти web-runner

```bash
cd /workspace/workflow-runner
python main.py
```

После старта интерфейс будет на `http://0.0.0.0:8766`.
Если порт проброшен наружу RunPod'ом, открывай именно его.

### 6. Настрой UI

В `Settings`:

- добавь ComfyUI host с URL `http://127.0.0.1:8188` или другим
  внутренним адресом;
- при необходимости добавь несколько pod'ов в пул;
- загрузи workflow JSON в `jobs/`, нажми `Register`, затем при желании
  проверь/подправь mapping;
- для public-репо используй нейтральные имена workflow и моделей.

В `Scenarios`:

- выбираешь workflow из списка;
- поля `Model` и параметры подставляются из mapping;
- одна строка таблицы = один subject;
- несколько фото для одной строки указываются в `input_images` через
  `|`, например `alice_1.png | alice_2.png`.

### Видео-флоу регистрируются как картиночные

Видео работает по той же схеме, что и фото: **загрузил → зарегистрировал →
имя флоу пишешь в CSV**. Никаких зашитых ID нод.

1. **Settings → загрузить workflow JSON** (API-формат). Файл попадёт в `jobs/`.
2. У файла нажми **⚙ CSV-поля**. Раннер сам распознаёт видео-флоу и показывает
   список известных полей (промт, фото, длина/ширина/высота, sigmas, cfg,
   громкость, checkpoint, diffusion model, lora). Распознавание идёт по
   заголовкам нод (`_meta.title`), затем по `class_type`.
3. **Отметь галочками** только те поля, которые хочешь задавать из CSV.
   Снятые поля держат своё значение из шаблона. Номер ноды при желании правится.
4. **💾 Зарегистрировать выбранные** — маппинг сохраняется в
   `storage/config.yaml` под ключом = имя файла.
5. **📄 Скачать пример CSV** — готовый шаблон ровно с выбранными колонками
   (плюс `workflow,scenario,girl`), заполненный текущими значениями из графа.

Дальше в `Видео`:

- загружаешь свой `jobs.csv` и входные фото;
- в колонке **`workflow` указываешь имя зарегистрированного флоу** — раннер
  возьмёт именно его шаблон (можно мешать разные флоу в одном CSV);
- запускаешь прогон как отдельный тип задач, история не смешивается с image-run.

> Поля, которых нет в CSV (или с пустым значением), не трогаются — нода
> сохраняет дефолт из шаблона. Если ключ из колонки `workflow` не
> зарегистрирован, прогон не стартует с понятной ошибкой.

> **Seed.** Колонка `seed` опциональна. Если её нет, ячейка пустая или в ней
> стоит `0`, `-1`, `random` — раннер подставляет **случайный seed на каждую
> строку** (0…2³²−1), так что каждый кадр получает своё зерно. Конкретное число
> используется как есть — фиксированно и воспроизводимо. Сгенерированный seed
> пишется в лог и в имя файла результата (`…_seed<seed>`), поэтому удачный кадр
> всегда можно повторить. Работает одинаково для image- и video-флоу.

Тонкая настройка маппинга доступна и через YAML на Settings
(`workflows.<key>.video_fields`), но обычно хватает кнопки **⚙ CSV-поля**.

### 7. Проверка запуска

1. На главной странице загрузи `jobs.csv`.
2. Добавь все нужные входные фото в блок `входные фото`.
3. Нажми `Создать прогон`.
4. Если `missing_inputs` пустой, нажми `▶ Запустить генерацию`.

### Практика для RunPod

- `storage/` держи на persistent volume, иначе потеряешь конфиг и runs;
- `WEB_ADMIN_TOKEN` ставь всегда, если UI доступен не только тебе;
- `jobs/` и `workflows.yaml` можно хранить в git, а runtime-данные
  (`storage/`, `runs/`) не коммитить;
- если workflow использует несколько `LoadImage`, Builder ожидает такое
  же число фото в `input_images`.

## Локальный запуск

```bash
uv venv
uv pip install -e .
uv run python main.py
```

Откроется на `http://0.0.0.0:8766`.

## Структура

```
main.py                # FastAPI + run lifecycle
static/
  index.html           # главный экран (upload + progress)
  settings.html        # pods / S3 / workflows mapping
  scenarios.html       # табличный builder jobs.csv
  video.html           # отдельный video-run экран
  app.css, app.js
storage/               # runtime, в .gitignore
  config.yaml          # активный конфиг
  runs/<run_id>/
    jobs.csv
    inputs/
    outputs/
    thumbs/
    status.json
jobs/                  # шаблоны workflow JSON (читаются по путям из config)
```

## Endpoints (REST + SSE)

| Метод | Путь | Что |
|---|---|---|
| `GET /` | главный экран | |
| `GET /settings` | страница настроек | |
| `GET /scenarios` | табличный builder jobs.csv | |
| `GET /video` | отдельный video-run экран | |
| `GET /api/config` | прочитать активный конфиг (pods + workflows + s3) |
| `PUT /api/config` | сохранить конфиг |
| `POST /api/pods/test` | пинг pod'а + диагностика missing-нод |
| `GET /api/workflows` | список зарегистрированных флоу (с типом image/video) |
| `POST /api/workflows/upload` | загрузить workflow JSON в `jobs/` |
| `GET /api/workflows/{name}/detect` | автодетект CSV-полей (для UI ⚙ CSV-поля) |
| `POST /api/workflows/{name}/register` | сохранить маппинг полей в config |
| `GET /api/workflows/{name}/sample_csv` | пример CSV по выбранным «дружелюбным» полям |
| `GET /api/workflows/{name}/universal_csv` | пример CSV со **всеми** редактируемыми входами нод (колонки `<title>[.field]`, lora-слоты — JSON) |
| `POST /api/runs` | создать **image**-прогон (multipart: `csv_file` + `photos[]`) |
| `POST /api/video-runs` | создать **video**-прогон (multipart: `csv_file` + `photos[]`) |
| `POST /api/runs/{id}/inputs` | дозалить фото в существующий прогон |
| `POST /api/runs/{id}/start` / `POST /api/video-runs/{id}/start` | поставить в общую очередь и запустить |
| `POST /api/runs/{id}/cancel` | реальная отмена: стоп воркеров + `/interrupt` в ComfyUI, pending/running → failed |
| `GET /api/runs/{id}/status` | снимок состояния (включая `jobs[].files`, `duration`, `seed`, `extra`) |
| `GET /api/runs/{id}/events` | SSE-стрим обновлений |
| `GET /api/runs/{id}/file/output/{path}` | отдать сгенерированный файл |
| `GET /api/runs/{id}/file/thumb/{path}` | превью 256px |
| `GET /api/runs/{id}/archive` / `GET /api/video-runs/{id}/archive` | zip всего прогона |
| `GET /api/runs` | список прошлых прогонов |
| `DELETE /api/runs/{id}` | удалить прогон |
| `POST /api/scenarios/expand` | YAML/JSON → плоский CSV |

## Auth

Если в env выставлен `WEB_ADMIN_TOKEN` — все `/api/*` (кроме SSE
`/events` и `/file/*`) требуют заголовок `Authorization: Bearer ...`.
В браузере фронт берёт токен из `localStorage.admin_token`:

```js
localStorage.setItem("admin_token", "your-token");
```

## Что осталось доделать (Phase 2)

- **History clean** — нет UI для удаления прошлых runs.
- **Retry failed only** — кнопка перезапуска только упавших.
- **Workflow upload** — сейчас JSON-файлы в `jobs/` правятся только на
  диске; добавить drag&drop в Settings.
- **Multi-image roles** (mask + pose + reference) — поддержаны через
  `input_images` в Builder и автодетект нескольких `LoadImage`-ролей;
  явный маппинг колонок CSV → роли можно добавить позже.
