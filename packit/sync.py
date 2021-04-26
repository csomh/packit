# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from pathlib import Path
from typing import List, Optional, Union, Sequence

from packit.exceptions import PackitException
from packit.utils import run_command

logger = logging.getLogger(__name__)


def check_subpath(subpath: Path, path: Path) -> Path:
    """Check if 'subpath' is a subpath of 'path'
    Args:
        subpath: Subpath to be checked.
        path: Path agains which subpath is checked.
    Returns:
        'subpath', in case it is a subpath of 'path'.
    Raises:
        PackitException, if 'subpath' is not a subpath of 'path'.
    """
    subpath_resolved = subpath.resolve()
    path_resolved = path.resolve()
    if not str(subpath_resolved).startswith(str(path_resolved)):
        raise PackitException(
            f"Sync files: Illegal path! {subpath} is not a subpath of {path}."
        )
    return subpath


class SyncFilesItem:
    def __init__(self, src: Sequence[Union[str, Path]], dest: Union[str, Path]):
        self.src = [Path(s) for s in src]
        self.dest = Path(dest)

    def __repr__(self):
        return f"SyncFilesItem(src={self.src}, dest={self.dest})"

    def __str__(self):
        return " ".join(self.command())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SyncFilesItem):
            raise NotImplementedError()

        return self.src == other.src and self.dest == other.dest

    def command(self) -> List[str]:
        command = ["rsync", "--archive"]
        command += [str(s) for s in self.src]
        command += [str(self.dest)]
        return command

    def resolve(self, src_base: Path = Path.cwd(), dest_base: Path = Path.cwd()):
        """Resolve all paths and check they are relative to src_base and dest_base"""
        self.src = [check_subpath(src_base / path, src_base) for path in self.src]
        self.dest = check_subpath(dest_base / self.dest, dest_base)

    def drop_src(
        self, src: Union[str, Path], criteria=lambda x, y: x != y
    ) -> Optional["SyncFilesItem"]:
        """Remove 'src' from the list of src-s

        This creates and returns a new SyncFilesItem instance if the internal
        src list still has some items after 'src' is removed. Otherwise returns None.
        """
        new_src = [s for s in self.src if criteria(s, src)]
        if new_src:
            return SyncFilesItem(new_src, self.dest)
        else:
            return None


def sync_files(synced_files: Sequence[SyncFilesItem]):
    """
    Copy files b/w upstream and downstream repo.
    """
    for item in synced_files:
        run_command(item.command(), print_live=True)
