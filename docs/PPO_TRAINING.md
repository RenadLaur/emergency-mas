# PPO smoke-training

## Назначение

`ai/training/ppo_smoke.py` проверяет полный воспроизводимый цикл Inc 15:
PettingZoo-наблюдения, маски действий, сбор rollout, GAE, clipped PPO update,
сохранение весов и оценку в SUMO. Это технический PPO baseline, а не финальный
MAPPO и не основание для вывода о превосходстве RL.

Модель содержит две actor-сети: локальная политика `dispatcher` выбирает
экстренный экипаж, локальная политика `routing` выбирает маршрут. Один
centralized critic получает `environment.state()`. Политики оптимизируются
совместно по общей награде; недопустимые действия исключаются из распределения
до sampling и при детерминированной оценке.

## Запуск

```bash
PYTHONPATH=. python -m ai.training.ppo_smoke \
  --seed 42 \
  --updates 20 \
  --rollout-steps 256 \
  --evaluation-episodes 100 \
  --sumo-evaluation-episodes 3 \
  --astana-evaluation-episodes 1 \
  --astana-ordinary-vehicles 100 \
  --astana-episode-calls 3 \
  --city-finetune-updates 2 \
  --output-dir experiments/results/ppo-smoke-seed42
```

В `policy.pt` сохраняются веса, в `metrics.json` — конфигурация, история
обновлений, оценка обученной политики, uniform-random baseline на тех же seed и
SUMO smoke-оценка. Каталог результатов локальный и исключён из Git.

`--city-finetune-updates N` продолжает обучение той же модели через полный
многовызовный Astana backend. Один update содержит
`--astana-episode-calls` переходов. Малое значение проверяет интеграцию; для
дипломного результата нужны отдельные train/validation seed и существенно
больше переходов.

Честная сравнительная матрица запускается отдельно:

```bash
PYTHONPATH=. python -m experiments.rl_policy_matrix \
  --policies random,nearest,dijkstra,astar,traffic_aware,ppo \
  --seeds 4201,4202,4203,4204,4205 \
  --ordinary-vehicles 100 --episode-calls 3 \
  --checkpoint experiments/results/ppo-smoke-seed42/policy.pt \
  --output experiments/results/rl-policy-matrix-test
```

Runner проверяет идентичность destination edge для каждого seed и сохраняет
`matrix.json`, `summary.csv` и `calls.csv`. В таблицы входят mean/P95/max
времени, успешность, ДТП, near-miss, минимальный TTC и задержка гражданского
транспорта.

PyTorch устанавливается conda-пакетом `pytorch=2.9.1`. На macOS ARM нельзя
заменять его pip-wheel без проверки: wheel со своей копией OpenMP конфликтовал
с OpenMP, который использует conda OpenBLAS. Переменная
`KMP_DUPLICATE_LIB_OK` не используется, потому что она скрывает конфликт и
может приводить к некорректным вычислениям.

## Контрольный результат

Seed 42, 20 обновлений × 256 переходов:

| Оценка | Средняя награда | Успешность | Среднее время |
| --- | ---: | ---: | ---: |
| PPO, 100 synthetic episodes | −8,606 | 99,875% | 832,6 с |
| Random valid action, те же 100 seed | −9,001 | 99,875% | 845,2 с |
| PPO, 3 SUMO smoke episodes | −1,046 | 100% | 59,3 с |

Улучшение награды относительно случайного выбора — 0,395. Малый объём SUMO и
синтетическая обучающая динамика означают, что эти числа подтверждают только
работоспособность интерфейса и обучения.

Честный Astana smoke-run на одинаковом вызове seed 42 выявил domain gap:
Dijkstra прибыл за 1466 с, а synthetic-only PPO выбрал другой экипаж и
traffic-aware маршрут и достиг timeout 2400 с. ДТП не произошло, но вызов PPO
неуспешен. Поэтому старый checkpoint нельзя использовать как дипломное
доказательство качества; он служит стартовой точкой для city fine-tuning.

## Требования к дипломному эксперименту

Многовызовный городской контракт, city fine-tuning и единый matrix runner уже
подключены и проверены короткими вертикальными прогонами. Перед использованием
результата в дипломе ещё необходимо:

1. разделить training, validation и test seed;
2. обучить несколько независимых seed и показать разброс/доверительные интервалы;
3. выполнить полную матрицу на одинаковых test-вызовах с nearest, Dijkstra, A*
   и traffic-aware;
4. отдельно вывести response time, P95, TTC/near-miss, ДТП, успешность и задержку
   обычного транспорта, а не только reward;
5. только после устойчивого PPO baseline переходить к полноценному MAPPO/RLlib.
