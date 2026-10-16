# 0001. macOS 应用版打包形态

状态：已接受

## 背景

CLI 版靠终端完成安装、更新与自启：更新链上要同时满足 git、uv、node、curl 四个外部工具。
本机两次故障都出在这条链上：一次是后端升了、配套前端没升（前后端劈叉导致缓存不一致弹窗），
一次是 launchd 会话默认 256 的文件描述符软上限被站点连接耗尽，accept 直接失败，
前端 `service.js` 因此健康检查连败退出，页面只剩 PWA 缓存外壳。

## 决策

一、壳层用 Swift 宿主 + WKWebView，前端继续用上游 Vue 产物，不重写界面。

二、去掉 Node。`public/service.js` 的职责（静态托管、`/api` 与 `/cookiecloud` 反代、SPA 回退、
后端健康看门狗）由 `pythonhost/mpweb` 用随包 Python 实现，省掉 159MB Node 运行时与 72 个 npm 依赖。

三、后端作为应用的子进程，不再由 launchd 直接托管。宿主在启动前把 RLIMIT_NOFILE 提到 10240；
子进程监听宿主 PID 变化，宿主被强杀时自行收尾，不留占端口的孤儿。

四、更新走整包（Sparkle），不在应用内做 git/uv 现场更新。前后端与配套前端本就是一个版本整体，
整包替换是唯一能保证两者不劈叉的方式。

五、数据目录沿用 `~/Library/Application Support/MoviePilot`，让 CLI 版用户原地升级。

六、上游源码零 patch：叠加内容放本仓库，构建时按 tag 组装，避免每次跟版本都要 rebase。

## 后果

应用体积从「venv 572MB + Node 159MB」降到「内嵌运行时 + 前端产物」，
更新面收敛为一个 .app；代价是必须处理签名与公证，以及内嵌解释器的首包体积。
文件描述符与代理继承这类问题从此由本仓库负责，不再依赖用户自己的 launchd 配置。
