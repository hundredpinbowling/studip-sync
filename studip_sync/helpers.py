import json
import os
import re


class ConfigError(Exception):
    pass


def names_a_file(component):
    """Does this name a file inside the directory holding it?

    Dots-only names ("", ".", "..") name that directory or its parent instead.
    """
    return component.strip(".") != ""


def sanitize_path_component(name, fallback_name):
    """Turn a server-chosen name into one writable directory name.

    Separators are flattened; a name that names no file falls back to
    fallback_name.
    """
    component = re.sub(r"\s\s+", " ", name.strip()).replace("/", "--").replace("\\", "--")

    if not names_a_file(component):
        return fallback_name

    return component


def sanitize_filename(name):
    """Turn a filename Stud.IP chose into one we can safely join onto a path.

    Raises ValueError if nothing usable is left.
    """
    filename = os.path.basename(name.replace("\\", "/"))

    if not names_a_file(filename):
        raise ValueError("{} does not name a file".format(name))

    return filename


class JSONConfig(object):
    def __init__(self, config_path=None):
        super(JSONConfig, self).__init__()

        try:
            with open(config_path) as config_file:
                self.config = json.load(config_file)
        except FileNotFoundError:
            raise ConfigError("Config file missing! Run 'studip-sync --init' to create a new "
                              "config file")

        self._check()

    def _check(self):
        pass

    @staticmethod
    def save_config(path, config):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as config_file:
            print("Writing new config to '{}'".format(path))
            json.dump(config, config_file, ensure_ascii=False, indent=4)
