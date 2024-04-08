import os
import yaml

from typing import Any

# Environment variables
DEVELOPER_MODE = (os.getenv('DEVELOPER_MODE') == "true")
ERROR_LOGGING = ((os.getenv('ERROR_LOGGING') or "true") == "true")
CONFIG_TAG = ("dev" if DEVELOPER_MODE else "prod")
AGENT_LOGGING = ((os.getenv('AGENT_LOGGING') or "true") == "true")

# App config.yaml file
config_all: dict[str, Any] = {}
config: dict[str, Any] = {}

def init_config() -> None:
    global config_all
    global config
    with open('config.yaml', 'r') as f:
        config_all = yaml.load(f, Loader=yaml.FullLoader)
        config = config_all[CONFIG_TAG]
