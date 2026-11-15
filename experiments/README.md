# Baseline experiments

Runner запускает воспроизводимые сценарии с обычным транспортом и экстренными
машинами на одной фиксированной сети. Базовые сценарии используют заранее
построенные маршруты; отдельный сценарий Dispatcher создаёт вызовы во время
симуляции и назначает ближайшую свободную машину.

Запуск:

```bash
python experiments/baseline_runner.py experiments/scenarios/astana_100_baseline.json
```

Результат сохраняется в `experiments/results/<run_id>/`: входной сценарий,
маршруты, события, `vehicles.csv`, `calls.csv`, `metrics.json` и метаданные.
Для сравнения `normal` и `off` создаётся копия сценария с другим `run_id` и
`tls_mode`. В режиме `traffic_aware` штатное rerouting-устройство SUMO
пересчитывает маршруты всех машин, в том числе до выезда при динамическом
перекрытии; экстренные машины дополнительно управляются через TraCI. Это не
ML-политика.

`experiment_end_seconds` ограничивает длительность контрольного прогона в
симуляционных секундах. Он нужен для большой карты: события до лимита и все
незавершённые вызовы сохраняются, а в журнал добавляется
`experiment_time_limit_reached`. Для итоговой серии экспериментов лимит должен
быть одинаковым во всех сравниваемых сценариях.

Готовая первая матрица находится в `experiments/scenarios/`:

- `astana_100_baseline.json` — normal + Dijkstra;
- `astana_100_normal_astar.json` — normal + A* с евклидовой эвристикой;
- `astana_100_off_dijkstra.json` — TLS off + Dijkstra;
- `astana_100_normal_traffic_aware.json` — normal + traffic-aware.
- `astana_100_normal_yielding.json` — normal + Dijkstra + реакция обычных водителей на сирену.
- `astana_100_dispatcher_reassign.json` — Dispatcher: два вызова, недоступность
  первой назначенной машины и автоматическое переназначение.
- `astana_100_blue_light.json` — SUMO blue-light + sublane: формирование
  спасательного коридора для экстренных машин.
- `astana_100_incident_reroute.json` — ДТП на фактическом маршруте первой
  экстренной машины, задержанная передача сообщения диспетчеру и reroute.
- `astana_100_incident_response.json` — тот же сквозной случай с реальными
  SUMO-экипажами police и ambulance, созданными из плана реагирования.
- `astana_100_ice.json` — 100 машин на полной сети при гололёде; сценарий
  проверяет уменьшение скорости и сцепления без скрытого изменения seed.
- `astana_100_driver_state.json` — 100 машин в дождь с динамическим состоянием
  водителей экстренных экипажей.
- `astana_100_safety.json` — 100 машин с журналированием TTC и near-miss.
- `astana_100_integrated_mvp.json` — сквозной Inc 13: Dispatcher, дождь,
  состояние водителя, TTC, blue-light и инцидент с задержанной связью и
  дополнительными службами в одном воспроизводимом прогоне.

В Dispatcher-сценарии `calls.csv` показывает все попытки, машину и финальный
статус каждого вызова. Искусственная недоступность нужна только для
проверяемого эксперимента: в `event_log.jsonl` должны появиться события
`unit_unavailable` и вторая `dispatcher_assigned` для того же вызова. Машина,
снятая через TraCI, не считается прибывшей в `vehicles.csv`, даже если SUMO
добавляет для неё техническую запись `tripinfo`.

`emergency_tactics.mode = "blue_light"` включает штатное устройство SUMO
blue-light, умеренное превышение лимита скорости и sublane-модель. В этом
режиме SUMO обрабатывает проезд на красный и уступание по спасательному
коридору. Событие `rescue_lane_forming` означает, что SUMO действительно
сообщил о реакции обычной машины; число таких реакций сохраняется в
`metrics.json`. Это ещё не разрешение свободно ехать по любой встречной
полосе: такие манёвры будут добавляться отдельно через проверяемые зоны сети.

`civilian_yielding` — упрощённая параметрическая модель: при первом
обнаружении экстренной машины в радиусе водитель принимает одно seed-зависимое
решение. При физической возможности модель назначает реакцию, затем снижает
скорость и пытается перейти в крайнюю полосу. Попытка, отказ, невозможность
уступить и применение действия записываются в `event_log.jsonl`. Это не
калиброванная психологическая или юридическая модель поведения.

`weather.condition` принимает `dry`, `rain`, `snow`, `ice` или `fog`.
Погода снижает желаемую скорость, задаёт сцепление посещённых рёбер и при
необходимости ограничивает дальность обнаружения сирены. Все коэффициенты и
границы модели описаны в `docs/WEATHER_MODEL.md`; они являются сценарными
допущениями до отдельной калибровки.

`emergency_driver.enabled = true` создаёт отдельный профиль для каждого
экстренного экипажа и обновляет его состояние на протяжении движения. Результат
содержит `drivers.csv`, события `emergency_driver_registered` и
`emergency_driver_state`, а также итоговые метрики стресса, усталости и реакции.

`safety.enabled = true` включает TTC/near-miss и контроль физических
столкновений SUMO. Порог TTC и период наблюдения задаются в сценарии. Подробнее
о границах метрики — в `docs/SAFETY_MODEL.md`.

`incident` выбирает полосу на маршруте уже движущейся экстренной машины, а не
жёстко заданный нестабильный OSM edge ID. Кандидат должен находиться не ближе
`minimum_lead_distance_meters` и иметь альтернативный путь до назначения. После
активации закрываются все полосы этого ребра; Incident Agent ждёт задержку
обнаружения, затем отправляет отчёт Dispatcher Agent. Только после доставки
команды машине вызывается SUMO reroute. В `traffic_aware` будущие гражданские
машины также получают исправленный маршрут до выезда, поэтому динамическое
перекрытие не делает заранее созданный маршрут недействительным.
В `event_log.jsonl` должны последовательно появиться `incident_activated`,
`incident_detected`, `dispatcher_response_plan`, `response_call_created`,
`dispatcher_assigned`, `dispatcher_reroute_command_sent` и
`emergency_rerouted`. Если включён Dispatcher с `fleet`, каждая строка
`response_call_created` создаёт реальный SUMO-экипаж подходящего типа. Его
назначение идёт на доступное ребро перед закрытым участком (`scene_access_edge`),
а не внутрь перекрытой дороги.

## PPO smoke

Технический цикл обучения запускается командой:

```bash
PYTHONPATH=. python -m ai.training.ppo_smoke \
  --seed 42 --updates 20 --rollout-steps 256 \
  --evaluation-episodes 100 --sumo-evaluation-episodes 3 \
  --output-dir experiments/results/ppo-smoke-seed42
```

Он обучает cooperative PPO на быстрой PettingZoo-среде, сравнивает результат с
uniform-random допустимыми действиями на тех же seed и делает короткую проверку
в SUMO. Полный протокол и ограничения приведены в `docs/PPO_TRAINING.md`.

Флаги `--astana-evaluation-episodes 1 --astana-ordinary-vehicles 100
--astana-episode-calls 3` дополнительно запускают обученную policy на полной
карте Астаны. Один эпизод дорогой и служит только интеграционной проверкой;
статистическая серия должна использовать заранее зафиксированные test seed.
Флаг `--city-finetune-updates` добавляет PPO updates непосредственно на
многовызовном городском backend.

## Единая RL-матрица

`experiments/rl_policy_matrix.py` сравнивает random, nearest, Dijkstra, A*,
traffic-aware и PPO через один городской интерфейс. Вызовы и срочность
фиксируются до первого решения policy. Runner проверяет, что при одном seed все
алгоритмы получили одну последовательность destination edge, и пишет
`matrix.json`, `summary.csv`, `calls.csv`.

```bash
PYTHONPATH=. python -m experiments.rl_policy_matrix \
  --policies random,nearest,dijkstra,astar,traffic_aware,ppo \
  --seeds 4201,4202,4203,4204,4205 \
  --ordinary-vehicles 100 --episode-calls 3 \
  --checkpoint experiments/results/ppo-smoke-seed42/policy.pt \
  --output experiments/results/rl-policy-matrix-test
```
