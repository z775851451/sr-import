# sr-import：会话记录回灌 Scope Recall（可重复运行的自动化）

把 WorkBuddy 与 Zcode 的历史对话，作为**源（source）**灌进 Scope Recall 共享库，
由系统自己的管线决定它们能长出什么。工具只喂数据，不写真值、不碰 SQLite。

## 为什么这样设计

| 事实（源码实证） | 对工具的影响 |
|---|---|
| `DERIVATION_ROOT_ORIGINS = {human_direct, external_document, imported}`（`core/evidence_question.py:50`） | 只有这三种 origin 的源能当提炼根。**用户原话用 `human_direct`**，蒸馏结论用 `imported` |
| `role='tool'` 只拿 embedding，永不提炼（`core/admission.py:164-172`） | 工具只抽 user/assistant 消息，从不抽工具结果 |
| 事件 origin 必须等于 context 的 actor_origin（`contracts.py:688`） | 一个批次只能有一种 origin，工具按 origin 分组 |
| 只有 `actor_origin == "imported"` 才允许带 ImportProvenance（`contracts.py:666`） | `human_direct` 批次不带 provenance（真人原话自身即是凭据） |
| `ImportProvenance.source_fingerprints` 上限 200（`contracts.py:583`） | 每批 ≤200 条，每批一份 manifest |
| 超长文本由 `prepare_capture` 自动切段（`core/events.py:51-68`） | 不需要自己切段，毒丸问题由系统兜住 |
| `max_pending_work=2048` → 每类队列上限 1024（`core/admission.py:28`） | 限速灌入；队列满了新源拿不到提炼名额，反而变不成 claim |

**真值等级**：`human_direct` 是第一手，可佐证、可晋升为 `active`；
`imported`（蒸馏结论）能提炼，但不是第一手，产出的 claim 会停在 `proposed`（线索）。
所以本工具默认灌**用户原话**——那是唯一能长成真值的层。

## 命令

```bash
./run.sh plan                       # 只读抽取统计 + 样本，什么都不写
./run.sh check --limit 400          # 走真实校验器预检，预测拒收率
./run.sh register-entries           # 注册 workbuddy / zcode 两个 entry（幂等）
./run.sh run --limit 30             # 有界批次
./run.sh run                        # 增量：只灌台账里没有的
./run.sh forget-noise               # 列出已入库的噪音（dry）
./run.sh forget-noise --apply       # 用官方 forget 通道删除它们
./run.sh status                     # 台账与历史运行
```

## 清洗管线（QA 审查后加固）

两个宿主都会把宿主生成的提醒存成 `role="user"`，role 过滤挡不住，所以清洗做在内容层：

1. HTML 实体解码（`html.unescape`，先于一切匹配，否则 `&quot;` 会绕过）
2. 黑名单（命中即弃，窄匹配防误杀）：TodoWrite 提醒、`system reminder`、`Background command`、`chatcmpl-tool-`、裸 token（`cfat_`/`sk-`/`ghp_` 开头的整条串）
3. 内容去重：清洗后按 `sha256` 去重，同一文本只保留最早的 `occurred_at`
4. 存储层二次判重（`content_sha256` → `duplicate`）

## 噪音清理：官方 forget 通道（不直写 SQLite）

`./run.sh forget-noise --apply` 走 `core.deletion.forget`（`mode="delete"`）：

- **授权机制**：forget 要求同 session 里最新一条可读 `human_direct` 源构成"删除命令"
  （命中 `删除/清除` 等词、点名所有目标 ref、不含否定/条件/疑问句式）——工具每批先灌一条
  命令源再做 forget，closure 会把命令源一并清掉
- **为什么用 delete 不是 suppress**：`delete_storage.apply_blocks` 只在 delete 模式把目标的
  pending `work_items` 置为 `obsolete`——suppress 会留着它们继续占提炼名额
- 每批 ≤100 个 target（schema 硬限），幂等（`request_key` 去重），`memory_epoch` 自动 +1

常用开关：`--source workbuddy|zcode`、`--roles user`（或 `user,assistant`）、`--dry-run`。

## 可重复运行的三个保证

1. **幂等**：每条记录有稳定 key（`wb:<session>:<msg>` / `zc:<session>:<msg>`），
   灌前先查本地台账 `state/imported.sqlite3`，已灌的跳过；即使重复灌，
   存储层也会按 `content_sha256` 判 `duplicate`。
2. **增量**：新会话下次运行自动补上，不重灌旧数据。
3. **可观测**：每次运行写台账 + manifest（`state/manifests/<sha256>.json`），
   统计 inserted / duplicate / rejected 与拒收原因。

## 自动化

`com.srimport.daily.plist.template` 是 launchd 模板（每日 03:15，限速 300 条）。
也可以挂 cron：

```cron
15 3 * * * cd "<本目录>" && ./run.sh run --limit 300 >> state/logs/cron.log 2>&1
```

Windows 侧共享库是另一份（不同步 SQLite），需要在那台机器上独立跑一次。

## 回灌结果（2026-09-27 完成）

| 项 | 结果 |
|---|---|
| 入库干净源 | **553 条**（workbuddy 343 + zcode 210），100% `human_direct` + `user` |
| 时间跨度 | 2026-09-11 → 09-27（原始会话时间完整保留） |
| 清理噪音 | **337 条**经官方 forget 通道删除（288 条 Zcode TodoWrite 提醒、45 条 WorkBuddy 后台命令、4 条裸 token） |
| 队列水位 | consolidate 551 / embed 525 pending（上限 1024，安全） |
| 可见性 | 与 Hermes 5 bot + Codex 同一条 scope，**全员可召回** |
| 待观察 | consolidate 依赖 CF `@cf/qwen/qwen3.8-27b`，当时网关限流（429/timeout/provider_hold）导致提炼暂停；embed 正常。队列会自行消化 |

## 红线（工具已遵守，改代码时别破坏）

- 不直接写共享库 SQLite，只用 `core.capture.record_event` / `storage.register_entry`
- 不写 claims、不绕过 gating；真值只能由系统提炼
- 不绕过 secret 扫描器（`plaintext_secret_rejected` 的条目就让它拒）
- 不把 `<cb_summary>` 压缩块和系统注入当会话内容

## 目录

```
sr-import/
  run.sh                              入口
  sr_import/{config,extract,ledger,importer,cli}.py
  state/{imported.sqlite3,manifests/,logs/}    工具自己的状态，不在共享库里
  backups/                            灌前快照
```
