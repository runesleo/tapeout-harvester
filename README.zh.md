# TapeOut Harvester

这是一个从真实 TapeOut/BEM 自动收割流程抽出来的、**fail-closed（不确定就停）** 的本地执行器：可以只自动领取 BEM，也可以在明确配置后，把**本轮 claim 交易实际归因到的钱**按比例卖出。

> 本地候选版本：`0.1.0rc3`（尚未发布）。默认 dry-run。真钱执行必须同时满足 `runtime.live_enabled = true` 和命令行 `--live` 两道开关。

[English](README.md) · [给 AI 的安装说明](AI_SETUP.md) · [安全说明](SECURITY.md)

## RC3 新增：长期运行可靠性

这次不是改资金策略，而是把真实生产里踩到的两个问题补进公开工具：RPC 看似在线但实际 `eth_call` 已坏，以及常驻进程悄悄消失却没有明显信号。

- 选 RPC 时不再只测 chainId / 区块高度，还必须真实读取一次 TapeOut `pending`；当前状态树坏掉的节点会直接跳过。
- `watch` 每轮重新建立 adapter，不会把几个小时前选中的 RPC 永久钉死。
- 每轮 watch 都会在 state 旁边写一个脱敏 heartbeat；fatal cycle error 也会留下错误心跳。
- 新增纯只读 `doctor`：检查 RPC、state/inflight、heartbeat 是否过期；不加载 signer、不广播交易。

```bash
tapeout-harvester --config config.toml doctor
```

heartbeat 过期、上一轮 ERROR、执行结果处于 BLOCKED 类状态，或者本地 journal 已持久化为 `BLOCKED_SAFE`，都会明确返回 degraded。heartbeat 写入使用独立阻塞锁，避免多个 watch 实例同时改同一个临时文件。没有 heartbeat 时会显示 `NOT_OBSERVED`，但只有 journal 本身健康时才算整体健康。

## RC2 解决了什么

RC1 的独立 Codex 审查抓出了几类不能带着公开的问题：用钱包余额差判断 claim/swap、恢复时没有绑定原 wallet/策略、approve 后失败可能残留授权、卖出路径在 claim 前提前模拟、累计 gas cap 不严、TOML 字符串布尔值、状态落盘耐久、RPC 错误泄漏等。

RC2 改为：

- claim / swap / unwrap 都从**对应交易 receipt 的事件**做归因，不再靠整个钱包的余额差猜；
- claim 确认后才进入 approve，approve 确认后才构造 swap；
- pending cycle 绑定 chain、wallet、miner set、contracts、sell policy、slippage/gas/confirmation/timeout 等执行身份，配置变了就停；
- receipt 必须来自 canonical block，并达到 `min_confirmations` 才推进下一阶段；
- 广播前把 tx hash / intent / gas commitment 先 `fsync` 到 state；
- 整轮按最坏 `gas * gasPrice` 累计预算，而不是每笔分别过上限；
- 本轮创建 exact allowance 前预留撤权 gas；已知的卖出失败会先尝试清理，再进入 blocked；
- 本机对同一 chain+wallet 使用稳定共享锁，并在每次签名前再检查 nonce；
- `live_enabled` / `unwrap_native` 必须真的是 TOML boolean，`"false"` 这种字符串不会被误当成 True；
- runtime 错误默认不回显第三方 provider 的异常正文；未知细节统一收敛为 `RUNTIME_DETAIL_REDACTED`，避免 URL/path/token 泄漏；
- keyring 只接受明确允许的安全 OS backend。

## dry-run 到底验证什么

```bash
tapeout-harvester --config config.toml run
```

dry-run 不加载 signer、不广播。它会：

1. 读取 pending；
2. 构造并模拟 claim；
3. 检查 claim 的 gas cap / reserve；
4. 如果配置了卖出，读取一个 sell quote preview。

它**不会假装在 claim 尚未发生时就能完整模拟 approve/swap**。真实卖出路径严格按：

```text
claim → confirmations → 从 claim receipt 归因到账
      → fresh quote → exact approve → confirmations
      → fresh quote → swap simulation → sign/broadcast
      → 从 swap receipt 归因到账
      → 必要时撤销本轮授权 → 可选 unwrap
```

## 安装

```bash
git clone <REPO_URL>
cd tapeout-harvester
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[keyring]'
cp config.example.toml config.toml
```

自己从当前官方来源核验合约地址和 miner keys。示例配置故意不是生产配置。

```bash
tapeout-harvester --config config.toml check
```

## 只领取不卖

```toml
[harvest]
sell_fraction = "0"
```

这是默认最小行为。

要卖出时，再配置 router / allowlist / quoter / pool / fee / destination。卖出只基于**本轮 claim receipt 的归因数量**，不会把离线期间别人转进钱包的 BEM 当成本轮收益。

## 真实执行

不要把私钥、助记词、keystore 密码写到配置或贴给 AI。使用本地加密 JSON keystore，并让密码进入工具允许的安全 OS keyring：

```bash
tapeout-harvester --config config.toml signer store-password
```

确认 dry-run 后：

```toml
[runtime]
live_enabled = true
```

```bash
tapeout-harvester --config config.toml run --live
```

缺任意一道开关都不会真实执行。

## receipt / 重试 / 确认数

- 广播前先把预期 tx hash 和 intent 持久化；
- receipt 不存在、区块不 canonical、确认数不足，都不会直接进入下一步；
- 未知结果绝不靠“再发一笔看看”解决；
- pending cycle 的执行身份和启动时配置不一致时，直接阻断恢复；
- state 使用临时文件 + replace，并对文件和父目录做 `fsync`。

## gas / allowance

- `max_gas_native_per_cycle` 是**整轮累计的最大 gas commitment**；
- 本轮如果需要创建 exact allowance，会提前把清理 revoke 的 gas 预算算进去；
- swap 构造失败或已知 revert 时，会尝试用预留预算清掉本轮授权；
- 如果 cleanup 本身失败/未知，系统保持 blocked，不继续交易；
- 如果 allowance 原本就存在且不是本轮创建的，工具不会把它冒充成自己的授权并擅自认领所有权。

## 并发边界

同一个 chain+wallet 的本工具实例会共享一个本地锁，即使 config/state 放在不同目录也会互斥。

但这**无法锁住其他钱包 APP、Bot 或别的交易程序**。因此每次签名前还会重新检查 latest/pending nonce；只要外部 writer 抢了 nonce，就 fail-closed。

同一 chain+wallet 仍应只使用**一个权威 `state_path`**；共享钱包锁不会自动合并两个不同 journal 的恢复状态。

## 已验证

当前本地 RC3 候选套件共 **100 个测试**：

```text
Python 3.11.4
web3 7.16.0
eth-account 0.14.0
keyring 可选能力由测试覆盖；本次验证环境已安装 keyring
100/100 PASS，0 skip
```

覆盖真实依赖 adapter、状态机、restart/unknown outcome、不重放、配置绑定、累计 gas、授权清理、strict bool/int、fsync、文件锁、secure keyring 与 security preflight。RC2 核心执行基线此前已完成独立 Codex blocker-focused 复审，结果为 **Blockers: None**。RC3 九轮 GPT-6 Astra / xhigh 复审累计发现 15 个 P2。第九轮补出 heartbeat 未绑定 watcher 启动配置的问题：配置变更后，旧 watcher 仍可能被新配置的 doctor 误判为健康。现在 heartbeat 已绑定配置 fingerprint。15 项均已修复并补回归测试，完整 100 项测试通过。最终 GPT-6 Astra / xhigh 只读 clean review 未发现可执行回归问题。

## 当前限制 / 操作边界

- cleanup gas 是有界预算，不是无限兜底；后续 gas 极端飙升时 revoke 可能挂起，需要人工撤权，系统会保持 blocked 而不是继续交易。
- 同一 chain+wallet 只使用**一个权威 state journal**；第二个 `state_path` 不会自动和第一个未完成 journal 对账。
- 卖出模式会先 reset 不匹配的旧 router allowance，本轮 cleanup 也可能 revoke 当前 allowance；不要让其他自动化同时共享这组 token/router allowance，除非你明确接受这种覆盖语义。
- state/runtime 目录应由当前用户独占，不要放进其他本地用户可写的共享目录；不可信目录仍可能制造临时状态文件的文件系统竞争。
- POSIX `fcntl` 锁；暂未做原生 Windows。
- 只实现 V3 单池单跳，不做聚合器和自动寻路。
- 不内置当前 TapeOut 合约地址。
- 本地共享锁无法约束外部钱包软件。
- 交易归因依赖标准 ERC-20 `Transfer` 和 wrapped-native `Withdrawal(address,uint256)` 事件语义。
- web3.py v8 尚未验证。
- **RC3 目前仍是本地未发布候选**；没有发布 GitHub Release，也没有用 rc3 做新的真钱执行。
- 已采用 MIT License；GitHub public push 已获 owner 授权，但仍需以远端 readback 作为发布完成证据。

## License

MIT — Copyright (c) 2026 `runesleo`。见 `LICENSE`。
