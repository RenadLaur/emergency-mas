# Контрольный сценарий ДТП и перерасчёта маршрута

Ambulance начинает движение по верхнему маршруту. На второй секунде
`IncidentManager` моделирует ДТП и закрывает полосу `upper_out_0`. Ambulance
об этом ещё не знает. Через одну секунду датчик фиксирует инцидент, ещё через
секунду Dispatcher получает отчёт, затем через секунду ambulance получает
команду сменить маршрут. Только после этой команды Routing Agent выбирает путь
через `lower_in` и `lower_out`.

```bash
python simulation/sumo/incident_rerouting/run.py
python simulation/sumo/incident_rerouting/run.py --gui
```

Ожидаемые события: `incident_activated`, `incident_detected`,
`message_received`, `dispatcher_reroute_command_sent`, `emergency_rerouted`,
`ambulance_arrived`. В `emergency_rerouted` новый маршрут не должен содержать
`upper_out`. `run_summary.json` также содержит измеренные задержки
`incident_to_dispatcher_seconds`, `dispatcher_to_vehicle_seconds` и
`incident_to_reroute_seconds`.

Текущие задержки по 1 секунде — только техническая демонстрация порядка
сообщений. Они не являются временем работы 112, полиции или Астаны. Правила
замены их на подтверждённые нормативы и сценарные распределения описаны в
`docs/INCIDENT_DATA_SOURCES.md` и `docs/INCIDENT_RESPONSE_MODEL.md`.
