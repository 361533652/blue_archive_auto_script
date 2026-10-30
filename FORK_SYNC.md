# 个人 Fork 工作流（自用说明）

这份文档记录本仓库被改造成「**Fork 即发行版**」之后的使用方式。
如果你几个月后回来忘了自己改过什么，看这一页就够了。

官方仓库：`https://github.com/pur1fying/blue_archive_auto_script`
你的 Fork：`https://github.com/361533652/blue_archive_auto_script`

---

## 1. 三方关系

```text
官方 upstream ──┐
                │  (启动时自动 merge，冲突则拦截启动)
                ▼
          你的 Fork（origin）── 本机的发行版
                │  (启动时自动对齐：别的机器推的提交也在这里收下)
                ▼
          本机 BAAS / 开发目录
```

规矩很简单：

* **origin 永远指向你的 Fork**，代码只从 Fork 进本机；
* **upstream 指向官方**，只是增量来源，不直接参与本机的更新链路；
* 想改功能 → 在本机改 → 提交 → 推到 Fork；下次启动就会生效。

## 2. 相关文件

| 文件 | 作用 |
| --- | --- |
| `deploy/sync_upstream.py` | 同步器本体。只用标准库 + 系统 `git`，不碰 BAAS 配置，可在 .venv 就绪前运行 |
| `Start_BAAS.bat` | 启动器：先同步，再拉起 `BlueArchiveAutoScript*.exe`（没有 exe 就直接跑 `window.py`） |
| `window.py` | GUI 入口。每次 GUI 启动都会先跑一次同步（`BAAS_SYNCED=1` 时跳过，避免同一次启动跑两遍） |
| `deploy/installer/installer.py` | 安装器/更新器。`pre_check` 先同步；`git_update_baas` 在本地领先 origin 时拒绝做覆盖式 `reset --hard` |
| `develop_tools/test_sync_upstream.py` | 回归测试（9 项，纯临时仓库，不联网） |

## 3. 每次启动发生了什么

`sync_with_upstream()` 的顺序：

1. 工作区有未提交改动 → 先 `git stash`（成功合并后再 `stash pop` 回来）；
2. `git fetch upstream`（官方）+ `git fetch origin`（Fork）；
3. **与 Fork 对齐**：`origin/<分支>` 有新提交就合并进来；
4. **吸收官方增量**：`upstream/<分支>` 有新提交就合并进来；
5. 本地领先 Fork → `git push origin <分支>`（推送失败不算致命，下次启动重试）；
6. 任一步冲突 → 保留合并现场、写 `log/sync_upstream_conflict.log`、**阻断启动**。

两种启动方式都覆盖：

* 双击 `Start_BAAS.bat`（推荐）→ 同步 → 启动；
* 双击官方 `BlueArchiveAutoScript*.exe` → 它自己的更新（`dev=true` 时跳过）→ 启动 `window.py` → 同步。

## 4. 冲突了怎么办

启动会被拦下来，GUI 会弹框提示，报告在 `log/sync_upstream_conflict.log`。
冲突现场被完整保留（没有 `merge --abort`、没有 `stash pop`）：

```bash
git status                      # 看冲突文件
git show :1:文件                # 冲突前的共同祖先
git show :2:文件                # 你这边（Fork）
git show :3:文件                # 官方那边
# 两边功能都保留地改完，然后：
git add 文件 && git commit      # 完成这次合并
git push origin master          # 推回 Fork
```

想放弃这次合并：`git merge --abort`。
误被外部流程硬重置过：`git reset --hard refs/baas-backup/auto-sync`（合并前的 HEAD 备份）。

**别为了消掉冲突去删官方代码**——官方新功能要留，自己的改动也要留，两边熔接。

## 5. 本机副本（`E:\baas\BlueArchiveAutoScript_v1.4.3_win_x86_64_full_env`）

`setup.toml` 里改了四处，缺一不可：

```toml
[General]
fork_repo_url = "https://github.com/361533652/blue_archive_auto_script.git"
current_BAAS_version = "<当前 HEAD>"
dev = true
[URLs]
REPO_URL_HTTP = "https://github.com/361533652/blue_archive_auto_script.git"
```

* `REPO_URL_HTTP` 必须等于 origin：官方 exe 的 `check_repo_url()` 会拿 origin 跟它比对，
  不一致就把 origin **改回官方仓库**——那一步会直接毁掉 Fork 工作流。
* `dev = true` 关掉启动器自带的「对着官方仓库 `reset --hard`」更新逻辑。
  代码更新完全交给 `sync_upstream.py`，好处是不再有覆盖本地提交的风险，
  也不会每次启动刷一条假的 `Failed to update the source code to latest version`；
  代价是这条更新链依赖 `sync_upstream.py` 能跑起来（它只用标准库，风险很低）。

## 6. 已知边界

* **官方 exe 是官方构建的**：`deploy/installer/installer.py` 里我加的 Fork 支持
  （`fork_repo_url`、禁止覆盖未推送提交、Fork 模式版本比对）只有在你**重新构建 exe**
  （`deploy/installer/build.bat`）之后才会生效。当前靠 `dev = true` 绕开它。
* `mirrorc_cdk` 路径（用 CDK 下载官方增量包覆盖文件）在 `dev = true` 下不会触发。
  如果你哪天填了 CDK 又关掉 `dev`，它会拿官方包覆盖你的 Fork —— 别这么干。
* 官方 `git_update_baas` 对「本地领先 origin」的保护是给 exe 用的；源码目录里
  真正起作用的是 `sync_upstream.py` 自己的 `is_local_ahead_of_origin`。

## 7. 想彻底回到官方

```bash
git remote set-url origin https://github.com/pur1fying/blue_archive_auto_script.git
# setup.toml：REPO_URL_HTTP 改回官方地址，dev 改回 false，删掉 fork_repo_url
```
`Start_BAAS.bat` 留着也不影响（它同步的就是官方自身，等于空转）。

## 8. 怎么验证这套东西还活着

```bash
python develop_tools/test_sync_upstream.py          # 9 项，临时仓库，不联网
python deploy/sync_upstream.py                      # 真跑一次，看输出
```

真实链路验证过一次：往 Fork 推一个提交 → 副本启动同步 → 副本 HEAD 与文件内容跟着变 →
回滚后再同步一次 → 副本同步回到回滚版。全程没动过副本的工作区文件。
