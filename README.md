## APC UPS Prometheus Exporter

Exporter на python для мониторинга UPS от **APC** через `apcaccess` с публикацией метрик в Prometheus и опциональной интеграцией с MQTT.

Проект рассчитан прежде всего на сценарий, когда `apcupsd` уже установлен и обеспечивает связь с UPS (как вариант по USB), а exporter используется как дополнительный инструмент мониторинга и интеграции.

### Возможности
- Получение состояния UPS через `apcaccess status`;
- Публикация метрик для Prometheus;
- Периодический polling UPS;
- Внеочередной polling после событий `onbattery` / `offbattery`;
- Контроль состояния связи с UPS;
- Обработка `STATUS=COMMLOST`;
- Расчёт возраста последних успешно полученных данных;
- HTTP endpoints: `/metrics` | `/health`
- Получение событий от apcupsd через MQTT (через отдельный срипт - описание ниже);
- Возможность отключить MQTT и использовать exporter только как Prometheus exporter;

Exporter намеренно не пытается экспортировать весь вывод `apcaccess status`. Используется лишь небольшой набор наиболее полезных параметров:
- состояние UPS;
- входное напряжение;
- нагрузка;
- заряд батареи;
- расчётное оставшееся время;
- суммарное время работы от батареи.

---

### Архитектура

[СХЕМА](images/scheme.jpg)

Exporter выполняет первичный опрос UPS сразу после запуска, затем выполняет polling с заданным интервалом (по умолчанию 300s / 5m).

*MQTT* используется как дополнительный канал интеграции (например, для [IoT контроллера УД](images/custom_homed_apc_scr.png)) и не является обязательным для работы exporter.
При отключённом MQTT, exporter продолжает самостоятельно опрашивать UPS через apcaccess.

При успешном опросе обновляются Prometheus-метрики и, если MQTT включён, публикуется актуальный *retained state*.

**Пример состояния:**
```
{
  "status": "ONLINE",
  "linev": 230.0,
  "load": 20.0,
  "bcharge": 100.0,
  "timeleft": 25.1,
  "cumonbatt": 674.0
}
```
**MQTT events** \
Exporter может подписываться на события, публикуемые `apcupsd:` \
`ups/deb2bot/event`

Поддерживаются события: `onbattery`, `offbattery`, `commfailure`, `commok`

События `onbattery` и `offbattery` запускают внеочередной опрос UPS через apcaccess. \
Таким образом, exporter не полагается только на MQTT-событие, а дополнительно проверяет фактическое состояние UPS.

**Обработка COMMLOST** \
Если apcaccess возвращает: `STATUS: COMMLOST`, exporter считает связь с UPS потерянной:
`ups_communication_ok = 0` 

При этом, последние успешно полученные значения измерений не перезаписываются. Возраст данных продолжает увеличиваться. Это позволяет отличать старые данные от актуальных.

При включённом MQTT дополнительно публикуется:
```
{
  "status": "COMMLOST",
  "linev": 0,
  "load": 0,
  "bcharge": 0,
  "timeleft": 0,
  "cumonbatt": 0
}
```
Нулевые значения в данном случае означают **недоступность данных**, а не реальные измерения UPS.

---

### Prometheus metrics
- `ups_exporter_up` (состояние exporter);
- `ups_communication_ok` (доступность связи с UPS);
- `ups_on_battery` (работа UPS от батареи);
- `ups_input_voltage_volts` (входное напряжение);
- `ups_load_percent` (текущая нагрузка);
- `ups_battery_charge_percent` (заряд батареи);
- `ups_timeleft_seconds` (расчётное оставшееся время работы);
- `ups_cumulative_on_battery_seconds` (суммарное время работы от батареи);
- `ups_last_update_timestamp` (Unix timestamp последнего успешного опроса);
- `ups_data_age_seconds` (возраст последних успешных данных);
- `ups_failed_polls_total` (количество неудачных опросов);

---

### HTTP endpoints
Exporter запускает HTTP-сервер, по умолчанию на порту 8011.

**Metrics**
```
http://localhost:8011/metrics
```
**Health**
```
http://localhost:8011/health
```
Возвращает: `ОК` при работающем exporter (предназначен для внешнего контроля доступности самого процесса exporter).

---

### MQTT topics
По умолчанию используются:
```
ups/deb2bot/event
ups/deb2bot/state
ups/deb2bot/availability
```
**Event** \
Источник событий от apcupsd:
```
{"event":"onbattery"}
{"event":"offbattery"}
{"event":"commfailure"}
{"event":"commok"}
```

**State** \
Retained-сообщение с текущим состоянием UPS (позволяет MQTT-клиенту получить последнее состояние сразу после подключения):
```
{
  "status": "ONLINE",
  "linev": 230.0,
  "load": 20.0,
  "bcharge": 100.0,
  "timeleft": 25.1,
  "cumonbatt": 674.0
}
```

**Availability** \
Exporter публикует: `online` при подключении к MQTT и использует MQTT Last Will: `offline` при неожиданном отключении exporter-а. При штатной остановке exporter-а также публикуется: `offline`.

Таким образом, availability относится именно к доступности exporter/MQTT-связи, а не к состоянию самого UPS.

---

### Конфигурация

Настройки задаются через `.env` (пример - в репозитории).

**Если MQTT не требуется** - установить: `MQTT_ENABLED=false` (в этом режиме exporter продолжает работать как обычный Prometheus exporter и выполняет polling UPS через `apcaccess`).

### Требования

- Linux (тестировался в Debian 12);
- Python 3.8+;
- Python-зависимости (рекомендуется использовать *venv*): \
`pip install prometheus-client paho-mqtt python-dotenv`
- apcaccess из пакета **apcupsd:** \
`apt install apcupsd`

### Проверка работы

- Проверить состояние UPS: `apcaccess status`
- Проверить exporter:  `curl http://localhost:8011/health` \
Ожидаемый ответ: `OK`
- Проверить Prometheus metrics: `curl -s http://localhost:8011/metrics` \
как вариант: `curl -s http://localhost:8011/metrics | grep ups_`

### Prometheus

Пример **job:**
```
scrape_configs:

  - job_name: "ups_exporter"
    scrape_interval: 60s
    static_configs:
      - targets: ["localhost:8011"]
        labels:
          instance: "deb2bot-ups"
```
Интервал Prometheus не обязан совпадать с интервалом polling UPS. \
Например:
```
UPS polling:       5 минут
Prometheus scrape: 60 секунд
```
Это позволяет Prometheus видеть изменение `ups_data_age_seconds` даже между фактическими опросами UPS.

### Systemd

Пример **unit-файла:**
```
[Unit]
Description=APC UPS Prometheus Exporter
After=network-online.target apcupsd.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ups-mqtt
ExecStart=/usr/bin/python3 /opt/ups-mqtt/ups_exporter.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
**После установки:**\
`sudo systemctl daemon-reload`\
`sudo systemctl enable --now ups-exporter`\
`sudo systemctl status ups-exporter`

**Логи** \
`journalctl -u ups-exporter -f`

---

### Интеграция apcupsd с MQTT
Для передачи событий от apcupsd в MQTT используется вспомогательный shell-скрипт `mqtt_alert.sh`. Он вызывается из `apccontrol` при возникновении соответствующих событий UPS.

Скрипт публикует событие в MQTT в формате JSON. После этого exporter получает сообщение и, в зависимости от типа события, может выполнить внеочередной опрос UPS через apcaccess.

Перед использованием необходимо указать **адрес** и **порт** MQTT-брокера, а также **топик** для публикации событий.

Скрипт использует утилиту `mosquitto_pub`, входящую в состав пакета `mosquitto-clients`.

- Установка в **Debian:** `sudo apt install mosquitto-clients`
- Сохранить скрипт в директорию:  `/etc/apcupsd/mqtt_alert.sh`
- Сделать его исполняемым: `sudo chmod +x /etc/apcupsd/mqtt_alert.sh`

---
**Настройка apccontrol**

Для публикации событий необходимо добавить вызов `mqtt_alert.sh` в соответствующие обработчики файла `apccontrol` (и указать путь до скрипта: `MQTT_SCRIPT="/etc/apcupsd/mqtt_alert.sh"`)

Например:
- для события перехода на батарею: `"$MQTT_SCRIPT" onbattery`
- для восстановления питания: `"$MQTT_SCRIPT" offbattery`
- аналогично, для обработчиков событий: `commfailure` и `commok`
Скрипт передаёт название события, полученное первым аргументом командной строки.

В качестве примера добавил в репозиторий свой-*тестовый* `apccontrol` (строки, ориентировочно: 17, 81, 91, 112, 118). 

! **Важно:** конкретные места вставки зависят от версии и текущей конфигурации `apccontrol`.\
**НЕ следует заменять** существующие обработчики целиком. Также не следует заменять Ваш `apccontrol` образцом из репозитория (т.к. в моём случае реализована и используется совсем другая логика работы с ИБП). Достаточно добавить вызовы публикации, сохранив штатную логику работы apcupsd.

---

**Проверка работы скрипта:**
`sudo /etc/apcupsd/mqtt_alert.sh onbattery`

Подписаться на события из MQTT-брокера:
```
mosquitto_sub \
    -h 192.168.1.170 \
    -p 1883 \
    -t "ups/deb2bot/event" \
    -v
```
При успешной публикации должно появиться сообщение:
`ups/deb2bot/event {"event":"onbattery"}`

---

**Пример дашборда** *(Grafana)*

Импортируйте `JSON template` из директории `grafana` [(скрин дашборда)](images/dashboards.png).

---
