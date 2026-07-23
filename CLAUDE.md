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
памяти). Правки `storage/config.yaml` и `jobs/*.json` подхватываются на каждый
запуск рана без рестарта (`load_config()` перечитывает файл с диска).

## RunPod: подводные камни

- Раннер = внутренний `:8766`, ComfyUI = `:8188`. nginx фронтит внешние
  proxy-порты. **Никогда** не поднимай раннер с `WEB_PORT=8085` — это порт
  nginx, заберёшь его → лягут все внешние порты пода.
- ComfyUI `/history` снаружи через proxy отдаёт **403**. Статус рана смотри
  через раннер: `/api/runs/{id}/status`. Сам раннер ходит в ComfyUI изнутри.
- В темплейте пода после установки requirements обязана быть `pip install
  "av>=16"` (иначе `LoadImage` падает `VideoFrame has no rotation`).

## Конфиг

- `workflows.yaml` (в git) — source-of-truth сид. `storage/config.yaml` — живой
  рантайм-конфиг, и **он в `.gitignore`**. Бутстрап из `workflows.yaml` в
  `config.yaml` происходит только когда `config.yaml` отсутствует.
- Поэтому новый флоу надо добавлять в **оба** места, а на поде — руками
  дописывать блок в под'овый `storage/config.yaml` (пуш его туда не привезёт).

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
- **txt2img** (нет `LoadImage`): в маппинге `load_images: {}`. Иначе протечёт
  дефолтный `load_images` и билдер упадёт на несовпадении числа картинок.
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
