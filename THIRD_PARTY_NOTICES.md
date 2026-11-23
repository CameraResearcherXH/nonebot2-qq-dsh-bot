# 第三方插件声明

本仓库有两类第三方代码：

1. `plugins/third/` —— 第三方 nonebot 插件，每个目录下有 `UPSTREAM.md`
   （上游地址 / 作者 / 许可证 / 是否修改）；
2. `vendor/` —— 被 `services/` 直接依赖的第三方库（目前只有 `PicImageSearch`）。
   放在仓库根而不是 `plugins/third`，是因为依赖方向是 `plugins → services → core`，
   `services` 不允许 import `plugins`；`vendor/` 是叶子依赖，两边都能用。

发布前必须核对：

- [ ] 上游地址是否准确
- [ ] 许可证是否允许再分发（GPL/AGPL 需评估隔离或移除）
- [ ] 是否修改过源码；修改说明是否写清
- [ ] 目录内 `LICENSE` / `COPYING` 是否保留

## 清单：`plugins/third/`

| 目录 | 上游 | 许可证 | 修改 | 状态 |
|---|---|---|---|---|
| `bittorrents` | https://github.com/NightDust981989/nonebot-plugin-BitTorrents | 见上游 | 否 | 
| `girls_2d` | https://github.com/zhuhiki/nonebot-plugin-2dgirls | 见目录 LICENSE | 否 | 
| `gspanel` | https://github.com/monsterxcn/nonebot-plugin-gspanel | 见上游  | 否 | 
| `jinriyunshi` | https://github.com/Doya16/NoneBot-QQbot-jinriyunshi | 见上游 | 否 | 
| `nonebot_plugin_rollpig_plus` | https://github.com/Felis2026/nonebot-plugin-rollpig-plus | 见上游 | 否 | 
| `petpet` | https://github.com/MeetWq/nonebot-plugin-petpet | MIT | 否 | ✅ 已确认 |
| `picsearch` | 本项目自建（薄封装，调用 `vendor/PicImageSearch`） | 与本项目一致 | — | ✅ 自建 |
| `pixivrank_search` | https://github.com/HibiKier/nonebot_plugin_pixivrank_search | 见上游 | 否 | 
| `steam_judger` | https://github.com/Duogeyu/nonebot_steam_judger | 见上游 | 否 |
| `VideoZoomer_master` | https://github.com/zsgvivo/VideoZoomer | 见目录 LICENSE / Notice.txt | 否 | 

## 清单：`vendor/`

| 目录 | 上游 | 许可证 | 修改 | 用途 |
|---|---|---|---|---|
| `vendor/PicImageSearch` | https://github.com/kitUIN/PicImageSearch | MIT（见目录 LICENSE） | 否（原样引入） | 以图搜源聚合：TraceMoe / SauceNAO / Ascii2D / IQDB |

> 许可证原文放在各目录的 `LICENSE` / `COPYING` / `Notice.txt`（如上游提供）。
> 未声明的插件在发行仓中标注风险，由发布者决定是否保留。

## 与本项目的关系

`plugins/third/` 里的插件通过 `plugin.toml` 注册成 function calling skill
（见 `docs/plugin-dev.md`），由角色扮演模型按需调用；
`vendor/` 里的库由 `services/` 直接调用（`services/image_search.py`）。
它们不是本项目原创代码，版权归各上游作者所有。
