# 第三方插件声明

本仓库的 `plugins/third/` 目录包含第三方 nonebot 插件。每个插件目录下都有
`UPSTREAM.md`（上游地址 / 作者 / 许可证 / 是否修改）。

## 清单

| 目录 | 上游 | 许可证 | 修改 | 状态 |
|---|---|---|---|---|
| `bittorrents` | https://github.com/NightDust981989/nonebot-plugin-BitTorrents | 见上游 | 否 | 
| `girls_2d` | https://github.com/zhuhiki/nonebot-plugin-2dgirls | 见目录 LICENSE | 否 | 
| `gspanel` | https://github.com/monsterxcn/nonebot-plugin-gspanel | 未声明 | 否 | 
| `jinriyunshi` | https://github.com/Doya16/NoneBot-QQbot-jinriyunshi | 未声明 | 否 | 
| `nonebot_plugin_rollpig_plus` | https://github.com/Felis2026/nonebot-plugin-rollpig-plus | 见上游 | 否 | 
| `petpet` | https://github.com/MeetWq/nonebot-plugin-petpet | MIT | 否 | 
| `pixivrank_search` | https://github.com/HibiKier/nonebot_plugin_pixivrank_search | 见上游 | 否 |
| `steam_judger` | https://github.com/Duogeyu/nonebot_steam_judger | 见上游 | 否 | 
| `VideoZoomer_master` | https://github.com/zsgvivo/VideoZoomer | 见目录 LICENSE / Notice.txt | 否 | 

> 许可证原文放在各插件目录的 `LICENSE` / `COPYING` / `Notice.txt`（如上游提供）。
> 未声明的插件在发行仓中标注风险，由发布者决定是否保留。

## 与本项目的关系

这些插件通过 `plugin.toml` 注册成 function calling skill（见 `docs/plugin-dev.md`），
由角色扮演模型按需调用；它们不是本项目原创代码，版权归各上游作者所有。
