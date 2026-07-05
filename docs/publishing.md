# 发布到 PyPI / TestPyPI

k8s-mcp 用 [uv](https://docs.astral.sh/uv/) 构建 wheel + sdist，再 `uv publish` 推到
PyPI 或 TestPyPI。这页是维护者发版流程。

> ⚠️ 包名 **不是** `k8s-mcp`：那个名字 [已被一个 27 工具的同类 MCP 占用了](https://pypi.org/project/k8s-mcp/)。
> 本项目发布的包名是 `k8s-mcp-bilbilmyc`。`import` 仍是 `k8s_mcp`，
> CLI 仍是 `k8s-mcp`。

## 1. 注册账号（首次发版才需要）

- **TestPyPI**（推荐先发这里）：<https://test.pypi.org/account/register/>
- **生产 PyPI**：<https://pypi.org/account/register/>

注册时**开启 2FA**（PyPI 强制 TOTP）。如果用 GitHub 登录，到
[Account settings → Security](https://test.pypi.org/manage/account/) 配 TOTP。

## 2. 生成 API token

1. 登录后到 <https://test.pypi.org/manage/account/token/>（生产把域名换成 `pypi.org`）。
2. 点 "Add API token"。
   - **Name**：随便起，比如 `k8s-mcp-bilbilmyc-local`。
   - **Scope**：
     - **Project scope**（推荐）—— 只能上传这一个项目，需要先在 PyPI 手动
       "create" 一次空项目。
     - **Account scope** —— 上传这个账号下所有项目（包括未来的）。
3. 点 "Add token"，**马上复制** `pypi-...` 开头的那串——**只显示一次**。
4. 妥善保管。**不要**提交到 git。

## 3. 写到 `~/.config/pypirc`（推荐方式）

```ini
[distutils]
  index-servers =
      pypi
      testpypi

[pypi]
  username = __token__
  password = pypi-AgEIcHl...   # 生产 PyPI 的 token

[testpypi]
  username = __token__
  password = pypi-AgENdGVz...   # TestPyPI 的 token
```

`username = __token__` 是 PyPI 要求的固定值（字面量），`password` 是 token
字符串。section header 与 `key = value` 之间**用 2 空格缩进**，INI 解析
不在意缩进但保持风格统一，便于后面用脚本抓 token。

文件权限收紧：

```bash
chmod 600 ~/.config/pypirc
```

## 4. 准备发布

```bash
# 1) 改 pyproject.toml 的 version
#    Bump 规则：v0.1.0 → v0.1.1 (bugfix) → v0.2.0 (新功能) → v1.0.0 (稳定)

# 2) 跑测试 + lint（发版前必过）
uv run pytest -q
uv run ruff check src tests

# 3) 跑一次 build 看产物
uv build
ls -la dist/
# 期待：
#   k8s_mcp_bilbilmyc-0.1.1-py3-none-any.whl
#   k8s_mcp_bilbilmyc-0.1.1.tar.gz
```

## 5. 推到 TestPyPI（先试这里）

```bash
# 标准做法：uv 会从 ~/.config/pypirc 读 [testpypi] 段
uv publish --publish-url https://test.pypi.org/legacy/ \
           --repository testpypi
```

输出 `Successfully uploaded k8s_mcp_bilbilmyc-0.1.1.tar.gz` 之类即可。

> ⚠️ **新版 uv 默认走 Trusted Publishing（OIDC），不会自动回退去读
> `~/.config/pypirc`**。如果看到 `Missing credentials for
> https://upload.pypi.org/legacy/`，说明 pypirc 没被读到，**最稳的兜底
> 是把 token 直接塞到环境变量**：
>
> ```bash
> UV_PUBLISH_TOKEN="$(grep -E '^[[:space:]]+password' ~/.config/pypirc \
>                    | sed -E 's/^[[:space:]]+password[[:space:]]*=[[:space:]]*//')" \
>     uv publish --publish-url https://test.pypi.org/legacy/ --repository testpypi
> ```
>
> 这里 `grep '^[[:space:]]+password'` 是为了**容错 section header 缩进**
> ——你 pypirc 里如果 password 行有 2 空格缩进，`^password`（无空格）会
> 匹配不上，token 静默变空，403 一脸懵。

**首次发版**：上一步是创建这个项目。如果用 Project-scope token，需要先在
<https://test.pypi.org/manage/projects/> 手动 "create" 一次空项目（包名
`k8s-mcp-bilbilmyc`）。

## 6. 在 TestPyPI 验证能装

```bash
# 在一个临时 venv 里装
uv venv /tmp/k8s-mcp-verify
uv pip install --python /tmp/k8s-mcp-verify/bin/python \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    k8s-mcp-bilbilmyc

# 跑一下确认能起
KUBECONFIG=/path/to/test-cluster.kubeconfig \
    /tmp/k8s-mcp-verify/bin/k8s-mcp </dev/null
# (stdio 模式没输入会立刻退出，但只要不报 ImportError / 配置错误就算过)
```

或者更简单：装到 uv tool 里直接试：

```bash
uv tool install --index-url https://test.pypi.org/simple/ \
                 --extra-index-url https://pypi.org/simple/ \
                 k8s-mcp-bilbilmyc
k8s-mcp --help    # CLI 入口
```

## 7. 推到生产 PyPI

TestPyPI 验证完再推生产：

```bash
# 标准做法：uv 从 ~/.config/pypirc [pypi] 段读
uv publish
```

第一次推生产如果用 Account-scope token 会自动创建项目；Project-scope
需要先在 <https://pypi.org/manage/projects/> 手动 "create" 一次空项目。

> ⚠️ 如果 pypirc 没生效（看到 `Missing credentials for
> https://upload.pypi.org/legacy/`），用 `UV_PUBLISH_TOKEN` 环境变量绕开，
> 见 §5 末尾的兜底命令。

## 8. 验证生产发布

```bash
uv tool install k8s-mcp-bilbilmyc
k8s-mcp --version
```

页面 <https://pypi.org/project/k8s-mcp-bilbilmyc/> 应该显示刚推的版本。

## 常见坑

- **`Missing credentials for https://upload.pypi.org/legacy/`** —— 新版
  `uv publish` 默认走 [Trusted Publishing](#不用-pypi-token-的方案trusted-publishing)
  （OIDC），不会自动回退去读 `~/.config/pypirc`。解决方案：
  `UV_PUBLISH_TOKEN=$(grep -E '^[[:space:]]+password' ~/.config/pypirc | sed ...) uv publish`。
- **403 Invalid or non-existent authentication** —— token 写错，或者 `username` 用了
  自己的 PyPI 用户名而不是 `__token__`，**或者 pypirc 里的 password 行带缩进但脚本
  `grep '^password'` 没匹配上、token 静默变空**。用 `grep -E '^[[:space:]]+password'`
  容错缩进。
- **403 The user 'X' isn't allowed to upload to project 'Y'** —— token scope
  跟项目不匹配。要么换 Account-scope token，要么先在 PyPI 手动 create 项目再用
  Project-scope token。
- **400 File already exists** —— 同一版本号二次上传。**PyPI 不允许覆盖**；
  即使是撤回（yank）也不行。修 bug 重发时一定要 bump version。
- **`ImportError` after install** —— 漏了某个 dependency。`uv pip check` 能查
  装出来的环境依赖完整性。

## 不用 PyPI token 的方案：Trusted Publishing

GitHub Actions 跑发布可以用 [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)：
在 PyPI 项目里登记 GitHub repo + workflow，发布时不传 token。本仓库 v0.3.0
起采用这个方案（`.github/workflows/release.yml`）。**这是一次性配
置**——下面是从零配好的步骤。

### A. 在 PyPI 项目页登记 Trusted Publisher（一次性）

1. 登录 <https://pypi.org/manage/account/publishing/>。
2. 滚动到 "Add a new pending publisher" → 填：
   - **PyPI Project Name**: `k8s-mcp-bilbilmyc`（包名，不是 GitHub repo 名）。
     如果项目还没在 PyPI 上存在，**这里要先在 PyPI 手动 "create" 一次
     空项目**：<https://pypi.org/manage/projects/> → "Create" → Name 填
     `k8s-mcp-bilbilmyc`、其余留空。
   - **Owner**: `bilbilmyc`
   - **Repository name**: `k8s-mcp`
   - **Workflow filename**: `release.yml`
   - **Environment name**: `pypi`（必须跟 release.yml 里的 `environment: pypi` 字面值一致）
3. 点 "Add"。

PyPI 会发一封确认邮件到你 PyPI 账号绑定的邮箱。**点邮件里的 "Approve"**
链接，Trusted Publisher 才算生效。没点的话 `uv publish` 会 403。

### B. 在 GitHub 端创建 `pypi` environment（一次性）

1. 仓库 → Settings → Environments → "New environment"。
2. Name: `pypi`（必须跟上面填的字面值一致）。
3. 可选：加一个 "Required reviewers" 的 approval gate——只有指定 reviewer
   approve 后 workflow 才能跑。生产建议加；纯自动化测试期可以不开。

### C. 测试一次发版

第一次走完整流程：

```bash
# 1) 手动跑 release dry-run 看会发生什么
bash scripts/bump_and_release.sh 0.3.0 --dry-run

# 2) 实际 bump + commit + tag + push（dry-run 看着 OK 之后）
bash scripts/bump_and_release.sh 0.3.0

# 3) 浏览器看 release.yml workflow 跑：
#    https://github.com/bilbilmyc/k8s-mcp/actions/workflows/release.yml

# 4) PyPI 上看新版本：
#    https://pypi.org/project/k8s-mcp-bilbilmyc/0.3.0/
```

如果第 3 步卡在 "Publish to PyPI" 这一步报 `403`，回头检查 §A 的邮
件是否点了 Approve，以及 §B 的 environment name 是否字面是 `pypi`。

### D. 为什么不直接用 PyPI API token

- Token 会过期、要轮换、要存 GitHub Secrets——多一道泄露面。
- Trusted Publishing 用 OIDC 短期 token，PyPI 验证 GitHub repo +
  workflow path 后才放行——即使 GitHub Actions 被攻，攻击者也得上传
  到一个**他控制的 GitHub repo + workflow**，而这个 repo 必须在 PyPI
  那边白名单里。
- 完全免维护（token 不存在 → 无轮换 → 无过期事故）。

### E. 常见坑

- **PyPI 报 `403 pending publisher not approved`** —— 没点 Approve 邮件，
  或者 §A 的字段跟 release.yml 字面值不匹配。检查 environment name 是
  否是 `pypi`（不是 `PyPI` / `production` 等）。
- **PyPI 报 `400 file already exists`** —— 同一 version 二次上传。PyPI
  不可覆盖；bump 版本号重发。
- **Workflow 报 `failed: Environment pypi not found`** —— §B 的
  environment 没建、或名字拼错。
- **Tag 跟 pyproject version 不一致** —— release.yml 里有一步校验会拒绝
  这种 push。修 tag 的方式：
  ```bash
  git tag -d v0.3.0 && git push origin :refs/tags/v0.3.0   # 删本地+远端
  # 修 pyproject.toml / CHANGELOG.md
  git commit --amend --no-edit
  git tag -a v0.3.0 -m "Release 0.3.0"
  git push origin HEAD --force                                  # 仅限 tag 与 version 不一致时
  git push origin v0.3.0
  ```
  `--force` 在 tag-only release commit 上安全，因为这个 commit 就是为
  了发版而存在的。
