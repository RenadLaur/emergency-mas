# Длинный коридор Мангилик Ел

Сценарий заменяет короткий перекрёсточный стенд для визуального просмотра.
Он использует один 4,3–4,6-километровый фрагмент от района Ұлы Дала через
нижний ход у Сығанақ до развязки с Сарайшық.

- 100 синтетических обычных автомобилей;
- скорая находится на удалённой исходной полосе с `t=0` и начинает движение
  после прогрева в `t=120`;
- точки генерации потока находятся вне видимой области мостов;
- Сығанақ моделируется верхним уровнем, Мангилик — нижним уровнем `−6,5 м`;
- Сарайшық моделируется верхним мостом `+6,5 м`;
- высоты относительные и не являются геодезическими измерениями.

```bash
python simulation/sumo/mangilik_corridor/run.py
python simulation/sumo/mangilik_corridor/run.py --gui
```

GUI создаёт обзорную 2D-вкладку и отдельный 3D-вид, который начинает следить
за скорой после прогрева. Старый `mangilik_uly_dala` остаётся коротким
автоматическим тестом управления светофором и не должен использоваться как
демонстрация всего коридора.

## Контролируемый эксперимент

Один и тот же сценарий запускается по умолчанию с `seed=42` в четырёх режимах:

- `normal` — без вмешательства;
- `priority` — только ограниченное продление зелёной фазы для скорой;
- `yielding` — только уступание обычных водителей;
- `assisted` — оба механизма.

Приоритет не выключает светофор и не переводит красный в зелёный. Когда
скорая входит в окно запроса, контроллер ждёт её штатный зелёный. Если
оставшейся длительности уже достаточно для ETA скорой, он не меняет фазу;
иначе продлевает её только до `ETA + 2 с`. SUMO сам продолжает штатную
программу после этой единичной корректировки — контроллер не сбрасывает её.

```bash
# Один режим
python simulation/sumo/mangilik_corridor/run.py --mode normal
python simulation/sumo/mangilik_corridor/run.py --mode priority
python simulation/sumo/mangilik_corridor/run.py --mode yielding
python simulation/sumo/mangilik_corridor/run.py --mode assisted

# Отдельный воспроизводимый прогон, не затирающий baseline-файлы
python simulation/sumo/mangilik_corridor/run.py --mode assisted \
  --seed 43 --civilian-count 160 \
  --output-dir simulation/sumo/mangilik_corridor/output/smoke_seed43_demand160

# Все четыре режима по очереди
python simulation/sumo/mangilik_corridor/run.py --compare

# Пересобрать общий отчёт из уже выполненных четырёх режимов
python simulation/sumo/mangilik_corridor/run.py --assemble-comparison

# Матрица: все режимы для каждого сочетания seed × спрос
python simulation/sumo/mangilik_corridor/run.py \
  --batch-seeds 42,43,44 --batch-civilian-counts 100,160 \
  --output-dir simulation/sumo/mangilik_corridor/output/batches/first_matrix

# Построить CSV, Markdown-отчёт и PNG-графики из готовой матрицы
MPLCONFIGDIR=/private/tmp/emergency-mas-matplotlib \
python simulation/sumo/mangilik_corridor/report.py \
  simulation/sumo/mangilik_corridor/output/batches/first_matrix/batch_summary.json \
  --output-dir simulation/sumo/mangilik_corridor/output/batches/first_matrix/report
```

Результаты сохраняются в `output/run_summary_<mode>.json`, события — в
`output/event_log_<mode>.jsonl`, сводное сравнение — в `output/comparison.json`.
В сводке есть дельты относительно `normal` для времени скорой, времени и
задержки обычного транспорта, near-miss и физических ДТП. Один запуск на seed
не доказывает эффект: для диплома этот опыт нужно повторить на наборе seed и
нескольких уровнях спроса. Серийный запуск создаёт отдельную папку на каждое
сочетание параметров и общий `batch_summary.json` со средним и выборочным
стандартным отклонением всех ключевых метрик и парных дельт. `report.py`
преобразует эту сводку в `summary.csv`, `effects.csv`, `REPORT.md` и четыре
графика, пригодных для дальнейшего анализа и оформления диплома.
