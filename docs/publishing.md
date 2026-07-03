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

`username = __token__` 是 PyPI 要求的固定值（字面量），`password` 是 token 字符串。

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
# uv 会自动从 ~/.config/pypirc 读 [testpypi] 段
uv publish --publish-url https://test.pypi.org/legacy/ \
           --repository testpypi
```

输出 `Successfully uploaded k8s_mcp_bilbilmyc-0.1.1.tar.gz` 之类即可。

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
uv publish    # 默认从 ~/.config/pypirc [pypi] 段读
```

第一次推生产如果用 Account-scope token 会自动创建项目；Project-scope
需要先在 <https://pypi.org/manage/projects/> 手动 "create" 一次空项目。

## 8. 验证生产发布

```bash
uv tool install k8s-mcp-bilbilmyc
k8s-mcp --version
```

页面 <https://pypi.org/project/k8s-mcp-bilbilmyc/> 应该显示刚推的版本。

## 常见坑

- **403 Invalid or non-existent authentication** —— token 写错，或者 `username` 用了
  自己的 PyPI 用户名而不是 `__token__`。
- **403 The user 'X' isn't allowed to upload to project 'Y'** —— token scope
  跟项目不匹配。要么换 Account-scope token，要么先在 PyPI 手动 create 项目再用
  Project-scope token。
- **400 File already exists** —— 同一版本号二次上传。**PyPI 不允许覆盖**；
  即使是撤回（yank）也不行。修 bug 重发时一定要 bump version。
- **`ImportError` after install** —— 漏了某个 dependency。`uv pip check` 能查
  装出来的环境依赖完整性。

## 不用 PyPI token 的方案：Trusted Publishing

GitHub Actions 跑发布可以用 [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)：
在 PyPI 项目里登记 GitHub repo + workflow，发布时不传 token。**v2 加 CI
时再配**，v1 阶段人工发布就够。
