
from functools import lru_cache

from studip_sync.config import get_config
from studip_sync.plugins.plugin_list import PluginList


@lru_cache(maxsize=None)
def get_plugins():
    """The configured plugins, loaded on first call and reused after that.

    Lazy for the same reason the config is: see config.get_config.
    """
    config = get_config()

    return PluginList(config.plugins, config.config_dir)


def start_plugins():
    """The configured plugins, with their start hook fired."""
    plugins = get_plugins()
    plugins.hook("hook_start")

    return plugins
