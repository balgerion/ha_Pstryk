
DOMAIN = "pstryk"
API_URL = "https://api.pstryk.pl/integrations/"
API_TIMEOUT = 60
# pstryk.pl/regulamin-api: 3 requests/hour per endpoint. Prices and costs share one endpoint.
API_HOURLY_LIMIT = 3
REQUEST_LOG_STORE_VERSION = 1
# A slot is counted from the moment its request is sent and frees this long
# after the hour: we time the send, Pstryk times the arrival.
API_WINDOW_MARGIN_SECONDS = 2
# A request that finds the budget full waits this long for a slot to free.
# Checks repeat on the hour, so last hour's request leaves the window a moment
# after the timer fires; each hour on the limit starts the margin later, and
# 30 s covers the longest run (12:10 to 23:50).
API_SLOT_WAIT_SECONDS = 30

PRICING_ENDPOINT = (
    "meter-data/unified-metrics/?metrics=pricing"
    "&resolution=hour&window_start={start}&window_end={end}"
)

UNIFIED_METRICS_ENDPOINT = (
    "meter-data/unified-metrics/?metrics=meter_values,cost"
    "&resolution={resolution}&window_start={start}&window_end={end}&for_tz=Europe/Warsaw"
)

ATTR_BUY_PRICE = "buy_price"
ATTR_SELL_PRICE = "sell_price"
ATTR_HOURS = "hours"

DEFAULT_MQTT_TOPIC_BUY = "energy/forecast/buy"
DEFAULT_MQTT_TOPIC_SELL = "energy/forecast/sell"
CONF_MQTT_ENABLED = "mqtt_enabled"
CONF_MQTT_TOPIC_BUY = "mqtt_topic_buy"
CONF_MQTT_TOPIC_SELL = "mqtt_topic_sell"
CONF_MQTT_48H_MODE = "mqtt_48h_mode"
CONF_JSON_SENSOR = "json_sensor_enabled"

CONF_RETRY_ATTEMPTS = "retry_attempts"
CONF_RETRY_DELAY = "retry_delay"
DEFAULT_RETRY_ATTEMPTS = 5
DEFAULT_RETRY_DELAY = 30
MIN_RETRY_ATTEMPTS = 1
MAX_RETRY_ATTEMPTS = 10
MIN_RETRY_DELAY = 5
MAX_RETRY_DELAY = 300
