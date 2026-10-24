#!/usr/bin/env python3

import json
import os
import re
import subprocess
import threading
import time
import logging
import paho.mqtt.client as mqtt
from logging.handlers import RotatingFileHandler
from prometheus_client import Gauge, Counter, exposition
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

# Конфигурация
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.170")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

MQTT_ENABLED = os.getenv(
    "MQTT_ENABLED",
    "true"
).lower() == "true"

MQTT_TOPIC = os.getenv(
    "MQTT_TOPIC",
    "ups/deb2bot/event"
 )
 
MQTT_STATE_TOPIC = os.getenv(
    "MQTT_STATE_TOPIC",
    "ups/deb2bot/state"
)

MQTT_AVAILABILITY_TOPIC = os.getenv(
    "MQTT_AVAILABILITY_TOPIC",
    "ups/deb2bot/availability"
)

POLL_INTERVAL_SEC = int(
    os.getenv("POLL_INTERVAL_SEC", "300")
)

APCACCESS_TIMEOUT_SEC = int(
    os.getenv("APCACCESS_TIMEOUT_SEC", "10")
)

HTTP_PORT = int(
    os.getenv("HTTP_PORT", "8011")
)

LOG_FILE = os.getenv(
    "LOG_FILE",
    "/opt/ups-mqtt/ups_exporter.log"
)

LOG_LEVEL = os.getenv(
    "LOG_LEVEL",
    "INFO"
).upper()

# Состояние
mqtt_connected = threading.Event()
ups_lock = threading.Lock()

# Logging
log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=128 * 1024,
    backupCount=3
)

file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logger = logging.getLogger("ups_exporter")
logger.setLevel(LOG_LEVEL)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Prometheus metrics
exporter_up = Gauge(
    "ups_exporter_up",
    "1 if UPS exporter is running"
)

communication_ok = Gauge(
    "ups_communication_ok",
    "1 if communication with UPS is available"
)

on_battery = Gauge(
    "ups_on_battery",
    "1 if UPS is currently running on battery"
)

input_voltage = Gauge(
    "ups_input_voltage_volts",
    "UPS input voltage in volts"
)

load_percent = Gauge(
    "ups_load_percent",
    "UPS load percentage"
)

battery_charge = Gauge(
    "ups_battery_charge_percent",
    "UPS battery charge percentage"
)

timeleft_seconds = Gauge(
    "ups_timeleft_seconds",
    "Estimated remaining UPS runtime in seconds"
)

cumulative_on_battery = Gauge(
    "ups_cumulative_on_battery_seconds",
    "Cumulative time UPS has spent on battery, in seconds"
)

last_update = Gauge(
    "ups_last_update_timestamp",
    "Unix timestamp of last successful UPS status update"
)
last_update_time = 0.0

data_age = Gauge(
    "ups_data_age_seconds",
    "Seconds since last successful UPS status update"
)
data_age.set_function(
    lambda: (
        max(0, time.time() - last_update_time)
        if last_update_time > 0
        else 0
    )
)

failed_polls = Counter(
    "ups_failed_polls_total",
    "Total number of failed UPS status polls"
)

# MQTT
client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    protocol=mqtt.MQTTv311
)

if MQTT_ENABLED:
    client.will_set(
        MQTT_AVAILABILITY_TOPIC,
        payload="offline",
        qos=0,
        retain=True
    )

client.reconnect_delay_set(
    min_delay=1,
    max_delay=120
)

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        logger.info("Подключено к MQTT")
        mqtt_connected.set()
        
        result = client.publish(
            MQTT_AVAILABILITY_TOPIC,
            "online",
            qos=0,
            retain=True
        )

        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(
                f"Ошибка публикации MQTT availability: rc={result.rc}"
            )
         
        result = client.subscribe(
            MQTT_TOPIC,
            qos=0
        )

        if result[0] != mqtt.MQTT_ERR_SUCCESS:
            logger.error(
                f"Ошибка подписки на {MQTT_TOPIC}: rc={result[0]}"
            )
        else:
            logger.info(
                f"Подписка: {MQTT_TOPIC}"
            )

    else:
        logger.error(
            f"Ошибка подключения MQTT: {reason_code}"
        )

def on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None
):

    logger.warning(
        f"MQTT отключен: {reason_code}"
    )

    mqtt_connected.clear()

def on_message(client, userdata, msg):
    try:
        payload = json.loads(
            msg.payload.decode()
        )
        event = payload.get("event")

        if not event:
            logger.warning(
                f"MQTT сообщение без event: {msg.payload}"
            )
            return

        logger.info(
            f"Получено MQTT событие: {event}"
        )

        with ups_lock:
            if event == "onbattery":
                on_battery.set(1)
            elif event == "offbattery":
                on_battery.set(0)
            elif event == "commfailure":
                communication_ok.set(0)
            elif event == "commok":
                communication_ok.set(1)
            else:
                logger.warning(
                    f"Неизвестное UPS событие: {event}"
                )

        # Внеочередной опрос UPS для корректировки статусов
        if event in ("onbattery", "offbattery", "commfailure", "commok"):
            logger.info(
                f"Внеочередной опрос UPS после события: {event}"
            )
            poll_ups()

    except json.JSONDecodeError:

        logger.error(
            f"Некорректный JSON из MQTT: {msg.payload}"
        )

    except Exception as e:

        logger.error(
            f"Ошибка обработки MQTT сообщения: {e}"
        )

def publish_ups_state(data):
    if not MQTT_ENABLED:
        return

    if not mqtt_connected.is_set():
        logger.warning(
            "MQTT недоступен, state UPS не опубликован"
        )
        return

    try:
        state = {
            "status": data.get("STATUS"),
            "linev": extract_number(
                data.get("LINEV")
            ),
            "load": extract_number(
                data.get("LOADPCT")
            ),
            "bcharge": extract_number(
                data.get("BCHARGE")
            ),
            "timeleft": extract_number(
                data.get("TIMELEFT")
            ),
            "cumonbatt": extract_number(
                data.get("CUMONBATT")
            )
        }

        result = client.publish(
            MQTT_STATE_TOPIC,
            json.dumps(state),
            qos=0,
            retain=True
        )

        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(
                f"Ошибка публикации UPS state: rc={result.rc}"
            )

    except Exception as e:
        logger.error(
            f"Ошибка публикации UPS state: {e}"
        )

client.on_connect = on_connect
client.on_message = on_message
client.on_disconnect = on_disconnect

# apcaccess parser
def parse_apcaccess(output):
    data = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        data[key] = value
    return data

def extract_number(value):
    if value is None:
        return None
    match = re.search(
        r"[-+]?\d+(?:\.\d+)?",
        value
    )

    if not match:
        return None
    return float(match.group())

# UPS polling
def poll_ups():
    logger.debug("Начинаем опрос UPS через apcaccess")
    try:
        result = subprocess.run(
            ["apcaccess", "status"],
            capture_output=True,
            text=True,
            timeout=APCACCESS_TIMEOUT_SEC
        )

        if result.returncode != 0:

            raise RuntimeError(
                f"apcaccess завершился с кодом "
                f"{result.returncode}: "
                f"{result.stderr.strip()}"
            )

        data = parse_apcaccess(
            result.stdout
        )

        status = data.get("STATUS")

        if not status:
            raise RuntimeError(
                "В ответе apcaccess отсутствует STATUS"
            )

        # COMMLOST означает, что apcaccess ответил,
        # но актуальных данных от UPS нет.
        if "COMMLOST" in status:
            logger.warning(
                "Связь с UPS потеряна: STATUS=COMMLOST"
            )
            communication_ok.set(0)
            exporter_up.set(1)
            failed_polls.inc()
            
            # Публикуем явное состояние потери связи в MQTT
            if MQTT_ENABLED and mqtt_connected.is_set():
                try:
                    state = {
                        "status": "COMMLOST",
                        "linev": 0,
                        "load": 0,
                        "bcharge": 0,
                        "timeleft": 0,
                        "cumonbatt": 0
                    }
                    client.publish(
                        MQTT_STATE_TOPIC,
                        json.dumps(state),
                        qos=0,
                        retain=True
                    )
                except Exception as e:
                    logger.error(
                        f"Ошибка публикации COMMLOST state: {e}"
                    )

            return False

        now = time.time()
        
        global last_update_time
        last_update_time = now

        with ups_lock:
            
            # Связь с UPS работает
            communication_ok.set(1)

            # Статус питания
            # if status == "ONBATT":
            if "ONBATT" in status:
                on_battery.set(1)
            # elif status == "ONLINE":
            elif "ONLINE" in status:
                on_battery.set(0)
            else:
                logger.warning(
                    f"Неизвестный STATUS UPS: {status}"
                )

            # LINEV
            value = extract_number(
                data.get("LINEV")
            )

            if value is not None:
                input_voltage.set(value)

            # LOADPCT
            value = extract_number(
                data.get("LOADPCT")
            )

            if value is not None:
                load_percent.set(value)

            # BCHARGE
            value = extract_number(
                data.get("BCHARGE")
            )

            if value is not None:
                battery_charge.set(value)

            # ------------------------------------------------
            # TIMELEFT
            #
            # Например:
            # 20.4 Minutes
            #
            # Переводим в секунды.
            # ------------------------------------------------

            value = extract_number(
                data.get("TIMELEFT")
            )

            if value is not None:
                timeleft_seconds.set(
                    value * 60
                )

            # ------------------------------------------------
            # CUMONBATT
            #
            # Например:
            # 14 Seconds
            # ------------------------------------------------

            value = extract_number(
                data.get("CUMONBATT")
            )

            if value is not None:

                cumulative_on_battery.set(
                    value
                )

            # Время получения данных
            last_update.set(now)
            exporter_up.set(1)

        publish_ups_state(data)
        
        logger.info(
            "UPS status: "
            f"STATUS={status}, "
            f"LINEV={data.get('LINEV')}, "
            f"LOAD={data.get('LOADPCT')}, "
            f"BCHARGE={data.get('BCHARGE')}, "
            f"TIMELEFT={data.get('TIMELEFT')}, "
            f"CUMONBATT={data.get('CUMONBATT')}"
        )
        
        return True

    except subprocess.TimeoutExpired:
        logger.error(
            "apcaccess: timeout"
        )

    except Exception as e:
        logger.error(
            f"Ошибка опроса UPS: {e}"
        )

    # Poll failed
    communication_ok.set(0)
    exporter_up.set(1)
    failed_polls.inc()

    return False

# HTTP server
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header(
                "Content-Type",
                exposition.CONTENT_TYPE_LATEST
            )

            self.end_headers()
            self.wfile.write(
                exposition.generate_latest()
            )

        elif self.path == "/health":

            exporter_value = (
                exporter_up._value.get()
            )

            status = (
                200
                if exporter_value == 1
                else 503
            )

            self.send_response(status)
            self.send_header(
                "Content-Type",
                "text/plain"
            )

            self.end_headers()
            message = (
                "OK"
                if status == 200
                else "DOWN"
            )

            self.wfile.write(
                message.encode()
            )

        else:

            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

def run_server():
    server_address = (
        "",
        HTTP_PORT
    )

    httpd = ThreadingHTTPServer(
        server_address,
        HealthHandler
    )

    logger.info(
        f"HTTP-сервер запущен на "
        f"{HTTP_PORT} "
        f"(/metrics и /health)"
    )

    httpd.serve_forever()

# Main
if __name__ == "__main__":

    logger.info(
        "Start UPS exporter"
    )

    exporter_up.set(1)
    communication_ok.set(0)
    on_battery.set(0)

    # HTTP server
    threading.Thread(
        target=run_server,
        daemon=True
    ).start()

    # MQTT
    if MQTT_ENABLED:
        client.connect_async(
            MQTT_BROKER,
            MQTT_PORT,
            60
        )

        client.loop_start()
        
        logger.info(
            "Ждём подключения к MQTT..."
        )

        if not mqtt_connected.wait(
            timeout=15
        ):
            logger.error(
                "MQTT connection timeout"
            )

    logger.info(
        "Первичный опрос UPS"
    )

    poll_ups()

    try:
        while True:
            time.sleep(
                POLL_INTERVAL_SEC
            )

            poll_ups()

    except KeyboardInterrupt:
        logger.info(
            "Останавливаем exporter..."
        )

        if MQTT_ENABLED:
            client.publish(
                MQTT_AVAILABILITY_TOPIC,
                "offline",
                qos=0,
                retain=True
            )
            
            client.loop_stop()
            client.disconnect()
        
        logger.info(
            "Exporter остановлен"
        )
