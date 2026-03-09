"""Constants for the Pstryk Energy integration."""

DOMAIN = "pstryk"
API_URL = "https://api.pstryk.pl/integrations/"
API_TIMEOUT = 60

BUY_ENDPOINT = "pricing/?resolution=hour&window_start={start}&window_end={end}"
SELL_ENDPOINT = "prosumer-pricing/?resolution=hour&window_start={start}&window_end={end}"

# Energy cost and usage endpoints
ENERGY_COST_ENDPOINT = "meter-data/energy-cost/?resolution={resolution}&window_start={start}&window_end={end}&for_tz=Europe/Warsaw"
ENERGY_USAGE_ENDPOINT = "meter-data/energy-usage/?resolution={resolution}&window_start={start}&window_end={end}&for_tz=Europe/Warsaw"

ATTR_BUY_PRICE = "buy_price"
ATTR_SELL_PRICE = "sell_price"
ATTR_HOURS = "hours"

# MQTT related constants
DEFAULT_MQTT_TOPIC_BUY = "energy/forecast/buy"
DEFAULT_MQTT_TOPIC_SELL = "energy/forecast/sell"
CONF_MQTT_ENABLED = "mqtt_enabled"
CONF_MQTT_TOPIC_BUY = "mqtt_topic_buy"
CONF_MQTT_TOPIC_SELL = "mqtt_topic_sell"
CONF_MQTT_48H_MODE = "mqtt_48h_mode"

# Retry mechanism constants
CONF_RETRY_ATTEMPTS = "retry_attempts"
CONF_RETRY_DELAY = "retry_delay"
DEFAULT_RETRY_ATTEMPTS = 5
DEFAULT_RETRY_DELAY = 30  # seconds
MIN_RETRY_ATTEMPTS = 1
MAX_RETRY_ATTEMPTS = 10
MIN_RETRY_DELAY = 5  # seconds
MAX_RETRY_DELAY = 300  # seconds (5 minutes)
