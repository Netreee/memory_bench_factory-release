# dsh profile（仓库钉住的 dsh 启动配置）

## 安装 CLI

先安装 Node.js 22.19.0 以上与 npm，再安装与根目录 `package.json` 一致的固定版本：

```text
npm install --global @deepseek-ai/dsh@0.1.2-rc.1
dsh --version
```

将 `configs/env/secrets.env.example` 复制为 `configs/env/secrets.env`，填写
`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`。模型名与接口协议从发布配置或 experiment 读取。
运行器从 `PATH` 查找 `dsh`；需要指定位置时填写 `DSH_BIN`。
Windows 用 `Get-Command dsh.cmd` 获取 npm 启动器，路径写成
`C:/Users/your-name/AppData/Roaming/npm/dsh.cmd`；Linux/macOS 用 `command -v dsh`。
CLI 安装完成后，运行器会为每次评测准备下述 profile，无需手动复制生成文件。

## Profile 的作用

`profiles/headless/` 是 DeepSeek Harness（dsh）的启动 profile：它声明**这个 harness
启动哪些 dsh bundle**。native CLI runner 在每次 run 时把它复制到该 run 的
`$DSH_HOME/profiles/`，再用 `dsh --profile headless` 启动，因此 profile 是「被测
harness 的一部分」，改动会改变 agent 实际能用的工具和插件。

放在 `configs/` 而不是 `agent_harnesses/` 里的原因：

- 它和 `systems.toml`、experiment TOML 一样是**仓库钉住的配置**，不是 Python 包数据；
- 里面的 bundle 名不带版本，跟随仓库根 `package.json` 的 `@deepseek-ai/dsh` pin 解析；
- 控制面只支持源码 checkout 运行（`configs/`、`input/` 都按 `REPOSITORY_ROOT` 解析），
  所以不需要把它打进 Python 包。

## 只分发 `headless/package.json`

仓库里**只保留 `package.json`**，它声明 `dsh.profile.bundles` =
`["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]` 与 `patchReload: startup`。

其余三个文件都由 dsh 自己在 `$DSH_HOME/profiles/<name>/` 下生成，随仓库分发没有意义
（dsh 0.1.2-rc.1 实测）：

| 文件 | dsh 的行为 | 为什么不分发 |
|---|---|---|
| `cordis.yml` | `prepareProfile()` 里 `writeFileSync`，**每次 boot 无条件重写** | 分发出去的副本会被立刻覆盖，我们手写的注释反而是噪音 |
| `cordis.patch.yml` | profile manifest 已存在时不写；完整 bootstrap（目录不存在）时按模板写入，boot 时解析 | 它是"以后加覆盖"的层，当前内容是空 `[]`；缺失时 dsh 正常启动 |
| `pnpm-workspace.yaml` | 同上，仅 bootstrap 时写入 | dsh 的 boot 路径不读它——只有 `dsh plugin`（一个 pnpm 转发器）会用它，而本仓库从不调用 |

`$DSH_HOME/settings.yaml` 是另一回事：它是 **dsh 自己的 settings 文档**
（`dsh-settings-file` 默认读 `<harness home>/settings.yaml`），由 runner 的
`_prepare_dsh_home` 生成，声明 `agent-default-model` 与我们的 endpoint/provider。
这个文件是必需的，且 dsh 不会替你生成。

## 验收

`tests/test_dsh_profile.py` 用离线的 `dsh --profile headless --dump-config` 断言
「我们钉住的 profile」与「dsh 完全自行 bootstrap 的 profile」组合出的插件树**完全
一致**。本机没有 dsh 时该测试整体跳过。手工复核：

```bash
DSH_HOME=$(mktemp -d) dsh --profile headless --dump-config | head
```

## 改动规则

- bundle 列表变了（改 `package.json`）就是 harness 变了：profile 内容当前**不进入
  runtime fingerprint**，不会自动换 run 目录，需要新 run 验证并记录影响（必要时 bump
  `NATIVE_CLI_ADAPTER_VERSION`）。
- `package.json` 缺失会让 `preflight` 直接报错，不会静默退化成「没有 profile 的 dsh」。
