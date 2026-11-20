# 第三方插件声明

本仓库的 `plugins/third/` 目录包含第三方 nonebot 插件。每个插件目录下都有
`UPSTREAM.md`（上游地址 / 作者 / 许可证 / 是否修改）。

发布前必须核对：

- [ ] 上游地址是否准确
- [ ] 许可证是否允许再分发（GPL/AGPL 需评估隔离或移除）
- [ ] 是否修改过源码；修改说明是否写清
- [ ] 目录内 `LICENSE` / `COPYING` 是否保留

## 清单

| 目录 | 上游 | 许可证 | 修改 | 状态 |
|---|---|---|---|---|
| `bittorrents` | https://github.com/NightDust981989/nonebot-plugin-BitTorrents | 见上游 | 否 | 上游已确认，许可证待核对 |
| `girls_2d` | https://github.com/zhuhiki/nonebot-plugin-2dgirls | 见目录 LICENSE | 否 | 上游已确认 |
| `gspanel` | 待人工确认 | 未声明 | 否 | ⚠️ 待核对 |
| `jinriyunshi` | 待人工确认 | 未声明 | 否 | ⚠️ 待核对 |
| `nonebot_plugin_rollpig_plus` | https://github.com/Felis2026/nonebot-plugin-rollpig-plus | 见上游 | 否 | 上游已确认，许可证待核对 |
| `petpet` | https://github.com/MeetWq/nonebot-plugin-petpet | MIT | 否 | ✅ 已确认 |
| `pixivrank_search` | https://github.com/HibiKier/nonebot_plugin_pixivrank_search | 见上游 | 否 | 上游已确认，许可证待核对 |
| `steam_judger` | https://github.com/Duogeyu/nonebot_steam_judger | 见上游 | 否 | 上游已确认，许可证待核对 |
| `VideoZoomer_master` | 待人工确认（volce） | 见目录 LICENSE / Notice.txt | 否 | ⚠️ 待核对 |

> 许可证原文放在各插件目录的 `LICENSE` / `COPYING` / `Notice.txt`（如上游提供）。
> 未声明的插件在发行仓中标注风险，由发布者决定是否保留。

## 与本项目的关系

这些插件通过 `plugin.toml` 注册成 function calling skill（见 `docs/plugin-dev.md`），
由角色扮演模型按需调用；它们不是本项目原创代码，版权归各上游作者所有。
