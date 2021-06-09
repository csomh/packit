# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT
"""
packit started as source-git and we're making a source-git module after such a long time, weird
"""
import configparser
import logging
import shutil
import tarfile
import tempfile
import textwrap
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import yaml
from git import GitCommandError
from ogr.parsing import parse_git_repo
from rebasehelper.exceptions import LookasideCacheError
from rebasehelper.helpers.lookaside_cache_helper import LookasideCacheHelper

from packit.config.common_package_config import CommonPackageConfig
from packit.config.config import Config
from packit.config.package_config import PackageConfig
from packit.constants import (
    RPM_MACROS_FOR_PREP,
    FEDORA_DOMAIN,
    CENTOS_DOMAIN,
    CENTOS_STREAM_GITLAB,
    CENTOS_STREAM_GITLAB_DOMAIN,
    CENTOS_STREAM_GITLAB_NAMESPACE,
)
from packit.distgit import DistGit
from packit.exceptions import PackitException
from packit.local_project import LocalProject
from packit.patches import PatchMetadata
from packit.utils import (
    run_command,
    clone_centos_8_package,
    clone_centos_9_package,
    get_default_branch,
)
from packit.specfile import Specfile

logger = logging.getLogger(__name__)


class CentOS8DistGit(DistGit):
    """
    CentOS dist-git layout implementation for 8: CentOS Linux 8 and CentOS Stream 8
    which lives in git.centos.org
    """

    # spec files are stored in this dir in dist-git
    spec_dir_name = "SPECS"

    # sources are stored in this dir in dist-git
    # this applies to CentOS Stream 8 and CentOS Linux 7 and 8
    source_dir_name = "SOURCES"

    @classmethod
    def clone(
        cls,
        config: Config,
        package_config: CommonPackageConfig,
        path: Path,
        branch: str = None,
    ) -> "CentOS8DistGit":
        clone_centos_8_package(
            package_config.downstream_package_name, path, branch=branch
        )
        lp = LocalProject(working_dir=path)
        return cls(config, package_config, local_project=lp)


class CentOS9DistGit(DistGit):
    """
    CentOS dist-git layout implementation for CentOS Stream 9
    which lives in gitlab.com/redhat/centos-stream/rpms
    """

    # spec files are stored in this dir in dist-git
    spec_dir_name = ""

    # sources are stored in this dir in dist-git
    source_dir_name = ""

    @classmethod
    def clone(
        cls,
        config: Config,
        package_config: CommonPackageConfig,
        path: Path,
        branch: str = None,
    ) -> "CentOS9DistGit":
        clone_centos_9_package(
            package_config.downstream_package_name, path, branch=branch
        )
        lp = LocalProject(working_dir=path)
        return cls(config, package_config, local_project=lp)


def get_distgit_kls_from_repo(
    repo_path: Path, config: Config
) -> Tuple[DistGit, Optional[str], Optional[str]]:
    """
    :return: DistGit instance, centos package name, fedora package name
    """
    path = Path(repo_path)
    pc = PackageConfig(downstream_package_name=path.name)
    lp = LocalProject(working_dir=path)

    lp_git_repo = parse_git_repo(lp.git_url)

    if FEDORA_DOMAIN in lp_git_repo.hostname:
        return DistGit(config, pc, local_project=lp), None, path.name
    elif CENTOS_DOMAIN in lp_git_repo.hostname:
        return CentOS8DistGit(config, pc, local_project=lp), path.name, None
    elif (
        CENTOS_STREAM_GITLAB_DOMAIN == lp_git_repo.hostname
        and lp_git_repo.namespace.find(CENTOS_STREAM_GITLAB_NAMESPACE) == 0
    ):
        return CentOS9DistGit(config, pc, local_project=lp), path.name, None
    raise PackitException(
        f"Dist-git URL {lp.git_url} not recognized, we expected one of: "
        f"{FEDORA_DOMAIN}, {CENTOS_DOMAIN} or {CENTOS_STREAM_GITLAB}"
    )


def get_tarball_comment(tarball_path: str) -> Optional[str]:
    """Return the comment header for the tarball

    If written by git-archive, this contains the Git commit ID.
    Return None if the file is invalid or does not contain a comment.

    shamelessly stolen:
    https://pagure.io/glibc-maintainer-scripts/blob/master/f/glibc-sync-upstream.py#_75
    """
    try:
        with tarfile.open(tarball_path) as tar:
            return tar.pax_headers["comment"]
    except Exception as ex:
        logger.debug(f"Could not get 'comment' header from the tarball: {ex}")
        return None


# https://stackoverflow.com/questions/13518819/avoid-references-in-pyyaml
# mypy hated the suggestion from the SA ^, hence an override like this
class SafeDumperWithoutAliases(yaml.SafeDumper):
    def ignore_aliases(self, data):
        # no aliases/anchors in the dumped yaml text
        return True


class SourceGitGenerator:
    """
    generate a source-git repo from provided upstream repo
    and a corresponding package in Fedora/CentOS ecosystem
    """

    # we store downstream content in source-git in this subdir
    DISTRO_DIR = ".distro"

    def __init__(
        self,
        local_project: LocalProject,
        config: Config,
        upstream_url: Optional[str] = None,
        upstream_ref: Optional[str] = None,
        dist_git_path: Optional[Path] = None,
        dist_git_branch: Optional[str] = None,
        fedora_package: Optional[str] = None,
        centos_package: Optional[str] = None,
        package_name: Optional[str] = None,
        tmpdir: Optional[Path] = None,
    ):
        """
        :param local_project: this source-git repo
        :param config: global configuration
        :param upstream_url: upstream repo URL we want to use as a base
        :param upstream_ref: upstream git-ref to use as a base
        :param dist_git_path: path to a local clone of a dist-git repo
        :param dist_git_branch: branch in dist-git to use
        :param fedora_package: pick up specfile and downstream sources from this fedora package
        :param centos_package: pick up specfile and downstream sources from this centos package
        :param package_name: name of the package in the distro
        :param tmpdir: path to a directory where temporary repos (upstream,
                       dist-git) will be cloned
        """
        self.local_project = local_project
        self.config = config
        self.tmpdir = tmpdir or Path(tempfile.mkdtemp(prefix="packit-sg-"))
        self._dist_git: Optional[DistGit] = None
        self._primary_archive: Optional[Path] = None
        self._upstream_ref: Optional[str] = upstream_ref
        self.dist_git_branch = dist_git_branch
        self.distro_dir = Path(self.local_project.working_dir, self.DISTRO_DIR)
        self.package_name = package_name

        logger.info(
            f"The source-git repo is going to be created in {local_project.working_dir}."
        )

        if dist_git_path:
            (
                self._dist_git,
                self.centos_package,
                self.fedora_package,
            ) = get_distgit_kls_from_repo(dist_git_path, config)
            self.dist_git_path = dist_git_path
            self.package_config = self.dist_git.package_config
        else:
            self.centos_package = centos_package
            self.fedora_package = fedora_package
            if centos_package:
                self.package_config = PackageConfig(
                    downstream_package_name=centos_package
                )
            elif fedora_package:
                self.fedora_package = (
                    self.fedora_package or local_project.working_dir.name
                )
                self.package_config = PackageConfig(
                    downstream_package_name=fedora_package
                )
            else:
                raise PackitException(
                    "Please tell us the name of the package in the downstream."
                )
            self.dist_git_path = self.tmpdir.joinpath(
                self.package_config.downstream_package_name
            )

        if upstream_url:
            if Path(upstream_url).is_dir():
                self.upstream_repo_path: Path = Path(upstream_url)
                self.upstream_lp: LocalProject = LocalProject(
                    working_dir=self.upstream_repo_path
                )
            else:
                self.upstream_repo_path = self.tmpdir.joinpath(
                    f"{self.package_config.downstream_package_name}-upstream"
                )
                self.upstream_lp = LocalProject(
                    git_url=upstream_url, working_dir=self.upstream_repo_path
                )
        else:
            # $CWD is the upstream repo and we just need to pick
            # downstream stuff
            self.upstream_repo_path = self.local_project.working_dir
            self.upstream_lp = self.local_project

    @property
    def primary_archive(self) -> Path:
        if not self._primary_archive:
            self._primary_archive = self.dist_git.download_upstream_archive()
        return self._primary_archive

    @property
    def dist_git(self) -> DistGit:
        if not self._dist_git:
            self._dist_git = self._get_dist_git()
            # we need to parse the spec twice
            # https://github.com/rebase-helper/rebase-helper/issues/848
            self._dist_git.download_remote_sources()
            self._dist_git.specfile.reload()
        return self._dist_git

    @property
    def upstream_ref(self) -> str:
        if self._upstream_ref is None:
            self._upstream_ref = get_tarball_comment(str(self.primary_archive))
            if self._upstream_ref:
                logger.info(
                    "upstream base ref was not set, "
                    f"discovered it from the archive: {self._upstream_ref}"
                )
            else:
                # fallback to HEAD
                try:
                    self._upstream_ref = self.local_project.commit_hexsha
                except ValueError as ex:
                    raise PackitException(
                        "Current branch seems to be empty - we cannot get the hash of "
                        "the top commit. We need to set upstream_ref in packit.yaml to "
                        "distinct between upstream and downstream changes. "
                        "Please set --upstream-ref or pull the upstream git history yourself. "
                        f"Error: {ex}"
                    )
                logger.info(
                    "upstream base ref was not set, "
                    f"falling back to the HEAD commit: {self._upstream_ref}"
                )
        return self._upstream_ref

    def _get_dist_git(
        self,
    ) -> DistGit:
        """
        For given package names, clone the dist-git repo in the given directory
        and return the DistGit class

        :return: DistGit instance
        """
        if self.centos_package:
            self.dist_git_branch = self.dist_git_branch or "c9s"
            # let's be sure to cover anything 9 related,
            # even though "c9" will probably never be a thing
            if "c9" in self.dist_git_branch:
                return CentOS9DistGit.clone(
                    config=self.config,
                    package_config=self.package_config,
                    path=self.dist_git_path,
                    branch=self.dist_git_branch,
                )
            return CentOS8DistGit.clone(
                config=self.config,
                package_config=self.package_config,
                path=self.dist_git_path,
                branch=self.dist_git_branch,
            )
        else:
            # If self.dist_git_branch is None we will checkout/store repo's default branch
            dg = DistGit.clone(
                config=self.config,
                package_config=self.package_config,
                path=self.dist_git_path,
                branch=self.dist_git_branch,
            )
            self.dist_git_branch = (
                self.dist_git_branch or dg.local_project.git_project.default_branch
            )
            return dg

    def _pull_upstream_ref(self):
        """
        Pull the base ref from upstream to our source-git repo
        """
        # fetch operation is pretty intense
        # if upstream_ref is a commit, we need to fetch everything
        # if it's a tag or branch, we can only fetch that ref
        self.local_project.fetch(
            str(self.upstream_lp.working_dir), "+refs/heads/*:refs/remotes/origin/*"
        )
        self.local_project.fetch(
            str(self.upstream_lp.working_dir),
            "+refs/remotes/origin/*:refs/remotes/origin/*",
        )
        try:
            next(self.local_project.get_commits())
        except GitCommandError as ex:
            logger.debug(f"Can't get next commit: {ex}")
            # the repo is empty, rebase would fail
            self.local_project.reset(self.upstream_ref)
        else:
            self.local_project.rebase(self.upstream_ref)

    def _run_prep(self):
        """
        run `rpmbuild -bp` in the dist-git repo to get a git-repo
        in the %prep phase so we can pick the commits in the source-git repo
        """
        _packitpatch_path = shutil.which("_packitpatch")
        if not _packitpatch_path:
            raise PackitException(
                "We are trying to unpack a dist-git archive and lay patches on top "
                'by running `rpmbuild -bp` but we cannot find "_packitpatch" command on PATH: '
                "please install packit as an RPM."
            )
        logger.info(
            f"expanding %prep section in {self.dist_git.local_project.working_dir}"
        )

        rpmbuild_args = [
            "rpmbuild",
            "--nodeps",
            "--define",
            f"_topdir {str(self.dist_git.local_project.working_dir)}",
            "-bp",
            "--define",
            f"_specdir {str(self.dist_git.absolute_specfile_dir)}",
            "--define",
            f"_sourcedir {str(self.dist_git.absolute_source_dir)}",
        ]
        rpmbuild_args += RPM_MACROS_FOR_PREP
        if logger.level <= logging.DEBUG:  # -vv can be super-duper verbose
            rpmbuild_args.append("-v")
        rpmbuild_args.append(str(self.dist_git.absolute_specfile_path))

        run_command(
            rpmbuild_args,
            cwd=self.dist_git.local_project.working_dir,
            print_live=True,
        )

    def _get_lookaside_sources(self) -> List[Dict[str, str]]:
        """
        Read "sources" file from the dist-git repo and return a list of dicts
        with path and url to sources stored in the lookaside cache
        """
        pkg_tool = "centpkg" if self.centos_package else "fedpkg"
        try:
            config = LookasideCacheHelper._read_config(pkg_tool)
            base_url = config["lookaside"]
        except (configparser.Error, KeyError) as e:
            raise LookasideCacheError("Failed to read rpkg configuration") from e

        package = self.dist_git.package_config.downstream_package_name
        basepath = self.dist_git.local_project.working_dir

        sources = []
        for source in LookasideCacheHelper._read_sources(basepath):
            url = "{0}/rpms/{1}/{2}/{3}/{4}/{2}".format(
                base_url,
                package,
                source["filename"],
                source["hashtype"],
                source["hash"],
            )

            path = source["filename"]
            sources.append({"path": path, "url": url})

        return sources

    def get_BUILD_dir(self):
        path = self.dist_git.local_project.working_dir
        build_dirs = [d for d in (path / "BUILD").iterdir() if d.is_dir()]
        if len(build_dirs) > 1:
            raise RuntimeError(f"More than one directory found in {path / 'BUILD'}")
        if len(build_dirs) < 1:
            raise RuntimeError(f"No subdirectory found in {path / 'BUILD'}")
        return build_dirs[0]

    def _rebase_patches(self, from_branch):
        """Rebase current branch against the from_branch"""
        to_branch = "dist-git-commits"  # temporary branch to store the dist-git history
        logger.info(f"Rebase patches from dist-git {from_branch}.")
        BUILD_dir = self.get_BUILD_dir()
        self.local_project.fetch(BUILD_dir, f"+{from_branch}:{to_branch}")

        # transform into {patch_name: patch_id}
        patch_ids = {
            Path(p.path).name: p.index
            for p in self.dist_git.specfile.patches.get("applied", [])
        }

        # -2 - drop first commit which represents tarball unpacking
        # -1 - reverse order, HEAD is last in the sequence
        patch_commits = list(
            LocalProject(working_dir=BUILD_dir).get_commits(from_branch)
        )[-2::-1]

        for commit in patch_commits:
            self.local_project.git_repo.git.cherry_pick(
                commit.hexsha,
                keep_redundant_commits=True,
                allow_empty=True,
                strategy_option="theirs",
            )

            # Annotate commits in the source-git repo with patch_id. This info is not provided
            # during the rpm patching process so we need to do it here.
            metadata = PatchMetadata.from_commit(commit=commit)
            # commit.message already ends with \n
            message = f"{commit.message}patch_id: {patch_ids[metadata.name]}"
            self.local_project.commit(message, amend=True)

        self.local_project.git_repo.git.branch("-D", to_branch)

    def _populate_distro_dir(self):
        """Copy files used in the distro to package and test the software to .distro."""
        # TODO(csomh): Check that the dist-git working directory is pristine,
        # that is, it's not dirty and has nothing git clean -xd would remove.
        # Error out, if it's not the case, we don't want to copy untracked content
        # to .distro.
        command = ["rsync", "--archive", "--delete"]
        for exclude in ["*.patch", "sources", ".git*"]:
            command += ["--filter", f"exclude {exclude}"]

        command += [
            str(self.dist_git.local_project.working_dir) + "/",
            str(self.distro_dir),
        ]

        self.distro_dir.mkdir(parents=True)
        run_command(command)

    def _reset_gitignore(self):
        reset_rules = textwrap.dedent(
            """\
            # Reset gitignore rules
            !*
            """
        )
        Path(self.distro_dir, ".gitignore").write_text(reset_rules)

    def _configure_syncing(self):
        """Populate source-git.yaml"""
        package_config = {}
        if self.upstream_lp.git_url:
            package_config["upstream_project_url"] = self.upstream_lp.git_url
        package_config.update(
            {
                "upstream_ref": self.upstream_ref,
                "downstream_package_name": self.package_name,
                "specfile_path": f"{self.DISTRO_DIR}/{self.package_name}.spec",
                "patch_generation_ignore_paths": [self.DISTRO_DIR],
                "patch_generation_patch_id_digits": self.dist_git.specfile.patch_id_digits,
                "sync_changelog": True,
                "synced_files": [
                    {
                        # The trailing-slash is important, as we want to
                        # sync the content of the directory, not the directory
                        # as a whole.
                        "src": f"{self.DISTRO_DIR}/",
                        "dest": ".",
                        "delete": True,
                        "filters": [
                            "protect .git*",
                            "protect sources",
                            "exclude source-git.yaml",
                            "exclude .gitignore",
                        ],
                    }
                ],
            }
        )
        lookaside_sources = self._get_lookaside_sources()
        if lookaside_sources:
            package_config["sources"] = lookaside_sources

        Path(self.distro_dir, "source-git.yaml").write_text(
            yaml.dump(
                package_config,
                Dumper=SafeDumperWithoutAliases,
                default_flow_style=False,
                sort_keys=False,
            )
        )

    def _adjust_specfile(self):
        """Remove patches from the spec-file

        They are added back when transforming source-git to dist-git.
        """
        spec = Specfile(
            f"{self.distro_dir}/{self.package_name}.spec",
            sources_dir=self.dist_git.local_project.working_dir,
        )
        spec.read_patch_comments()
        spec.remove_patches()

    def _download_source_files(self):
        """Download the source-files from the lookaside cache

        Use the specified package-tool for this.
        """

    def create_from_upstream(self):
        """
        create a source-git repo from upstream
        """
        self._pull_upstream_ref()
        self._populate_distro_dir()
        self._reset_gitignore()
        self._configure_syncing()
        self._adjust_specfile()
        self.local_project.stage(path=self.DISTRO_DIR, force=False)
        self.local_project.commit(
            message="Initialize as a source-git repository", allow_empty=False
        )
        self.dist_git.download_source_files()
        self._run_prep()
        self._rebase_patches(
            get_default_branch(LocalProject(working_dir=self.get_BUILD_dir()).git_repo)
        )
