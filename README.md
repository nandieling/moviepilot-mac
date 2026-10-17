# MoviePilot macOS 应用版

把 CLI 版 MoviePilot 装成双击可用的 `MoviePilot.app`：拖进应用程序、打开、按向导配置，全程不碰终端。
本仓库只放应用化需要的壳层与打包脚本，**不改上游源码**，构建时把这里的内容叠加到上游 tag 上。

## 组成

| 目录 | 作用 |
|---|---|
| `pythonhost/mpweb` | 前端托管层：静态资源 + `/api`、`/cookiecloud` 反代 + 后端健康看门狗，替代上游 `public/service.js`，由此去掉 Node 运行时 |
| `host` | Swift 宿主：窗口（WKWebView）、标准主菜单（编辑菜单保证网页里能剪切粘贴）、菜单栏常驻、子进程守护、登录项 |
| `packaging` | 组包、签名、冒烟与生命周期检查 |
| `docs/adr` | 决策记录 |

后端进程是应用的子进程：应用退出即整体退出，不会再出现只剩静态页面的假死界面。

## 本地验证

```
swift build --package-path host                       # 编译宿主
python3 -m unittest discover -s tests -p "test_*.py"   # 托管层单测
python3 packaging/build_app.py --dev-root ~/MoviePilot # 借本地 CLI 版出开发包
packaging/lifecycle_check.sh ~/MoviePilot              # 界面可用 + 退出无孤儿
```

开发包用 `MOVPILOT_DEV_ROOT` 指向一份 CLI 版仓库即可运行，不需要完整打包：

```
MOVPILOT_DEV_ROOT=~/MoviePilot open dist/MoviePilot.app
```

CLI 版仓库不在（本机就已卸载）时，`lifecycle_check.sh` 自动改测 `dist/MoviePilot.app` 整包，
并额外断言包内插件目录已重定向到数据目录 —— 两种模式跑同一套就绪、冒烟、退出无孤儿检查。

## 发布组包

```
git clone --depth 1 --branch v3.1.6 https://github.com/jxxghp/MoviePilot .cache/upstream
python3 packaging/build_app.py --tag v3.1.6 --backend-repo .cache/upstream --dmg
```

取源码用的仓库由 `--backend-repo` 指定，自己留一份缓存即可，不依赖机器上有没有装 CLI 版。

四步各自独立、都有缓存，重跑一次约两分钟：

1. `git archive` 取该 tag 的干净源码树（丢掉 `public/`，前端另有来源）。
2. 按 `version.py` 的 `FRONTEND_VERSION` 取前端 `dist.zip`，用发布产物自带的 digest 校验；
   `version.txt` 与后端声明不一致直接失败 —— 前后端版本不成对正是「缓存版本不一致」弹窗的根源。
3. `build_runtime.py` 按 `runtime-manifest.json` 钉住的 python-build-standalone 独立解释器解包，
   用 `uv` 按上游 `uv.lock` 装依赖，再编译中文检索扩展（用户机器上不再需要 C 编译器）；
   `make_icon.py` 用前端产物里的 1024 母版做圆角遮罩，生成 `AppIcon.icns`。
4. `fetch_resources.py` 同步 `sites` 原生扩展与站点数据包，这两个文件不在后端 git 里。

装完用包内解释器跑一遍 `import app.main` 验收，预编译字节码，逐个签名内嵌二进制，
最后 `hdiutil` 出可拖拽 dmg。溯源信息落在 `Resources/build-info.json`。

arm64 产物：`MoviePilot.app` 732 MB，`MoviePilot-v3.1.6-arm64.dmg` 455 MB（内嵌运行时 605 MB、
前端 53 MB、后端 36 MB）。

## 包内布局

```
MoviePilot.app/Contents/Resources/
  runtime/             内嵌 CPython 与冻结依赖（发布模式产出）
  app/                 上游后端源码（按 tag 取）
  web/                 前端 dist（按 version.py 声明的配套版本取）
  pythonhost/          本仓库的托管层
  plugin-package-init.py  插件包标记的出厂副本，见下节
```

数据目录沿用 `~/Library/Application Support/MoviePilot`，CLI 版用户可直接继承已有 `user.db`、插件与 Cookie。

## 插件目录

上游把插件市场安装的插件写进「程序目录/app/plugins」，而应用版的程序目录在 .app 里面，
换一版软件就等于把用户自装的插件一起换掉。`PluginStore` 在起进程之前把这个路径换成指向
`<数据目录>/plugin_packages` 的软链：上游照旧往那个名字写，插件却活在整包升级之外，
插件字节码也不再写进包内。

- 首启把包内目录（含上一版留下的用户插件）搬进数据目录，再建软链；两边同名以数据目录为准。
- 换数据目录（含测试用的临时目录）时重新接链，软链断掉也能自愈。
- 数据目录被清空时用 `plugin-package-init.py` 补回 `app/plugins/__init__.py`，
  `import app.plugins` 不会因此断掉；缺的插件下次启动按数据库里的安装记录自动从市场拉回。

## 与 CLI 版共存

应用启动时先探端口（connect 探活，不看 TIME_WAIT）：

- 3000/3001 空闲：自己拉起一套服务。
- 3000 上已经是 MoviePilot：直接接入这个实例，不再起第二个后端，界面标题会写明。
- 端口被别的程序占用：整对端口顺延 +1 直到最多 +20，并记住这次的选择。

环境变量：`MOVPILOT_FRONTEND_PORT`、`MOVPILOT_BACKEND_PORT` 指定首选端口；`MOVPILOT_PORT_PERSIST=0`
关闭端口记忆（测试脚本用，保证每次都用指定端口）。

## 缓存策略

托管层按「状态类资源一律回源」处理：同源 API 上游没写 `Cache-Control` 就补 `no-store`，
`index.html`、`service-worker.js`、`version.txt`、`*.webmanifest` 一律 `no-cache`，
只有带内容哈希的 `assets/` 走一年不可变缓存。

这不是洁癖：WebView 把 `login/initialization` 缓存住之后，数据目录刚清空也会被判成
「已初始化」，首次设置页直接跳过；`version.txt` 被缓存则是「缓存版本不一致」弹窗的老病根。
宿主还记录界面载入时刻，后端本轮启动时刻晚于它就重新载入，页面不会停在旧实例的状态上。

仅靠响应头还不够。上游前端自带 workbox Service Worker，它用 `CacheStorage.put` 把同源
GET 响应直接写盘，而 `Cache-Control: no-store` 拦不住这个写入 —— 注册完超级用户再启动，
读到的还是上一次的 `initialized: false`，于是又回到首次设置页。所以 mpweb 用
`mpweb/sw-stub.js` 顶掉上游的 `service-worker.js`：照常注册，激活时清空历史缓存，之后
不再缓存任何东西。设 `MOVPILOT_SERVICE_WORKER=upstream` 可退回上游原版。

旧缓存是注册前那一次留下的，清一次就干净：菜单 显示 ▸ 清空界面缓存并重载（⌥⌘R）。

## 已知缺口

- 签名目前只有 ad-hoc：换 Developer ID 证书 + 公证只需给 `build_app.py --identity`，
  但内嵌 572 个二进制的逐层签名要在真证书下再验一遍。
- Sparkle 更新通道未接，appcast 地址是占位值。
- 首屏之后的首次设置页直接用上游前端自带的 `/initialize`，无需自建向导。
- 内嵌运行时占了 605 MB，其中 langchain/boto3 等可选 SDK 可以按插件需要再裁一轮。
- `build_runtime.py` 每次组包都重新解包解释器，CI 上可加 `--reuse-runtime` 跳过。
