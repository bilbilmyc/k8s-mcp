# 发布到 PyPI

k8s-mcp 的 PyPI 发版**只走 GitHub Actions**——本地 `uv publish` 留作
附录里的应急 fallback，正常情况不用。本页是维护者发版流程。

发布管道：本地 `scripts/bump_and_release.sh` 做 version bump + 提交 +
打 tag + push tag → GitHub Actions `release.yml` 自动跑（OIDC 上 PyPI
+ 创建 GitHub Release）。全程**不**需要 PyPI API token，也不需要在
本地配 `~/.pypirc`。

> ⚠️ 包名 **不是** `k8s-mcp`：那个名字 [已被一个 27 工具的同类 MCP 占用了](https://pypi.org/project/k8s-mcp/)。
> 本项目发布的包名是 `k8s-mcp-bilbilmyc`。`import` 仍是 `k8s_mcp`，
> CLI 仍是 `k8s-mcp`。

---

## 1. 一次性配置：PyPI Trusted Publisher

> 这一步**只**需要在第一次配，或者换 GitHub repo / workflow 路径时
> 重新配。已经配过的话直接跳到 §2。

走 [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)：
PyPI 项目页登记 GitHub repo + workflow，发布时 GitHub Actions 用
短期 OIDC token 通过 PyPI 校验，**全程不存 PyPI API token**。

### 1.1 PyPI 项目页登记

1. 登录 <https://pypi.org/manage/account/publishing/>。
2. 滚动到 "Add a new pending publisher" → 填：
   - **PyPI Project Name**: `k8s-mcp-bilbilmyc`（包名，不是 GitHub repo 名）。
     如果项目还没在 PyPI 上存在，**先在 PyPI 手动 "create" 一次
     空项目**：<https://pypi.org/manage/projects/> → "Create" →
     Name 填 `k8s-mcp-bilbilmyc`、其余留空。
   - **Owner**: `bilbilmyc`
   - **Repository name**: `k8s-mcp`
   - **Workflow filename**: `release.yml`
   - **Environment name**: `pypi`（必须跟 `release.yml` 里的
     `environment: pypi` 字面值一致）
3. 点 "Add"。

PyPI 会发一封确认邮件到 PyPI 账号绑定的邮箱。**点邮件里的 "Approve"
链接**，Trusted Publisher 才算生效。没点的话 workflow 会 403。

### 1.2 GitHub 端建 `pypi` environment

1. 仓库 → Settings → Environments → "New environment"。
2. Name: `pypi`（必须跟上面填的字面值一致）。
3. 可选：加一个 "Required reviewers" 的 approval gate——只有指定
   reviewer approve 后 workflow 才能跑。生产建议加。

---

## 2. 正常发版流程

发版就是三步：`bump_and_release.sh` 一条命令 + 看 GitHub Actions
跑完 + `uv tool upgrade` 拉新版。

### 2.1 跑发版脚本

```bash
# 1) 改 CHANGELOG.md 的 [Unreleased] 段（手动），
#    写本次发版要进 release notes 的内容
#    (changelog 段会被 release.yml 抓出来填进 GitHub Release body)

# 2) 跑脚本：bump pyproject.toml + commit + tag + push
bash scripts/bump_and_release.sh 0.4.4
```

脚本会做的事：

1. 校验新版本号 `X.Y.Z` 格式
2. 检查 working tree 干净
3. 读当前 version，改 `pyproject.toml` 的 `version = "..."` 行
4. 把 CHANGELOG.md 的 `## [Unreleased]` 提升为 `## [0.4.4] — 2026-07-XX`，
   顶上重新插入新的 `## [Unreleased]`
5. 提交 `pyproject.toml` + `CHANGELOG.md`（commit message: `Release 0.4.4`）
6. 创建 annotated tag `v0.4.4`
7. `git push origin HEAD` + `git push origin v0.4.4`

发版**前**必过：

```bash
uv run pytest -q          # 全部测试过
uv run ruff check src tests
```

**dry-run** 验证脚本会改什么而不真改：

```bash
bash scripts/bump_and_release.sh 0.4.4 --dry-run
```

### 2.2 看 GitHub Actions 跑

push tag 后，`release.yml` 自动触发：

- <https://github.com/bilbilmyc/k8s-mcp/actions/workflows/release.yml>

三个 job：

1. **build** — `uv build` 出 `dist/*.whl` + `*.tar.gz`，校验 tag 跟
   `pyproject.toml` 的 `version` 字面一致再上传 artifact。
2. **publish** — `pypa/gh-action-pypi-publish` 用 OIDC 上 PyPI
   (`skip-existing: true` 防止重发同一 version)。
3. **release** — 把 CHANGELOG 那一节抠出来当 release body + 附
   dist artifact + 创建 GitHub Release。

### 2.3 验证发版成功

```bash
# PyPI 页面
# https://pypi.org/project/k8s-mcp-bilbilmyc/0.4.4/

# 拉新版到本地
uv tool upgrade k8s-mcp-bilbilmyc
k8s-mcp --version    # 应显示 0.4.4
```

GitHub Release 在 <https://github.com/bilbilmyc/k8s-mcp/releases/tag/v0.4.4>。

---

## 3. Tag 跟 pyproject.toml version 不一致怎么办

`release.yml` 的 build job 里有一步校验：tag 必须字面等于
`pyproject.toml` 的 `version`。不一致直接 `::error::` 拒绝。

修法（**这是 tag-only release commit 专用的 `--force` 场景**，
普通代码 commit 不要 force push）：

```bash
# 1) 删本地 + 远端 tag
git tag -d v0.4.4
git push origin :refs/tags/v0.4.4

# 2) 修 pyproject.toml / CHANGELOG.md
#    （用 git commit --amend 把 release commit 替换掉）
git commit --amend --no-edit
git tag -a v0.4.4 -m "Release 0.4.4"

# 3) 强制推（仅限 release commit，普通 commit 严禁 --force）
git push origin HEAD --force
git push origin v0.4.4
```

---

## 4. 常见问题

- **PyPI 报 `403 pending publisher not approved`** —— §1.1 的邮件没
  点 Approve，或 §1.1 / §1.2 的 environment name 跟 `release.yml`
  字面值不匹配（必须是 `pypi`，不是 `PyPI` / `production`）。
- **PyPI 报 `400 file already exists`** —— 同一 version 二次上传。
  PyPI 不可覆盖；bump 版本号重发。
- **Workflow 报 `failed: Environment pypi not found`** —— §1.2 的
  environment 没建、或名字拼错。
- **PyPI 报 `403 Invalid or non-existent authentication`** —— 一般
  是 Trusted Publisher 没配好；不会发生在纯 OIDC 流程里。
- **GitHub Release body 是空的 / 不是预期的** —— CHANGELOG 那一节
  没匹配上；`release.yml` 用的正则 `## \[<version>\][^\n]*\n(.*?)(?=^## |\Z)`
  要求 `## [X.Y.Z] — DATE` 严格格式（`[` `]` 与 ` — DATE` 之间是空格 + em-dash）。

---

## 附录 A：应急手动发版（`uv publish`）

> 正常情况**不要**走这条。`uv publish` 留作：
>
> 1. Trusted Publisher 配错、邮件没点 Approve、GitHub Actions 挂了
> 2. PyPI 服务侧故障，OIDC 校验不能完成
> 3. 想先发 TestPyPI 验证再上生产

### A.1 配 PyPI API token

1. <https://pypi.org/manage/account/token/> → "Add API token"。
   - Name 随便起。
   - Scope：**Project scope**（推荐）—— 只能传这个项目；
     或 **Account scope**——能传账号下所有项目。
2. 复制 `pypi-...` 开头的 token（**只显示一次**）。

### A.2 配置 `~/.pypirc`

`uv publish` 只读 `~/.pypirc`，**不**读 `~/.config/pypirc`。
最简办法是软链：

```bash
mkdir -p ~/.config
cat > ~/.config/pypirc <<'EOF'
[distutils]
  index-servers =
      pypi
      testpypi

[pypi]
  username = __token__
  password = pypi-AgEIcHl...

[testpypi]
  username = __token__
  password = pypi-AgENdGVz...
EOF
chmod 600 ~/.config/pypirc
ln -sf ~/.config/pypirc ~/.pypirc
```

`username = __token__` 是 PyPI 要求的固定字面值，`password` 是 token。
section header 与 `key = value` 之间用 2 空格缩进。

### A.3 发到 TestPyPI 验证

```bash
# 标准做法
uv publish --publish-url https://test.pypi.org/legacy/ \
           --repository testpypi

# 或用环境变量绕开 pypirc：
UV_PUBLISH_TOKEN="$(grep -E '^[[:space:]]+password' ~/.config/pypirc \
                   | sed -E 's/^[[:space:]]+password[[:space:]]*=[[:space:]]*//')" \
    uv publish --publish-url https://test.pypi.org/legacy/ --repository testpypi
```

> ⚠️ 新版 `uv` 默认走 Trusted Publishing (OIDC)，不会自动回退去
> 读 `~/.pypirc`。看到 `Missing credentials for
> https://upload.pypi.org/legacy/` 就用上面 `UV_PUBLISH_TOKEN` 兜底
> 命令。`grep '^[[:space:]]+password'` 是为了**容错 section header
> 缩进**——password 行带 2 空格缩进时，`^password`（无空格）会匹配
> 不上、token 静默变空，403 一脸懵。

### A.4 发到生产 PyPI

```bash
uv publish
```

第一次推生产用 Project-scope token 会自动创建项目（前提是 §A.1
已配）；Account-scope token 不需要。

### A.5 常见坑

- **`Missing credentials for https://upload.pypi.org/legacy/`** ——
  见 §A.3，用 `UV_PUBLISH_TOKEN` 兜底。
- **403 Invalid or non-existent authentication** —— token 写错、
  `username` 用了自己的 PyPI 用户名（**必须**是 `__token__`），或
  pypirc 缩进导致 `grep '^password'` 没匹配上、token 静默变空。
- **403 The user 'X' isn't allowed to upload to project 'Y'** ——
  token scope 跟项目不匹配。换 Account-scope token，或先在 PyPI
  手动 create 项目再用 Project-scope token。
- **400 File already exists** —— 同一 version 二次上传。PyPI **不
  允许覆盖**；bump version 重发。
- **`ImportError` after install** —— 漏 dependency。`uv pip check`
  能查装出来的环境依赖完整性。

### A.6 Secret 卫生

- `~/.pypirc` / `~/.netrc` / `.env` **绝不**要 `cat` / `sed` 出来——
  token 进入对话 context 等于泄露（一旦泄露视同已失密，到 PyPI
  那里 rotate）。
- 引用 secret 时用**用它的工具**（`uv publish` 自己读 pypirc），
  不要把文件内容读到对话里。
- 真要展示时用 `sed -E 's/(password\s*=\s*).*/\1***/'`（锚到字段名 +
  等号，不是行首）这种替换锚定。
