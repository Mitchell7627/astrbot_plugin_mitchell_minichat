<div align="center">

# 主动私聊 Mini

### 让 他/她 想起你，也主动找你聊一句。

加权主动聊天 · 事件感知 · 跨日约定 · 睡眠免打扰

<p>
  <a href="https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat/releases/latest"><img src="https://img.shields.io/github/v/release/Mitchell7627/astrbot_plugin_mitchell_minichat?style=flat-square&amp;label=version&amp;color=8b7cf6" alt="最新正式版本"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-69c9a5?style=flat-square" alt="MIT license"></a>
  <a href="https://github.com/AstrBotDevs/AstrBot"><img src="https://img.shields.io/badge/AstrBot-%3E%3D4.29.0-5ca9ef?style=flat-square" alt="AstrBot >= 4.29.0"></a>
</p>

<p>
  <a href="#功能一览">功能</a> ·
  <a href="#安装与上手">安装</a> ·
  <a href="#token-消耗与后续计划">Token 消耗</a> ·
  <a href="docs/configuration.md">配置说明</a> ·
  <a href="https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat/releases">下载更新</a> ·
  <a href="#错误反馈与建议">反馈与建议</a>
</p>

<img src="docs/assets/minichat-banner.svg?v=4" width="960" alt="Mini 主动陪伴：知道你在忙，也记得什么时候找你">

<a href="https://count.getloli.com/@mitchell7627-minichat">
  <img src="https://count.getloli.com/@mitchell7627-minichat?theme=moebooru&amp;padding=6&amp;offset=0&amp;align=bottom&amp;scale=0.85&amp;pixelated=1&amp;darkmode=auto" width="680" alt="猫娘举牌访问计数">
</a>

</div>

---

Mini 是 AstrBot 的 QQ 私聊主动消息插件。它根据聊天间隔、当前活动、在场状态和约定时间安排下一次开口，再结合你配置的人格与上下文生成消息。

**当前仅支持 aiocqhttp（QQ）私聊**，只服务 `session_list` 中指定的会话。群聊不生效，其他适配器尚未验证。首次使用建议选「默认」频率，先看生成预览，再调整语气。

<details>
<summary><strong>免责声明与用户协议</strong></summary>

使用本插件前，请阅读并理解以下说明：

1. 本项目用于学习、研究与合法的个人聊天陪伴。请遵守当地法律法规和平台服务条款，不得用于骚扰、滥发消息或侵犯他人隐私。
2. 监听、保存和处理聊天消息前，使用者应取得必要授权；启用模型功能时，相关消息与上下文可能发送至你配置的模型服务，请自行确认其数据处理方式。
3. 事件识别、时间理解与模型生成可能出错，提醒也可能受网络、模型服务或平台连接影响。重要事项请同时使用独立提醒工具。
4. 本项目按“原样”提供，不承诺持续可用或完全无误；相关担保与责任限制以 MIT License 及适用法律为准。
5. 使用者应对自己的部署、配置与使用行为负责。本说明可能随版本更新，代码的使用、修改与分发以仓库中的 MIT License 为准。

</details>

## 功能一览

| 功能 | 能做什么 |
| --- | --- |
| 主动聊天 | 用加权概率曲线选择下一次开口时间；四档预设与自定义权重，连续未回复达到上限后暂停 |
| 事件感知 | 默认配置提供 16 类活动；关键词开始 / 结束、持续时长、事件加成及预算均可配置，未明确命中时可用模型分类与灰区复核 |
| 在场状态 | 区分离开、刚回来、明确「在一起」，给主动消息提供相应的场景提示 |
| 约定与跨日预约 | 识别下课、下班、到家、开会等时间点；支持范围内的明确跨日日期、匹配取消和明确改期 |
| 起床 / 叫醒预约 | 识别「明早八点叫醒我」等本人明确请求；到点执行，不提前叫醒，不重复补叫 |
| 睡眠与提醒 | 晚睡、午睡提醒，「等会再睡」的单次提醒；明确入睡后免打扰，唤醒词或起床时间解除 |
| 上下文控制 | 选择会话历史、平台流水或两者；可限制条数 / 时间窗口、过滤历史图片与思考块 |
| 智能分段 | 把长消息拆成短气泡，保护引号、括号和代码块，并设置段间延迟 |
| Web 面板 | 任务、概率、配置三页；生成预览、立即发送、状态调整、最近判定及可用记录的撤销 |
| 排程诊断 | 查看采样、事件调整、睡眠顺延、约定优先、未回复暂停等原因，便于定位「为什么没发」 |

### 试着这样说

| 你说的话 | 插件会尝试做什么 |
| --- | --- |
| 「我去洗澡了」 / 「洗完了」 | 开始 / 结束洗澡事件，调整后续主动消息的场景 |
| 「出门了」 / 「到家了」 | 更新离开 / 回来状态；具体结果也受同句活动和保护规则影响 |
| 「我们牵着手呢」 | 标记明确「在一起」，在提示词中减少离别式措辞 |
| 「明天下午三点开会」 | 登记未来约定；实际是否登记以面板判定和配额为准 |
| 「明天下午三点的会议取消了」 | 按日期、钟点和事项匹配取消 |
| 「明早八点叫醒我」 | 登记本人叫醒预约；发送失败时保留既有睡眠状态 |
| 「等会再睡」 / 「晚安」 / 「我醒了」 | 分别尝试建立待睡提醒、进入免打扰、提前解除免打扰 |

第三方、引用、疑问、否定、条件句以及备选时间会受到保护，避免把「我妈去洗澡了」「明天三点或四点开会」直接记成本人确定活动或约定。自然语言仍可能存在歧义，识别结果与生成措辞也会受模型影响；发现误判欢迎提交可复现例子。

默认活动的时长、加成及自定义方式见 [活动与在场配置](docs/configuration.md#活动与在场)。已有安装以自己的实际规则为准。

## 安装与上手

需要 **AstrBot ≥ 4.29.0**、可用的 aiocqhttp 私聊连接和已配置的模型服务。

1. 在 AstrBot 插件管理中从仓库安装：

   ```text
   https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat
   ```

   也可从 [Releases](https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat/releases) 下载 ZIP，将插件目录放入 `data/plugins/`。插件市场的收录与版本更新可能晚于 GitHub。

2. 启用插件，在 QQ 私聊发送 `/sid` 获取会话 ID，将它填入插件配置的「监听的会话」：

   ```text
   机器人名:FriendMessage:QQ号
   ```

3. 保存配置，先使用「默认」聊天频率。默认启动后约 1 分钟会尝试主动开口，睡眠、暂停和其他排程条件仍会影响实际执行。
4. 打开 AstrBot 插件页中的 Mini 面板，或本机访问 `http://127.0.0.1:7627`。
5. 点击「生成预览」试语气；它会调用模型并消耗 token，**不发送 QQ 消息**。「立即发送」会实际发送。
6. 试说「我去洗澡了」，在任务页查看当前事件与最近判定；之后再调整提示词、频率和提醒开关。

服务器部署时，`127.0.0.1` 指服务器自身；远程查看建议使用 SSH 隧道或带 HTTPS 与鉴权的反向代理。面板默认只监听本机，扩大监听范围前需设置密码。

## 频率、约定和睡眠规则

频率是加权采样，事件、约定、睡眠和正常回复避让会调整实际排程；达到未回复上限后，常规主动消息会暂停。提醒能否登记、执行及撤销，应以任务页的状态和最近判定为准。

具体默认值集中在 [配置说明](docs/configuration.md)，避免首页和配置页分别维护同一张参数表：

- [聊天节奏](docs/configuration.md#聊天节奏)：四档预设、采样权重和未回复暂停。
- [约定](docs/configuration.md#约定)：跨日范围、时区、容量、取消 / 改期、叫醒与失败保护。
- [睡眠](docs/configuration.md#睡眠)：夜间 / 午睡窗口、待睡提醒和免打扰。
- [模型与上下文](docs/configuration.md#控制模型调用和上下文)、[分段与面板](docs/configuration.md#分段与面板)。

## Token 消耗与后续计划

**插件会额外调用模型，不能保证每次消耗固定，也不能按聊天条数直接推算费用。** 常规聊天回复由 AstrBot 自己管理；下表说明 Mini 自己产生的开销。

| 操作 | 是否额外调用模型 |
| --- | --- |
| 随机采样、计时、明确关键词命中、明确约定登记 / 取消、读取面板状态 | 这些规则处理本身不调用模型；同条消息若还有其他语义分支，仍可能调用 |
| 未命中规则的事件语义分类 | 满足预筛和开关条件时调用，通常为一次分类请求 |
| 事件灰区 / 约定灰区复核 | 各自开关和额度允许时调用；与普通分类不是同一个额度 |
| 主动聊天、催睡、约定提醒、生成预览 | 实际生成内容时调用；历史越长，输入通常越多 |
| 仅把生成的内容拆成多个气泡 | 分段本身不重复生成，不因拆成多段额外调用模型 |

### 已有计量：供理解量级

2026-11-03 22:08（UTC+8）读取的**私有低消耗测试版**累计匿名计量如下。它是特定模型、提示词与历史配置下的小样本，**不是公版基准、每次固定预算或双版本节省率**。

| 请求用途 | 完成次数 | 非缓存输入 token | 缓存输入 token | 输出 token | 平均总 token / 次 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 事件语义分类 | 28 | 11,888 | 22,272 | 4,877 | ≈ 1,394 |
| 事件灰区复核 | 1 | 235 | 0 | 268 | 503 |
| 常规主动生成 | 3 | 5,616 | 7,296 | 252 | 4,388 |

总 token 按「非缓存输入 + 缓存输入 + 输出」计算；缓存输入仍是 token，计费价格可能不同。未列出的请求类型没有足够样本，不能据此当作零消耗。实际费用需按自己的服务商 usage 和价格计算，**字符数不等于 token 数**。

**公版现在可用的控制**：降低主动频率、设置连续未回复上限、合理裁剪主动消息的历史窗口；历史图片和历史思考块默认不带，历史硬上限默认 120 条。语义识别可单独指定模型；关闭某一类识别会降低对应识别覆盖，详见 [配置说明](docs/configuration.md#控制模型调用和上下文)。

### 低消耗 Token 版将在后续发布

当前 GitHub 公版已有完整确认短句的过滤与意图保护；私有测试版新增的**分用途请求计量和最多 500ms 的背景判断合并**将在后续发布。发布前会继续验证识别准确性与跨功能回归，进度以 [Releases](https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat/releases) 为准，暂不承诺日期或固定节省比例。

已有替身对照中，两条间隔 50ms 的背景消息可从 2 次请求合并为 1 次；间隔 700ms 时仍为 2 次。节省取决于连续消息和确认句的占比，不能把单个场景的调用减半当作总体 token 减半。

## 常见问题

<details>
<summary>没有主动说话，先看哪里？</summary>

确认插件启用、`session_list` 正确、加权主动消息开启、模型服务可用。再看任务页的下次排程、睡眠状态、`paused` 和「排程原因」，最后查看 `[Minichat]` 日志。达到未回复上限后会暂停；普通约定在睡眠中也可能顺延。

</details>

<details>
<summary>说了约定，为什么没有登记或提醒？</summary>

先看「最近判定」确认是否登记成功，再核对宿主时区、日期范围、每日额度和 3 条队列容量。否定、第三方、引用、疑问和备选时间不会直接登记。已经登记但未发送时，继续检查睡眠、改期 / 取消、合并阶段和模型 / 发送失败日志；叫醒未送达不会按成功处理。

</details>

<details>
<summary>为什么识别不准，或者语气与预期不同？</summary>

规则识别和模型生成是两层：判定正确，也不代表模型一定遵守每句场景提示。可先用「生成预览」检查上下文和语气，核对实际事件规则与语义模型。连续短消息、含糊指代和复杂改口仍需要具体语料验证，欢迎到 Issues 提供脱敏复现。

</details>

<details>
<summary>配置保存后没变化？</summary>

多数参数保存即生效；事件规则和面板监听地址 / 端口等需要重载。面板提示词覆盖保存在 `data/plugin_data/astrbot_plugin_mitchell_minichat/web_overrides.json`，应与 AstrBot 原生配置一起检查。界面旧缓存可尝试强制刷新。

</details>

## 错误反馈与建议

**错误、识别问题、功能建议请统一提交到本仓库的 GitHub Issues**，便于复现、追踪和在版本更新中注明处理结果。提交前可先搜索已有问题。

<p>
  <a href="https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat/issues/new?template=bug_report.yml"><img src="https://img.shields.io/badge/%E6%8A%A5%E5%91%8A%E9%94%99%E8%AF%AF-GitHub%20Issues-f08090?style=for-the-badge" alt="报告错误"></a>
  <a href="https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat/issues/new?template=feature_request.yml"><img src="https://img.shields.io/badge/%E5%8A%9F%E8%83%BD%E5%BB%BA%E8%AE%AE-GitHub%20Issues-8b7cf6?style=for-the-badge" alt="功能建议"></a>
</p>

错误反馈请带上插件 / AstrBot 版本、适配器、宿主时区、复现原句与顺序、预期结果、实际结果，以及相关判定或日志。**请先删除 API Key、密码、QQ 号、会话 ID 和私人聊天内容**；不必上传完整生产配置或整份日志。

建议可以直接说明「什么场景下，希望它怎么做」，如果有担心的副作用也一起写。公开追踪与处理进度以 Issues 为准。

## 开发与更新

- [更新日志](CHANGELOG.md) · [正式版本与 ZIP / SHA-256](https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat/releases)
- [开发验证说明](docs/validation.md)：仓库回归、历史回归、真实浏览器面板检查和离线回放。
- 模型 / 发送替身用于验证流程，不能证明任意自然语言的识别质量或真实 QQ 投递；以固定提交的 [CI](https://github.com/Mitchell7627/astrbot_plugin_mitchell_minichat/actions) 记录为准。

## SHA-256 校验文件的用途

Release 中的 `.zip.sha256` 是对应 ZIP 安装包的校验文件，记录该文件的 SHA-256 摘要。它用于检查下载是否完整、内容是否与发布包一致，**不需要安装，也不需要放进插件目录**。

下载 ZIP 和同名 `.zip.sha256` 后，用文本编辑器打开校验文件，再计算 ZIP 的摘要进行比较。例如在 Windows PowerShell 中：

```powershell
Get-FileHash .\astrbot_plugin_mitchell_minichat_v0.3.3.zip -Algorithm SHA256
```

将输出的 `Hash` 与校验文件中的摘要比较（大小写不影响结果）；一致表示校验通过，不一致则应重新下载并核对版本。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。
