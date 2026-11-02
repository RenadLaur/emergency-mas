# Контрольный сценарий приоритета светофора

Сценарий использует базовый перекрёсток SUMO и одну ambulance, движущуюся
с запада на восток. Обычная программа сначала даёт этому направлению красный.
Когда ambulance оказывается в 40 м от стоп-линии, `EmergencyPriorityController`
выполняет фиксированную безопасную последовательность:

1. `priority_requested` — все направления получают красный;
2. через 3 секунды `priority_granted` — зелёный получает только направление
   ambulance;
3. после пересечения перекрёстка `priority_restored` — возвращается штатная
   программа светофора.

Проверка без интерфейса:

```bash
python simulation/sumo/traffic_light_priority/run.py
```

Для визуального просмотра:

```bash
python simulation/sumo/traffic_light_priority/run.py --gui
```

После запуска журнал находится в `output/event_log.jsonl`.
