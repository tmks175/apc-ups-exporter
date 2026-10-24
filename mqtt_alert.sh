#!/bin/sh

MQTT_BROKER="192.168.1.170"
MQTT_PORT="1883"
MQTT_TOPIC="ups/deb2bot/event"

EVENT="$1"

if [ -z "$EVENT" ]; then
    echo "Usage: $0 <event>" >&2
    exit 1
fi

mosquitto_pub \
    -h "$MQTT_BROKER" \
    -p "$MQTT_PORT" \
    -t "$MQTT_TOPIC" \
    -m "{\"event\":\"${EVENT}\"}"

EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "MQTT publish failed: event=${EVENT}, rc=${EXIT_CODE}" >&2
fi

exit 0
