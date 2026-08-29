# Workflow Runner — заметки для агента

FastAPI-раннер (`main.py`, uvicorn на порту **8766**), который гоняет ComfyUI
API-format workflow'ы на RunPod-подах по CSV: аплоад входов → патч графа →
submit в ComfyUI → poll `/history` → скачивание → S3. Фронт — статика в
`static/`. Подробности и REST API — в `README.md` (секция «Агенту: как гонять
раннер по API»).

## Запуск / рестарт (на поде)

Канонический рестарт. **Убивать только процесс на `:8766`** — `pkill main.py`
НЕЛЬЗЯ (заденет ComfyUI):

```bash
git pull && kill $(ss -tlnp | grep ':8766' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
sleep 2
nohup uv run python main.py > app.log 2>&1 &
sleep 3; tail -5 app.log
```

Рестарт нужен только при изменении **Python-кода** (uvicorn держит модуль в
памяти). Правки `config.yaml` и `jobs/*.json` подхватываются на каждый запуск
рана без рестарта (`load_config()` перечитывает файл с диска).

Рестарт больше не теряет прогон: незавершённые раны раннер добирает сам — см.
«Возобновление после рестарта».

## RunPod: подводные камни

- Раннер = внутренний `:8766`, ComfyUI = `:8188` **или `:8083`** — зависит от
  темплейта, смотри `ps` / `ss -tlnp` на конкретном поде. nginx фронтит внешние
  proxy-порты. **Никогда** не поднимай раннер с `WEB_PORT=8085` — это порт
  nginx, заберёшь его → лягут все внешние порты пода.
- ComfyUI `/history` снаружи через proxy отдаёт **403**. Статус рана смотри
  через раннер: `/api/runs/{id}/status`. Сам раннер ходит в ComfyUI изнутри.
- В темплейте пода после установки requirements обязана быть `pip install
  "av>=16"` (иначе `LoadImage` падает `VideoFrame has no rotation`).

## Где живёт состояние (важно на поде)

Entrypoint пода на **каждый старт контейнера** делает `rm -rf /workspace/<repo>
&& git clone`. Всё, что лежит внутри каталога репозитория, умирает вместе с ним.
Поэтому состояние вынесено наружу:

- `_persistent_root()` в `main.py`: если репозиторий лежит прямо в `/workspace`,
  состояние уходит в `/workspace/<repo>-state` (на поде —
  `/workspace/workflow-runner-state`). Вне пода путь прежний — `<repo>/storage`,
  локальная разработка не меняется.
- Переопределяется через `RUNNER_STORAGE` (весь путь) или
  `RUNNER_PERSIST_ROOT` (какой volume считать персистентным).
- Там же лежат `runs/`, `config.yaml`, `queue.json` и `jobs/` — шаблоны,
  залитые через UI (upload пишет **туда**, а не в `<repo>/jobs`).
- Первый старт после обновления сам переносит `<repo>/storage` в новое место;
  из `<repo>/jobs` копируются только файлы, которых git не знает (сид из
  репозитория не дублируется, иначе он бы навсегда заслонил обновление).
- `_resolve_workflow_template_path` ищет шаблон сначала в персистентном
  `jobs/`, потом в `<repo>/jobs`.

## Возобновление после рестарта

Прогресс прогона и так пишется в `runs/<id>/status.json` на каждое событие —
не хватало того, кто дочитает его после старта. Это делает lifespan-хук
`_startup_resume` → `_resume_after_restart`:

1. `_interrupted_runs()` — прогоны, где `started_at` есть, `finished_at` нет,
   отмены не было и осталась незавершённая джоба.
2. `_rearm_interrupted()` — `running`/`stalled` → `pending` (очередь ComfyUI
   умирает вместе с подом, эти джобы надо слать заново), `done` не трогается.
   `started_at`/`finished_at` обнуляются, иначе `_any_run_active` увидел бы
   вечно активный ран и очередь встала бы.
3. `_wait_for_pods()` — ждём `/system_stats` до 30 мин: после рестарта ComfyUI
   поднимается минутами, и без ожидания раннер сжёг бы все попытки впустую.
4. Прогон уходит в `RUN_QUEUE`; `_completed_indexes()` отдаёт индексы готовых
   джоб в `PodPool.run(..., skip=...)` / `_run_video_pool(..., skip=...)` —
   пересчёта уже готового не будет. Нумерация `idx` остаётся сквозной по CSV.

`RUN_QUEUE` дублируется в `<storage>/queue.json`, так что ждавшие очереди
прогоны тоже переживают рестарт. Выключается через `RUNNER_AUTORESUME=0`.

**Ретраи**: `PodPool` больше не завершает работу по poison pill'ам. Они лежали
в очереди сразу за джобами, а упавшая джоба возвращается в очередь **за** них —
воркеры выходили раньше, чем доходили до повтора, ретрай молча терялся, а
строка навсегда оставалась в статусе `running` при «завершённом» батче. Теперь
конец работы ловит `PodPool._drain()` через `queue.join()`.

## Конфиг

- `workflows.yaml` (в git) — source-of-truth сид. `<storage>/config.yaml` —
  живой рантайм-конфиг, он **вне репозитория** (см. выше). Бутстрап из
  `workflows.yaml` происходит только когда `config.yaml` отсутствует.
- Поэтому новый флоу надо добавлять в **оба** места, а на поде — руками
  дописывать блок в под'овый `config.yaml` (пуш его туда не привезёт).

## Как добавить фото-workflow

1. Сохранить граф как **API Format** (ComfyUI Dev mode), положить в `jobs/`.
2. Прописать маппинг в `workflows.yaml` И в `storage/config.yaml` под
   `workflows.workflows.<key>`. Ключ = значение колонки `workflow` в CSV.
3. Поля маппинга: `ksampler`, `positive_prompt`, `negative_prompt`,
   `load_images` (роли, `main` обязателен первым), `save_images` (список нод),
   `lora_loaders`. `NodeRef` = либо `{node: "12"}`, либо `{node, field, cast}`.

Авто-регистрация (`_autodetect_mapping` в `main.py`) годится только для простых
графов с одним KSampler и одной парой промптов. Для многоэтапных графов / txt2img
маппинг пиши **руками** — авто-детект промахивается.

### Что важно знать про `build_workflow` (`workflow_builder.py`)

- **ksampler: поля `steps/cfg/sampler_name/scheduler/denoise` ВСЕГДА
  перезаписываются из CSV.** Если в CSV их нет → дефолты (`steps=20, cfg=7,
  dpmpp_2m, karras`). Для 4-шаговых Flux2/Krea2-флоу это ломает генерацию —
  задавай значения в каждой строке CSV. NAG-поля (`nag_scale` и т.д.) не трогает.
- Патчится только **первый** сэмплер и **первый** lora-лоадер из маппинга.
  В многоэтапном графе второй KSampler остаётся с параметрами из шаблона.
- lora патчится только если в CSV `lora_name` непустой (иначе шаблонное `None`
  остаётся — «Load Lora» отвергает `""`).
- **txt2img** (нет `LoadImage`): работает автоматически. `WorkflowRegistry.get`
  выкидывает `load_images`-роли, чьей ноды нет в шаблоне, поэтому протёкший из
  `defaults` `main->617` (или затёртый UI-Register `load_images: {}`) сам
  схлопывается в пустой → «картинка не нужна». Ручной `load_images: {}` больше
  не обязателен. `pod_pool._handle` тоже не требует картинку при пустых ролях.
- Несколько `SaveImage` → к `filename_prefix` добавляется суффикс `_n<id>`.
- Больше промптов, чем один pos+neg? Лишние ноды драйвь через `extra_inputs`
  (маппинг `{csv_col: NodeRef}`); ноды с фиксированным текстом оставляй в шаблоне.
- **`set_fields`** (маппинг `{node_id: {field: value}}`) — статические значения
  виджетов, проставляются на билде последними. Для случая «в API-экспорте нет
  обязательного виджета, которого требует нода на поде» (например
  `ResolutionSelector.multiple`) или чтобы запинить значение из конфига без
  правки JSON. Правь конфиг, а не шаблон — переживёт перезалив. Неверный id
  ноды → понятный `KeyError` на билде.
- Модели (UNET/CLIP/VAE/checkpoint) на фото-пути **не трогаются** — берутся из
  шаблона как есть и должны существовать на поде.

## Зарегистрированные Krea2/Flux2 флоу (нужны свои модели+ноды на поде)

| ключ | тип | UNet / CLIP / VAE | кастом-ноды |
|---|---|---|---|
| `krea2_regina_1` | Krea2 img2img-edit | `BigLoveKreaEdit1_bf16` / `qwen3vl_4b_bf16` / `qwen_image_vae` | comfyui-krea2edit |
| `krea2_regina_2` | Krea2 txt2img | `bigLove_kreaedit1` / `qwen3vl_4b_bf16` / `qwen_image_vae` | — |
| `regina_klein_2etapa` | Flux2, два этапа в одном графе | `BigLoveKlein3_bf16` / `qwen_3_8b` / `flux2-vae` | KSamplerWithNAG, ComfyUI_LayerStyle, ReferenceLatent, EmptyFlux2LatentImage |

`regina_klein_2etapa` — оба этапа внутри одного графа (A: img2img → B:
фотореализм через `ReferenceLatent`), чейнинг внутренний. CSV правит только
этап A; промпты и сид этапа B фиксированы в шаблоне.

## Видео vs фото

Разные пути: фото — `csv_loader.py` + `workflow_builder.py` + `pod_pool.py`
(`POST /api/runs`); видео — `video_csv_loader.py` + `video_workflow_builder.py`
(`POST /api/video-runs`). Классификация при регистрации — `is_video_workflow()`.
Раннер выполняет **один граф на строку CSV** — межграфового чейнинга нет.

## Как добавить видео-workflow

1. Сохранить граф как **API Format**, положить в `jobs/`.
2. Зарегистрировать в Settings. Детектор (`detect_video_mapping`) сам разложит
   ноды по каталожным полям (`VIDEO_FIELD_CATALOG`), можно поправить руками.
3. Скачать пример CSV этого флоу и гонять `POST /api/video-runs`.

### Как детектор находит ноду (важно при отладке маппинга)

Проходы по очереди: **title** → **class_type** → **наличие входа**
(`probe_field`) → легаси-дефолтный id. Спека забирает ноду только если поле
реально **резолвится** на ней (`_resolve_field`): сначала `class_fields`
(RandomNoise → `noise_seed`, PrimitiveFloat → `value`), потом dual-слайдер
`Xi`/`Xf`, потом своё поле, потом `value`. Поэтому «правильно звучащий»
заголовок больше не может привести к записи в несуществующий вход — раньше
`Float (Duration)` получал мусорные `Xi`/`Xf`, а длина молча оставалась
шаблонной. Захват — по паре **(нода, поле)**, так что одна нода законно тянет
несколько полей: `MiniMaxH3ReferenceToVideo` → `width` + `height`,
`BasicScheduler` → `steps` + `denoise` + `scheduler`.

Отсюда правило: заголовки нод пиши по-английски и по смыслу. Русский заголовок
детектор не поймёт (ключевые слова английские) — флоу всё равно поедет, но
только через universal-колонки.

### Референсное видео (ref2v-флоу)

Колонка `input_video` (или universal-колонка `LoadVideo`) — имя файла в
`inputs/` рана; раннер **сам заливает его на под** через
`PodClient.upload_file` (эндпоинт `/upload/image` кладёт в `input` любой файл,
не только картинку). Дропзона на `/video` принимает mp4/webm/mov наравне с фото.
Пре-флайт `_missing_inputs` проверяет все файлы джобы, включая universal-колонки
с фото и видео (`_video_job_input_files`), — забытый реф больше не роняет ран
в середине.

### Зарегистрированные видео-флоу

| ключ | тип | модели | кастом-ноды |
|---|---|---|---|
| `mh3_ref2v` (`MH3.json`) | MiniMax H3 ref2v, видео+аудио, 4 реф-фото + реф-видео | `minimax_h3_ref2va_bf16` / `qwen3vl_32b_minimax_h3_bf16` / `minimax_h3_video_vae_fp16` + `minimax_h3_audio_vae_fp32`, лора `minimax_h3_ref2v_turbo_4step_v0.1` | `MiniMaxH3ReferenceToVideo`, `ComfyMathExpression`, `LoadVideo`/`GetVideoComponents`, `CreateVideo`/`SaveVideo`, `ImageStitch`, `VAEDecodeAudio` |

MH3 отдаёт **два** файла на джобу: чистое видео (нода 92) и склейку
side-by-side с оригиналом (нода 164) — `filename_prefix` проставляется обоим.
Длительность живёт на `Float (Duration)` (секунды), кадры считает
`ComfyMathExpression`; ширина/высота — на самой ноде `MiniMax H3 Reference to
Video`.

### MiniMax H3 + авто-описание по рефам (`minimax_h3_qwenvl`, `minimax_h3_grok`)

Два флоу-близнеца в `jobs/`: промпт для генерации **не пишется руками**, а
собирается анализатором из реф-фото и реф-видео. Отличаются только анализатором:

| ключ | анализатор | что нужно |
|---|---|---|
| `minimax_h3_qwenvl` | `AILab_QwenVL_Advanced` ×2, модель `Qwen3-VL-8B-Instruct-FP8` | локально, ключей не надо |
| `minimax_h3_grok` | `GrokImageAnalyzer` + `GrokVideoAnalyzer` + `GrokConfig` | **API-ключ x.ai**; фото/видео уходят во внешний сервис |

Общий стек нод: `MiniMaxH3ReferenceToVideo`, `SAM3_Detect` (+ `CheckpointLoaderSimple`
на `sam3.1_multiplex_fp16`), `SolAttnPatch`, `Video Slice`, `ComfyMathExpression`,
`StringFormat`, `LoadVideo`/`GetVideoComponents`, `CreateVideo`/`SaveVideo`,
`ImageScaleToTotalPixels`. Ключ в `GrokConfig.api_key` **в git не хранится** —
проставляй через `set_fields` в `storage/config.yaml` или руками на поде.

Три `StringFormat` подписаны по роли (определено по связям, не по догадке):

- **`Форматировать текст видео`** (920) — инструкция анализатору **видео**, на вход
  берёт `Positive Prompt (action hint)`;
- **`Форматировать текст фото`** (922) — инструкция анализатору **реф-фото**;
- **`Форматировать текст итог (в MiniMax H3)`** (917) — склейка обоих описаний,
  уходит в `MiniMaxH3ReferenceToVideo`.

Заголовки остальных нод русские — и это нормально: детектор ловит их **по классу**
(`LoadVideo`, `LoadImage`, `RandomNoise`, `BasicScheduler`, `LoraLoaderModelOnly`,
`CheckpointLoaderSimple`). Английский нужен только ноде промпта — у неё класса-
признака нет, поэтому 509 названа `Positive Prompt (action hint)`; слово `prompt`
намеренно больше нигде не встречается, иначе спека `prompt_positive` заберёт
`StringFormat`.

Детект даёт 13 каталожных колонок: `prompt_positive`, `input_image`, `input_video`,
`seed`, `video_length_seconds`, `steps`, `denoise`, `scheduler`, `sampler_name`,
`lora_name`, `lora_strength`, `checkpoint_name`, `diffusion_model_name`.

**Разрешение не задаётся через `video_width`/`video_height`** — эти входы у
`MiniMax H3` заняты проводом от `GetImageSize`, и раннер их не перезаписывает
(запись во вход-провод запрещена). Управляй через universal-колонку
`Resolution (mp)` (нода 924) → `ImageScaleToTotalPixels`.
