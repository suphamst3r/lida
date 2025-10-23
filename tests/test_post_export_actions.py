import os
import time
import subprocess
from unittest import mock

import pytest

from lada.gui.export.export_view import ExportView
from lada.gui.config.config import Config
from gi.repository import Adw


class DummyWindow:
    def __init__(self):
        self._visible = True
    def get_root(self):
        return self
    def close(self):
        pass


@pytest.fixture()
def config(tmp_path, gtk_loop):
    # create a minimal style manager fake for Config
    style_manager = Adw.StyleManager()
    cfg = Config(style_manager)
    cfg.post_export_commands = 'echo test'
    cfg.post_export_sound = None
    cfg.post_export_shutdown = False
    cfg.post_export_close = False
    return cfg


def test_post_export_runs_commands(monkeypatch, config):
    ev = ExportView()
    ev._config = config
    calls = []

    class DummyPopen:
        def __init__(self, cmd, shell=False):
            calls.append(cmd)
        def __repr__(self):
            return f"DummyPopen({calls[-1]})"

    monkeypatch.setattr(subprocess, 'Popen', DummyPopen)
    # ensure no exceptions
    ev.perform_post_export_actions()
    assert any('echo test' in str(c) for c in calls)
