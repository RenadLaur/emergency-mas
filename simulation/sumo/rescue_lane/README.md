# Контрольный сценарий спасательного коридора

Это маленькая двухполосная сеть без случайной географии. Медленные обычные
машины создают очередь, а ambulance с устройством SUMO blue-light догоняет её.
Сценарий нужен для проверки, что модель действительно создаёт спасательный
коридор, до применения механики на карте Астаны.

Проверяемый запуск:

```bash
python simulation/sumo/rescue_lane/run.py
```

Ожидаемый результат содержит `rescue_lane_observed: true` и число машин,
реагирующих на ambulance. Для просмотра в SUMO-GUI сначала сгенерируйте сеть,
затем выполните:

```bash
sumo-gui -n simulation/sumo/rescue_lane/rescue_lane.net.xml \
  -r simulation/sumo/rescue_lane/rescue_lane.rou.xml \
  --lateral-resolution 0.4 --device.bluelight.reactiondist 100
```
