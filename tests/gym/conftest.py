"""Every gym test module runs under its own temp HOME: gym reads HOME at import (in-process load_module) and for its
default paths, so nothing reaches the real home. The tools' own env knobs from the caller are dropped."""

import os

import pytest

_DROP = ("GYM_", "JEFIT_", "TG_CMD_", "TGCMD_", "USAGE")


@pytest.fixture(scope="module", autouse=True)
def gym_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("gymhome")
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(home))
    for k in [k for k in os.environ if k.startswith(_DROP)]:
        mp.delenv(k)
    yield home
    mp.undo()
