# SPDX-License-Identifier: MIT
# Copyright (c) 2020 Adam Souzis
import logging
import os
import sys

import pbr.version

from unfurl import logs


# We need to initialize logging before any logger is created
logs.initialize_logging()


def __version__(release=False):
    # a function because this is expensive
    if release:  # appends .devNNN
        return pbr.version.VersionInfo(__name__).release_string()
    else:  # semver only
        return pbr.version.VersionInfo(__name__).version_string()


def version_tuple(v=None):
    if v is None:
        v = __version__(True)
    return tuple(int(x.lstrip("dev") or 0) for x in v.split("."))


def is_version_unreleased():
    return len(version_tuple()) > 3


vendor_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "vendor")
sys.path.insert(0, vendor_dir)


class DefaultNames:
    SpecDirectory = "spec"
    EnsembleDirectory = "ensemble"
    Ensemble = "ensemble.yaml"
    EnsembleTemplate = "ensemble-template.yaml"
    ServiceTemplate = "service-template.yaml"
    LocalConfig = "unfurl.yaml"
    SecretsConfig = "secrets.yaml"
    HomeDirectory = ".unfurl_home"
    JobsLog = "jobs.tsv"
    ProjectDirectory = ".unfurl"
    LocalConfigTemplate = "unfurl-local-template.yaml"

    def __init__(self, **names):
        self.__dict__.update({name: value for name, value in names.items() if value})


def get_home_config_path(homepath):
    # if homepath is explicitly it overrides UNFURL_HOME
    # (set it to empty string to disable the homepath)
    # otherwise use UNFURL_HOME or the default location
    if homepath is None:
        if "UNFURL_HOME" in os.environ:
            homepath = os.getenv("UNFURL_HOME")
        else:
            homepath = os.path.join("~", DefaultNames.HomeDirectory)
    if homepath:
        homepath = os.path.expanduser(homepath)
        if not os.path.exists(homepath):
            isdir = not homepath.endswith(".yml") and not homepath.endswith(".yaml")
        else:
            isdir = os.path.isdir(homepath)
        if isdir:
            return os.path.abspath(os.path.join(homepath, DefaultNames.LocalConfig))
        else:
            return os.path.abspath(homepath)
    return None


### Ansible initialization
if "ANSIBLE_CONFIG" not in os.environ:
    os.environ["ANSIBLE_CONFIG"] = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "configurators", "ansible.cfg")
    )
try:
    import ansible
except ImportError:
    pass
else:
    import ansible.constants as C

    if "ANSIBLE_NOCOWS" not in os.environ:
        C.ANSIBLE_NOCOWS = 1
    if "ANSIBLE_JINJA2_NATIVE" not in os.environ:
        C.DEFAULT_JINJA2_NATIVE = 1

    import ansible.utils.display

    ansible.utils.display.logger = logging.getLogger("unfurl.ansible")
    display = ansible.utils.display.Display()

    # Display is a singleton which we can't subclass so monkey patch instead
    _super_display = ansible.utils.display.Display.display

    def _display(self, msg, color=None, stderr=False, screen_only=False, log_only=True):
        if screen_only:
            return
        return _super_display(self, msg, color, stderr, screen_only, log_only)

    ansible.utils.display.Display.display = _display

    from ansible.plugins.loader import filter_loader, lookup_loader

    lookup_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
    filter_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
