#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from studip_sync.arg_parser import ARGS
from studip_sync.config_creator import ConfigCreator

if ARGS.init:
    with ConfigCreator() as creator:
        creator.new_config()
    exit()

from studip_sync.helpers import ConfigError
from studip_sync.plugins import PluginError, PluginConfigError
from studip_sync.plugins.plugin_helper import PluginHelper

try:
    if ARGS.enable_plugin:
        with PluginHelper(ARGS.enable_plugin) as plugin_helper:
            exit(plugin_helper.enable())

    if ARGS.reconfigure_plugin:
        with PluginHelper(ARGS.reconfigure_plugin) as plugin_helper:
            exit(plugin_helper.reconfigure())

    if ARGS.disable_plugin:
        with PluginHelper(ARGS.disable_plugin) as plugin_helper:
            exit(plugin_helper.disable())

    if ARGS.old:
        from studip_sync.studip_sync import StudipSync
        with StudipSync() as s:
            exit(s.sync(ARGS.full, ARGS.recent))
    else:
        from studip_sync.studip_rsync import StudIPRSync
        with StudIPRSync() as s:
            exit(s.sync(ARGS.full, ARGS.recent, not ARGS.disable_api))
except (ConfigError, PluginError, PluginConfigError) as err:
    # Lazy config/plugin loading moves these errors out of the imports above.
    print(str(err))
    print("Aborting...")
    exit(1)

