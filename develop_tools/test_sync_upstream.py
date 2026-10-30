# -*- coding: utf-8 -*-
"""deploy/sync_upstream.py 的回归测试。

用临时仓库模拟 upstream（官方）/ origin（个人 Fork）/ 本地工作区三方关系，
覆盖自动合并、推送 Fork、冲突阻断、脏工作区暂存、领先保护等关键路径。

运行::

    python develop_tools/test_sync_upstream.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "deploy" / "sync_upstream.py"


def load_sync_module():
    """按文件路径加载被测模块。"""
    spec = importlib.util.spec_from_file_location("sync_upstream_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GIT_ENV = dict(os.environ)
GIT_ENV.update(
    {
        "GIT_AUTHOR_NAME": "baas-test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "baas-test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
)


def git(cwd, *args):
    """在指定目录执行 git 命令。"""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=GIT_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def rmtree(path):
    """删除目录树；Windows 上 Git 对象是只读的，需要先补写权限。"""

    def on_error(func, target, exc_info):
        try:
            os.chmod(target, 0o700)
            func(target)
        except Exception:
            pass

    shutil.rmtree(path, ignore_errors=False, onerror=on_error)


def read(path):
    return path.read_text(encoding="utf-8")


class SyncUpstreamTests(unittest.TestCase):
    """三方仓库场景测试。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="baas-sync-test-"))
        self.upstream = self.tmp / "upstream.git"
        self.fork = self.tmp / "fork.git"
        self.local = self.tmp / "local"
        self._build_fixture()

    def tearDown(self):
        rmtree(self.tmp)

    # ---------------------------------------------------------------- fixture
    def _build_fixture(self):
        """upstream(bare) <- fork(bare) <- local(worktree)。"""
        self.upstream.mkdir()
        git(self.upstream, "init", "--bare", "-b", "master")

        scratch = self.tmp / "scratch"
        git(self.tmp, "clone", str(self.upstream), str(scratch))
        write(scratch / "a.txt", "base\n")
        write(scratch / "keep.txt", "keep\n")
        git(scratch, "add", "-A")
        git(scratch, "commit", "-m", "init")
        git(scratch, "push", "origin", "master")
        rmtree(scratch)

        git(self.tmp, "clone", "--bare", str(self.upstream), str(self.fork))

        git(self.tmp, "clone", str(self.fork), str(self.local))
        git(self.local, "remote", "add", "upstream", str(self.upstream))
        # 与真实场景一致：上游地址写进 remote，拉取走上游
        git(self.local, "fetch", "upstream")

    def upstream_commit(self, name, text):
        """在上游 master 上追加一个提交。"""
        scratch = self.tmp / "up-scratch"
        if not scratch.exists():
            git(self.tmp, "clone", str(self.upstream), str(scratch))
        git(scratch, "pull", "origin", "master")
        write(scratch / name, text)
        git(scratch, "add", "-A")
        git(scratch, "commit", "-m", f"upstream: {name}")
        git(scratch, "push", "origin", "master")

    def local_commit(self, name, text, push=True):
        """在本地追加一个提交，默认推送到 Fork。"""
        write(self.local / name, text)
        git(self.local, "add", "-A")
        git(self.local, "commit", "-m", f"local: {name}")
        if push:
            git(self.local, "push", "origin", "master")

    def fork_head(self):
        return git(self.tmp, "ls-remote", str(self.fork), "refs/heads/master").split()[0]

    def local_head(self):
        return git(self.local, "rev-parse", "HEAD")

    # ------------------------------------------------------------------ cases
    def test_clean_merge_then_push_to_fork(self):
        """上游有新提交时：合并进本地并推送到 Fork。"""
        module = load_sync_module()
        self.upstream_commit("b.txt", "from upstream\n")
        before = self.local_head()

        self.assertTrue(module.sync_with_upstream(str(self.local)))

        self.assertTrue((self.local / "b.txt").is_file(), "上游文件应被合并进本地")
        self.assertEqual(self.local_head(), self.fork_head(), "合并结果应已推送到 Fork")
        # 备份引用记录合并前的 HEAD，用于误重置后找回
        self.assertEqual(git(self.local, "rev-parse", module.BACKUP_REF), before)
        self.assertNotEqual(self.local_head(), before, "合并后 HEAD 应前进")

    def test_aligns_with_fork_before_merging_upstream(self):
        """另一台机器推了 Fork：本地先与 Fork 对齐，没有再吸收官方增量。"""
        module = load_sync_module()

        # 另一台机器往 Fork 推一个提交
        other = self.tmp / "other-machine"
        git(self.tmp, "clone", str(self.fork), str(other))
        write(other / "from_pc2.txt", "pushed elsewhere\n")
        git(other, "add", "-A")
        git(other, "commit", "-m", "pc2: fork only commit")
        git(other, "push", "origin", "master")

        self.upstream_commit("b.txt", "from upstream\n")

        self.assertTrue(module.sync_with_upstream(str(self.local)))

        self.assertTrue(
            (self.local / "from_pc2.txt").is_file(), "Fork 上的提交应被收下"
        )
        self.assertTrue((self.local / "b.txt").is_file(), "官方增量也要合并进来")
        self.assertEqual(self.local_head(), self.fork_head())

    def test_no_op_when_up_to_date(self):
        """已是最新时：不报错、不产生新提交。"""
        module = load_sync_module()
        before = self.local_head()

        self.assertTrue(module.sync_with_upstream(str(self.local)))

        self.assertEqual(before, self.local_head())
        self.assertEqual(before, self.fork_head())

    def test_conflict_blocks_and_keeps_merge_state(self):
        """冲突时：返回 False、列出冲突文件、保留合并中间态、写冲突日志。"""
        module = load_sync_module()
        self.local_commit("a.txt", "local change\n")
        self.upstream_commit("a.txt", "upstream change\n")

        self.assertFalse(module.sync_with_upstream(str(self.local)))

        self.assertEqual(module.LAST_CONFLICTS, ["a.txt"])
        status = git(self.local, "status", "--short")
        self.assertIn("UU a.txt", status, "冲突中间态应被保留")
        log_path = self.local / module.CONFLICT_LOG_PATH
        self.assertTrue(log_path.is_file(), "冲突报告应落盘")
        self.assertIn("a.txt", read(log_path))

    def test_dirty_worktree_is_stashed_and_restored(self):
        """脏工作区：自动 stash、合并、再 pop 回来。"""
        module = load_sync_module()
        self.upstream_commit("b.txt", "from upstream\n")
        write(self.local / "keep.txt", "keep\nlocal edit\n")

        self.assertTrue(module.sync_with_upstream(str(self.local)))

        self.assertEqual(read(self.local / "keep.txt"), "keep\nlocal edit\n")
        self.assertTrue((self.local / "b.txt").is_file())
        self.assertEqual(git(self.local, "stash", "list"), "")
        self.assertNotEqual(git(self.local, "status", "--short"), "")

    def test_local_ahead_detection(self):
        """领先判定：未推送为 True，推送后为 False。"""
        module = load_sync_module()
        module._REPO_DIR = str(self.local)

        self.assertFalse(module.is_local_ahead_of_origin())
        self.local_commit("c.txt", "local only\n", push=False)
        self.assertTrue(module.is_local_ahead_of_origin())
        git(self.local, "push", "origin", "master")
        git(self.local, "fetch", "origin")
        self.assertFalse(module.is_local_ahead_of_origin())

    def test_push_failure_keeps_local_commit(self):
        """推送失败：返回 True 但本地提交保留，Fork 未更新。"""
        module = load_sync_module()
        fork_before = self.fork_head()
        # 用一个不存在的远程地址模拟推送失败（保留 origin 名字以便领先判定）
        git(self.local, "remote", "set-url", "--push", "origin", str(self.tmp / "nope.git"))
        self.local_commit("d.txt", "unsent\n", push=False)

        self.assertTrue(module.sync_with_upstream(str(self.local)))

        self.assertTrue((self.local / "d.txt").is_file(), "本地提交必须保留")
        self.assertEqual(self.fork_head(), fork_before, "Fork 不应被改动")

    def test_repo_dir_drives_all_git_calls(self):
        """传入 repo_dir 时不依赖当前工作目录。"""
        module = load_sync_module()
        self.upstream_commit("e.txt", "from upstream\n")
        other_cwd = self.tmp / "elsewhere"
        other_cwd.mkdir()
        old_cwd = os.getcwd()
        os.chdir(other_cwd)
        try:
            self.assertTrue(module.sync_with_upstream(str(self.local)))
        finally:
            os.chdir(old_cwd)
        self.assertTrue((self.local / "e.txt").is_file())
        self.assertEqual(self.local_head(), self.fork_head())

    def test_non_git_dir_is_skipped(self):
        """非 Git 目录：跳过同步并返回 True，不影响启动。"""
        module = load_sync_module()
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertTrue(module.sync_with_upstream(str(plain)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
