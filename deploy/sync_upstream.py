"""上游官方仓库自动同步器。

启动时按两步把代码带到位，然后推送到个人的 Fork 仓库（origin）：

1. 与个人 Fork 对齐（``origin/<分支>``）——Fork 才是这台机器的发行版，
   另一台机器推了东西进来时要先收下；
2. 合并官方上游（``upstream/<分支>``）的增量。

两步出现代码冲突都会立即阻断启动并输出排查指引。

设计约束
--------
* 只使用标准库与系统 ``git``，不依赖 pygit2 / requests，也不读取 BAAS 的
  业务配置：同步发生在启动链路最前端，此时 .venv、配置文件都可能尚未就绪。
* 所有函数都接受 ``repo_dir``，默认使用当前工作目录。启动器（installer、
  window.py）不一定以仓库根目录为 cwd，必须显式传入路径。
* 本地领先 origin 时绝不执行覆盖式更新（见 :func:`is_local_ahead_of_origin`）。
  ``deploy/installer/installer.py`` 里的 ``git_update_baas`` 会执行
  ``reset --hard origin/<分支>``，一旦推送失败，那一下就会把刚合并进来的上游
  提交整段抹掉，白合一轮。
* 合并前把 HEAD 记进 ``refs/baas-backup/auto-sync``，即使被外部流程硬重置，
  也能用 ``git reset --hard refs/baas-backup/auto-sync`` 找回。
* 冲突时保留合并中间态（不 ``merge --abort``、不 ``stash pop``），让冲突现场
  完整留给人工/AI 排查。

用法::

    python deploy/sync_upstream.py     # 退出码 0 表示可继续启动，1 表示冲突阻断
"""

import os
import subprocess
import sys
import time
from typing import List, Optional

# 兼容 Windows 控制台（GBK 代码页）打印中文与符号
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 官方上游仓库；可用环境变量 BAAS_UPSTREAM_REPO 覆盖
UPSTREAM_URL = os.environ.get(
    "BAAS_UPSTREAM_REPO",
    "https://github.com/pur1fying/blue_archive_auto_script.git",
)
UPSTREAM_NAME = "upstream"
ORIGIN_NAME = "origin"
STASH_MESSAGE = "auto-sync-pre-stash"
# 合并前的本地 HEAD 备份引用（滚动覆盖，配合 reflog 使用）
BACKUP_REF = "refs/baas-backup/auto-sync"
# 冲突报告落盘位置（相对仓库根目录）：pythonw 启动时控制台不可见
CONFLICT_LOG_PATH = os.path.join("log", "sync_upstream_conflict.log")

# 本次同步的目标仓库目录；None 表示使用当前工作目录
_REPO_DIR: Optional[str] = None

# 最近一次同步检测到的冲突文件（供调用方写日志或弹窗展示）
LAST_CONFLICTS: List[str] = []


def run_git(
    cmd: List[str], check: bool = False, capture_output: bool = True
) -> subprocess.CompletedProcess:
    """在目标仓库中运行 Git 命令并统一编码。

    Args:
        cmd: 完整命令行列表，首元素为 ``git``。
        check: 是否在非零退出码时抛出 CalledProcessError。
        capture_output: 是否捕获标准输出与标准错误。

    Returns:
        subprocess.CompletedProcess: 命令执行结果。
    """
    return subprocess.run(
        cmd,
        cwd=_REPO_DIR,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def find_repo_root(repo_dir: Optional[str] = None) -> Optional[str]:
    """定位仓库根目录。

    Args:
        repo_dir: 待检查的目录，缺省使用当前工作目录。

    Returns:
        str | None: 仓库根目录绝对路径；该目录不在 Git 仓库内时返回 None。
    """
    res = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode != 0:
        return None
    root = res.stdout.strip()
    return root or None


def get_current_branch() -> str:
    """获取当前所在的 Git 分支名。

    Returns:
        str: 分支名；处于游离头指针时返回 ``HEAD``。
    """
    res = run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = res.stdout.strip()
    return branch or "master"


def get_remote_names() -> List[str]:
    """列出已配置的远程仓库名称。

    Returns:
        List[str]: 远程仓库名列表，命令失败时为空列表。
    """
    res = run_git(["git", "remote"])
    if res.returncode != 0:
        return []
    return [name.strip() for name in res.stdout.splitlines() if name.strip()]


def ensure_upstream() -> bool:
    """确保 upstream 远程仓库已配置为官方地址。

    只补齐缺失的远程名；已存在时不动它的 URL，避免覆盖用户自选的镜像源。

    Returns:
        bool: 已存在或添加成功返回 True。
    """
    if UPSTREAM_NAME in get_remote_names():
        return True
    print(f"[Sync] 未检测到 {UPSTREAM_NAME} 远程源，正在添加 {UPSTREAM_URL} ...")
    added = run_git(["git", "remote", "add", UPSTREAM_NAME, UPSTREAM_URL])
    if added.returncode != 0:
        print(f"[Sync] 添加 {UPSTREAM_NAME} 失败：{(added.stderr or '').strip()}")
        return False
    return True


def check_dirty_worktree() -> bool:
    """检查已跟踪文件是否存在未提交的改动。

    仅看已跟踪文件：未跟踪文件不会参与合并，也不需要暂存。

    Returns:
        bool: 存在未提交改动返回 True。
    """
    res1 = run_git(["git", "diff", "--quiet"])
    res2 = run_git(["git", "diff", "--cached", "--quiet"])
    return res1.returncode != 0 or res2.returncode != 0


def get_conflicted_files() -> List[str]:
    """获取处于合并冲突状态的文件列表。

    Returns:
        List[str]: 冲突文件相对路径列表。
    """
    res = run_git(["git", "diff", "--name-only", "--diff-filter=U"])
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def has_origin_remote() -> bool:
    """判断是否配置了 origin 远程仓库。

    Returns:
        bool: 存在 origin 返回 True。
    """
    return ORIGIN_NAME in get_remote_names()


def _ref_exists(ref: str) -> bool:
    """判断本地是否存在该引用（如 ``origin/master``）。

    Args:
        ref: 引用名。

    Returns:
        bool: 存在返回 True。
    """
    return run_git(["git", "rev-parse", "--verify", "--quiet", ref]).returncode == 0


def is_local_ahead_of_origin(branch: Optional[str] = None) -> bool:
    """判断本地分支是否存在尚未推送到 origin 的提交。

    调用方用它决定能否执行覆盖式的 ``git reset --hard origin/<分支>``：
    本地领先时该重置会丢弃尚未推送的提交（包括刚合并进来的上游更新），
    因此必须跳过。

    Args:
        branch: 待检查的分支名，缺省使用当前分支。

    Returns:
        bool: 本地领先 origin 返回 True；未配置 origin 时返回 False，
              让上层继续执行能够自动补齐远程仓库的初始化流程。
    """
    if not has_origin_remote():
        return False
    branch = branch or get_current_branch()
    remote_ref = f"{ORIGIN_NAME}/{branch}"
    if not _ref_exists(remote_ref):
        # origin 上还没有这个分支：本地提交全部算领先，推送时会新建分支。
        return _ref_exists("HEAD")
    res = run_git(["git", "log", f"{remote_ref}..HEAD", "--oneline"])
    return bool(res.stdout.strip())


def backup_head() -> None:
    """把当前 HEAD 记录到备份引用，供误重置后找回。

    这是一个滚动引用：每次合并前覆盖为当时的 HEAD。配合 ``git reflog``
    可以定位到更早的状态。
    """
    res = run_git(["git", "update-ref", BACKUP_REF, "HEAD"])
    if res.returncode == 0:
        print(f"[Sync] 已把当前 HEAD 备份到 {BACKUP_REF}")


def push_to_origin(branch: Optional[str] = None) -> bool:
    """把当前分支推送到 origin，无需推送时视为成功。

    Args:
        branch: 目标分支名，缺省使用当前分支。

    Returns:
        bool: 已推送、无需推送或推送成功返回 True；推送失败返回 False。
    """
    if not has_origin_remote():
        print(f"[Sync] 未配置 {ORIGIN_NAME} 远程源，跳过推送（更新保留在本地）。")
        return False

    branch = branch or get_current_branch()
    if not is_local_ahead_of_origin(branch):
        print(f"[Sync] {ORIGIN_NAME}/{branch} 已是最新，无需推送。")
        return True

    print(f"[Sync] 本地领先 {ORIGIN_NAME}/{branch}，正在推送...")
    push_res = run_git(["git", "push", ORIGIN_NAME, branch])
    if push_res.returncode == 0:
        print("[Sync] 个人 Fork 仓库已同步更新至最新。")
        return True
    detail = (push_res.stderr or push_res.stdout or "").strip().splitlines()
    if detail:
        print(f"[Sync] 推送至 {ORIGIN_NAME} 失败：{detail[-1]}")
    print(
        f"[Sync] 推送未完成（网络波动或权限问题），"
        f"更新已保留在本地，下次启动会自动重试推送。"
    )
    return False


def write_conflict_log(conflicts: List[str]) -> Optional[str]:
    """把冲突报告落到仓库的 log 目录。

    启动器可能用 pythonw.exe 拉起 BAAS，控制台输出彻底不可见；冲突信息必须
    留一份在磁盘上，用户排查时才有东西可看。

    Args:
        conflicts: 冲突文件列表。

    Returns:
        str | None: 写入成功返回日志路径。
    """
    root = _REPO_DIR or os.getcwd()
    log_path = os.path.join(root, CONFLICT_LOG_PATH)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S]") + "\n")
            f.write("BAAS 启动被拦截：与官方上游仓库存在代码冲突。\n")
            f.write("冲突文件：\n")
            for path in conflicts or ["（未取到冲突文件，请执行 git status）"]:
                f.write(f"  - {path}\n")
            f.write(
                "处理：保留双方功能后 git add / git commit；"
                "放弃合并用 git merge --abort；"
                f"合并前的 HEAD 备份在 {BACKUP_REF}\n\n"
            )
        print(f"[Sync] 冲突报告已写入 {log_path}")
        return log_path
    except Exception as e:
        print(f"[Sync] 写冲突日志失败：{e}")
        return None


def merge_ref(ref: str, message: str) -> "tuple[bool, List[str]]":
    """把 ``ref`` 合并进当前分支。

    Args:
        ref: 形如 ``upstream/master`` 的引用名。
        message: 合并提交的说明。

    Returns:
        tuple[bool, List[str]]: 是否成功，以及冲突文件列表（成功时为空）。
    """
    pending = run_git(["git", "log", f"HEAD..{ref}", "--oneline"]).stdout.strip()
    if not pending:
        print(f"[Sync] {ref} 已是最新，无需合并。")
        return True, []

    count = len(pending.splitlines())
    print(f"[Sync] 发现 {ref} 的 {count} 个新提交，正在合并...")
    # 合并前留一次本地备份，出错时可以回到合并前的 HEAD
    backup_head()
    merge_res = run_git(["git", "merge", ref, "--no-edit", "-m", message])
    conflicts = get_conflicted_files()
    if merge_res.returncode != 0 or conflicts:
        return False, conflicts
    print(f"[Sync] {ref} 合并成功。")
    return True, []


def _print_conflict_report(conflicts: List[str]) -> None:
    """打印醒目的冲突阻断报告。

    Args:
        conflicts: 冲突文件列表。
    """
    print("\n" + "!" * 64)
    print("【BAAS 启动拦截】检测到与官方上游仓库存在代码冲突！")
    print("!" * 64)
    print("合并已被暂停，冲突文件列表如下：")
    for path in conflicts:
        print(f"   x {path}")
    print("")
    print("已安全阻断启动，保护本地代码环境。")
    print("排查要点：")
    print("   1. git status                 查看冲突文件")
    print("   2. git show :1:<file>         冲突前的共同祖先")
    print("      git show :2:<file>         本地（Fork）一侧")
    print("      git show :3:<file>         官方（upstream）一侧")
    print("   3. 两边功能都要保留，改完后 git add <file> && git commit")
    print("   4. 若想放弃本次合并：git merge --abort")
    print("   5. 未提交的本地改动可能暂存在 git stash 中，见 git stash list")
    print(f"   6. 合并前的本地 HEAD 备份在 {BACKUP_REF}")
    print("!" * 64 + "\n")


def sync_with_upstream(repo_dir: Optional[str] = None) -> bool:
    """执行上游同步、冲突检测与 Fork 推送。

    Args:
        repo_dir: 目标仓库目录，缺省使用当前工作目录。

    Returns:
        bool: True 表示同步完成或无需同步且环境安全；
              False 表示发生冲突已阻断启动。
    """
    global _REPO_DIR
    global LAST_CONFLICTS

    LAST_CONFLICTS = []
    print("=" * 60)
    print("[BAAS] 启动前检查：正在同步官方上游代码...")
    print("=" * 60)

    root = find_repo_root(repo_dir)
    if root is None:
        print(f"[Sync] {repo_dir or os.getcwd()} 不是 Git 仓库，跳过上游同步。")
        return True
    _REPO_DIR = root
    print(f"[Sync] 仓库目录: {root}")

    try:
        if not ensure_upstream():
            print(f"[Sync] 配置 {UPSTREAM_NAME} 远程仓库失败，跳过同步继续启动。")
            return True

        branch = get_current_branch()
        print(f"[Sync] 当前分支: {branch}")

        has_stash = False
        merge_conflicted = False
        if check_dirty_worktree():
            print("[Sync] 检测到工作区有未提交的代码改动，正在自动创建临时暂存...")
            stash_res = run_git(["git", "stash", "push", "-m", STASH_MESSAGE])
            has_stash = "Saved working directory" in stash_res.stdout

        try:
            print(f"[Sync] 正在拉取官方上游最新状态 (git fetch {UPSTREAM_NAME})...")
            upstream_ref = f"{UPSTREAM_NAME}/{branch}"
            fetch_res = run_git(["git", "fetch", UPSTREAM_NAME])
            upstream_reachable = fetch_res.returncode == 0
            if not upstream_reachable:
                print("[Sync] 无法连接到上游仓库（可能网络波动），本次只与个人 Fork 对齐。")
            if has_origin_remote():
                print(f"[Sync] 正在拉取个人 Fork 最新状态 (git fetch {ORIGIN_NAME})...")
                run_git(["git", "fetch", ORIGIN_NAME])

            # 先对自己的发行版（Fork）对齐，再吸收官方增量。
            merge_targets = []
            origin_ref = f"{ORIGIN_NAME}/{branch}"
            if has_origin_remote() and _ref_exists(origin_ref):
                merge_targets.append(
                    (origin_ref, f"merge: 同步个人 Fork {origin_ref} 更新")
                )
            if upstream_reachable:
                if _ref_exists(upstream_ref):
                    merge_targets.append(
                        (upstream_ref, f"merge: 同步官方 {upstream_ref} 更新")
                    )
                else:
                    print(f"[Sync] 上游不存在对应分支 {upstream_ref}，跳过与上游的合并。")

            for ref, message in merge_targets:
                ok, conflicts = merge_ref(ref, message)
                if not ok:
                    merge_conflicted = True
                    LAST_CONFLICTS = conflicts
                    write_conflict_log(conflicts)
                    _print_conflict_report(conflicts)
                    return False

            # 合并与否都要尝试推送：上一次推送失败时，合并结果仍留在本地，
            # 这里负责在下次启动把它补推到 Fork。
            push_to_origin(branch)
            return True

        finally:
            # 冲突未解决时不能 pop：工作区正处于合并中间态，恢复了也无法编译。
            # 暂存条目会完整保留，排错时可用 `git stash list` 找回。
            if has_stash and not merge_conflicted:
                print("[Sync] 正在恢复之前未提交的修改 (git stash pop)...")
                run_git(["git", "stash", "pop"])
                if get_conflicted_files():
                    print("[Sync] 恢复工作区修改时产生局部冲突，请查看相应文件！")
            elif has_stash and merge_conflicted:
                print("[Sync] 未提交的改动已保留在 git stash 中（合并冲突未解决，暂不恢复）。")
    finally:
        _REPO_DIR = None


def main() -> int:
    """命令行入口。

    Returns:
        int: 0 表示可继续启动，1 表示冲突阻断。
    """
    return 0 if sync_with_upstream() else 1


if __name__ == "__main__":
    sys.exit(main())
