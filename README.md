# MoviePilot macOS 应用版

把 CLI 版 MoviePilot 装成双击可用的 `MoviePilot.app`：拖进应用程序、打开、按向导配置，全程不碰终端。
本仓库只放应用化需要的壳层与打包脚本，**不改上游源码**，构建时把这里的内容叠加到上游 tag 上。

## 组成

| 目录 | 作用 |
|---|---|
| `pythonhost/mpweb` | 前端托管层：静态资源 + `/api`、`/cookiecloud` 反代 + 后端健康看门狗，替代上游 `public/service.js`，由此去掉 Node 运行时 |
| `host` | Swift 宿主：窗口（WKWebView）、菜单栏、子进程守护、登录项 |
| `packaging` | 组包、签名、冒烟与生命周期检查 |
| `docs/adr` | 决策记录 |

后端进程是应用的子进程：应用退出即整体退出，不会再出现只剩静态页面的假死界面。

## 本地验证

```
swift build --package-path host                      # 编译宿主
python3 -m unittest discover -s tests -p "test_*.py"  # 托管层单测
python3 packaging/build_app.py --dev-root ~/MoviePilot  # 用本地 CLI 版做运行时出开发包
packaging/lifecycle_check.sh ~/MoviePilot             # 界面可用 + 退出无孤儿
```

开发包用 `MOVPILOT_DEV_ROOT` 指向一份 CLI 版仓库即可运行，不需要完整打包：

```
MOVPILOT_DEV_ROOT=~/MoviePilot open dist/MoviePilot.app
```

## 包内布局

```
MoviePilot.app/Contents/Resources/
  runtime/     内嵌 CPython 与冻结依赖（发布模式产出）
  app/         上游后端源码（按 tag 取）
  web/         前端 dist（按 version.py 声明的配套版本取）
  pythonhost/  本仓库的托管层
```

数据目录沿用 `~/Library/Application Support/MoviePilot`，CLI 版用户可直接继承已有 `user.db`、插件与 Cookie。

## 与 CLI 版共存

应用启动时先探端口（connect 探活，不看 TIME_WAIT）：

- 3000/3001 空闲：自己拉起一套服务。
- 3000 上已经是 MoviePilot：直接接入这个实例，不再起第二个后端，界面标题会写明。
- 端口被别的程序占用：整对端口顺延 +1 直到最多 +20，并记住这次的选择。

环境变量：`MOVPILOT_FRONTEND_PORT`、`MOVPILOT_BACKEND_PORT` 指定首选端口；`MOVPILOT_PORT_PERSIST=0`
关闭端口记忆（测试脚本用，保证每次都用指定端口）。

## 已知缺口

- 发布模式待接入 python-build-standalone 运行时下载与依赖冻结（M2）。
- 签名走 Developer ID + 公证，Sparkle 更新通道待接；当前只做 ad-hoc 签名。
- 首启向导复用上游 `init` 内部命令，界面部分待做（M1）。
