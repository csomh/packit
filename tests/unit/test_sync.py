# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

from pathlib import Path
from contextlib import contextmanager

import pytest

# from flexmock import flexmock

from packit.sync import check_subpath
from packit.exceptions import PackitException


@contextmanager
def return_result(result):
    yield result


@pytest.mark.parametrize(
    "subpath,path,result",
    [
        (Path("./test/this"), Path("."), return_result(Path("./test/this"))),
        (Path("test/this"), Path("."), return_result(Path("test/this"))),
        (Path("../test/this"), Path("."), pytest.raises(PackitException)),
        (Path("test/../../this"), Path("."), pytest.raises(PackitException)),
    ],
)
def test_check_subpath(subpath, path, result):
    with result as r:
        assert check_subpath(subpath, path) == r


# TODO(csomh): test SyncFilesItem
