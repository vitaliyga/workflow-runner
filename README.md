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
- запускать несколько pod'ов параллельно;
- сохранять результаты и thumbnails в `storage/runs/<run_id>/`;
- поддерживать строки с несколькими фото одной персоны через
  `input_images` в Builder и CSV.

## Переменные окружения

`.env` рядом с `main.py` загружается автоматически.

| Var | Purpose |
| --- | --- |
| `CONFIG` | путь к основному YAML-конфигу, по умолчанию `storage/config.yaml` |
| `WEB_HOST` | адрес биндинга, по умолчанию `0.0.0.0` |
| `WEB_PORT` | порт веб-интерфейса, по умолчанию `8766` |
| `WEB_ADMIN_TOKEN` | bearer-token для защиты `/api/*` |
| `LOG_LEVEL` | уровень логов, по умолчанию `info` |
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

В `Видео`:

- выбираешь workflow для видео;
- загружаешь `jobs.csv` и входные фото;
- запускаешь прогон как отдельный тип задач, история не смешивается с image-run.

Чтобы runner подхватил поля видео, в `workflows.yaml` для этого workflow
добавь `extra_inputs` с мэппингом на нужные узлы. Пример:

```yaml
workflows:
  video_ltx_v1:
    template: "jobs/video_ltx_v1.json"
    extra_inputs:
      main_lora_on: { node: "6", field: "lora_16.on", cast: bool }
      main_lora_name: { node: "6", field: "lora_16.lora" }
      main_lora_strength: { node: "6", field: "lora_16.strength", cast: float }
      distilled_lora_on: { node: "7", field: "lora_2.on", cast: bool }
      distilled_lora_name: { node: "7", field: "lora_2.lora" }
      distilled_lora_strength: { node: "7", field: "lora_2.strength", cast: float }
      video_length_seconds: { node: "18", field: "Xi", cast: int }
      video_width: { node: "19", field: "Xi", cast: int }
      video_height: { node: "181", field: "Xi", cast: int }
      seed: { node: "125", field: "seed", cast: int }
      sigmas_first_pass: { node: "225", field: "sigmas" }
      sigmas_final_pass: { node: "226", field: "sigmas" }
      prompt_positive: { node: "28", field: "text" }
      prompt_negative: { node: "29", field: "text" }
      cfg_first_pass: { node: "245", field: "cfg", cast: float }
      cfg_final_pass: { node: "255", field: "cfg", cast: float }
      audio_volume_first_pass: { node: "249", field: "volume", cast: float }
      audio_volume_final_pass: { node: "251", field: "volume", cast: float }
      load_checkpoint: { node: "1", field: "ckpt_name" }
      load_diffusion_model: { node: "186", field: "unet_name" }
```

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
| `POST /api/runs` | создать прогон (multipart: csv_file + photos[]) |
| `POST /api/runs/{id}/start` | запустить |
| `POST /api/runs/{id}/cancel` | пометить pending как failed |
| `GET /api/runs/{id}/status` | снимок состояния |
| `GET /api/runs/{id}/events` | SSE-стрим обновлений |
| `GET /api/runs/{id}/file/output/{path}` | отдать сгенерированный файл |
| `GET /api/runs/{id}/file/thumb/{path}` | превью 256px |
| `GET /api/runs` | список прошлых прогонов |
| `POST /api/scenarios/expand` | YAML/JSON → плоский CSV |

## Auth

Если в env выставлен `WEB_ADMIN_TOKEN` — все `/api/*` (кроме SSE
`/events` и `/file/*`) требуют заголовок `Authorization: Bearer ...`.
В браузере фронт берёт токен из `localStorage.admin_token`:

```js
localStorage.setItem("admin_token", "your-token");
```

## Что осталось доделать (Phase 2)

- **Cancel** сейчас только помечает pending как failed; в `PodPool` нет
  кооперативной отмены running-заданий. Нужно добавить `asyncio.Event`
  и проверку в `_worker`.
- **History clean** — нет UI для удаления прошлых runs.
- **Retry failed only** — кнопка перезапуска только упавших.
- **Workflow upload** — сейчас JSON-файлы в `jobs/` правятся только на
  диске; добавить drag&drop в Settings.
- **Multi-image roles** (mask + pose + reference) — поддержаны через
  `input_images` в Builder и автодетект нескольких `LoadImage`-ролей;
  явный маппинг колонок CSV → роли можно добавить позже.
