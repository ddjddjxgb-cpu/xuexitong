# 失败信号速查表（failure-signals）

> 排障入口：按你在 **GitHub Actions 日志 / 章节目志 / result.json** 里看到的原始信号串检索。
> 每条均为本仓库实测出现过的信号（来源：2026-10 真站运行日志），标注了【系统自动行为】与【何时需要人工】。
> 原则：系统能自愈的不打扰人；需要人的地方一定有明确动作。

## 一、正常自愈类（看到 = 系统在工作，无需处理）

| 日志信号 | 含义 | 系统自动行为 |
|---|---|---|
| `[sanitize] inject: 注入=N` | Secrets `COURSE_URL` 的 enc/openc 已注入工作区 state | 无需处理；缺失时行不出现 |
| `[tdvp] probe-scan cid: 翻到第 N/8 页…` | 探测逐页扫描（有界：≤8 页、连续 2 空页早停） | 正常 |
| `[prepare] 深读第 N 次 cid (槽位 X/3)` | 窗口化点级深读进度 | 正常 |
| `→ 回填槽位` / `无未完成视频工作` | 该章视频已尽或无视频，不占窗口槽、跨轮记忆跳过 | 正常 |
| `★ R-04 auto-resume #N` | 视频 paused=True → 自动 play() 续播 | 正常（次数上限 MAX_RESUME_ATTEMPTS）|
| `★ A5 stalled-resume #N` | 视频 paused=False 但 ct 静止 ≥25s（缓冲卡顿）→ play() 恢复 | 正常（上限 3 次；耗尽仍静止则见 HEARTBEAT_DEAD）|
| `watchdog extended: … -> …s` | 看门狗按视频实际时长自适应扩预算 | 正常 |
| `REFINE-PRUNED …` | live 读数发现账本里的点页面不存在（历史幻影），投递前剪除 | 正常自愈 |
| `PHANTOM-CORRECTED …` | 引擎实测页面上没有该点，账本/快照已纠正，不计为播放失败 | 正常自愈 |
| `healed_by_server` / `A2 crash-truth heal` | 崩溃后点级复核发现服务端已判 finished → SERVER_VERIFIED 落账 | 正常自愈（预算 2 次/进程）|

## 二、需关注类（单次出现可忽略，反复出现要查）

| 日志信号 | 含义 | 系统自动行为 | 人工动作 |
|---|---|---|---|
| `⚠️ Heartbeat dead at ct=…` | 视频 paused=False 但 ct 静止 60s（A5 恢复耗尽后）| 该章判失败退出，**下轮自动重试** | 反复出现在同一章 → 检查该视频是否需特殊清晰度/网络 |
| `⚠️ A5/R-04 … 未生效` | 恢复动作没让视频动起来 | 继续按冷却重试或最终判死 | 同上 |
| `verdict=DEGRADED` | 不确定结果（部分证据缺失但服务端未否认）| 不计成功也不计入连续失败 | 下轮自动重试 |
| `catalog tree selector not found` | 课程页目录树 30s 未出现 | 重试一次后 PROBE_EMPTY 跳过本轮 | **反复出现 = 九成是 enc 失效**，见三-1 |
| `⚠️ No result file produced` | 子进程没产出 evidence | 汇总判失败 | 看 Run Scheduler 步骤更早的报错 |

## 三、需要人工类（系统已如实上报，等恢复/等配置）

| 日志信号 | 含义 | 系统自动行为 | 人工动作 |
|---|---|---|---|
| 页面 body 出现 `enc校验失败` | 课程 URL 的 enc 签名不被服务端接受（失效/被剥） | 探测空 → 本轮 NOOP | 更新 Secret `COURSE_URL`：浏览器进课程页复制地址栏 URL（含 enc），Settings → Secrets → COURSE_URL 覆盖保存 |
| `LOGIN_FAILED` / 登录失败 | 账密不对或触发验证 | 本轮终止（exit） | 核对 Secrets `CX_USER`/`CX_PASS`；若触发验证码，隔天再跑 |
| `SESSION_KICKED` | 会话被服务端踢出 | 环境类失败，本轮终止不计章节失败 | 通常等冷却自愈；频繁出现说明账号在别处登录 |
| `CRASH: TargetClosedError` | 浏览器窗口被外部关闭（runner 回收/资源不足） | 先 A2 点级真源复核（学完的直接落账），否则按环境类失败终止 | 偶发无需处理；同轮多次出现查 runner 资源 |
| `PROBE_EMPTY — 目录探测空` | 目录树两次提取均为空 | 不臆测选章，本轮跳过 | 先看是否伴随 enc 失败/登录失败；都排除则可能是站点改版 |
| `decision=NOOP No state for active course` | courses 状态文件缺失（云端冷启动/误删）| 本轮空转退出 | 手动触发一次 `action=bootstrap`（需 COURSE_URL）重建状态 |
| `BLOCKED` / `consecutive failures ≥ 3` | 同章连续失败达熔断阈值 | 该章冻结，schedule 触发按冷却间隔尝试解除 | 看该章失败原因；人工确认无问题后可手动触发 manual 强制重试 |
| `STALL/QUEUED (concurrency)` | 前一轮未结束，本轮排队 | 只延后不取消 | 无需处理 |

## 四、判定词汇对照

| 词 | 含义 |
|---|---|
| `SERVER_CONFIRMED_PASS` | 服务端 isPassed=true —— 唯一可信完成判据 |
| `SERVER_VERIFIED` | 账本证据等级：服务端点级确认（强证据，不被章级读数推翻）|
| `RECHECK` | 播放后复查确认（强证据）|
| `UI` | 目录 DOM"已完成"标记（弱证据，7 天体检 TTL）|
| `REFINE` | 投递前对目标章的 live 点级复核（E6.2）|
| `PRUNE` | 按 live 读数剪除账本中页面不存在的幻影点 |
| `NOOP` | 本轮无事可做（探测空/无待办/状态缺失）——不是错误 |
| `watchdog` | 看门狗：子进程超时收割器（按视频时长自适应）|

## 五、最快的定位路径

1. `result.json` 的 `decision` / `verdict` / `failure_stage` 三字段 → 定性
2. 失败 → `runtime_result` 与 `failure_stage` → 定层（登录/页面/视频/网络）
3. 回到本表按信号串检索 → 按人工动作列执行
4. 仍无法定位 → 把 **Run Scheduler 步骤日志最后 40 行** 发给维护者
