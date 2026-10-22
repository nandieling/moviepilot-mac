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
# 缓存只需要一次，之后每个新版本只补一个 tag
git clone --depth 1 --branch v3.1.6 https://github.com/jxxghp/MoviePilot .cache/upstream
git -C .cache/upstream fetch --depth 1 origin tag v3.1.7
python3 packaging/build_app.py --tag v3.1.7 --backend-repo .cache/upstream --dmg --delta
```

前后端版本都取自上游 `version.py`，壳层不另存一份版本号：`--tag v3.1.7` 会让
`Info.plist` 的 `CFBundleShortVersionString`、`build-info.json` 和产物文件名一起变成 3.1.7。
```

取源码用的仓库由 `--backend-repo` 指定，自己留一份缓存即可，不依赖机器上有没有装 CLI 版。

四步各自独立、都有缓存，重跑一次约两分钟：

1. `git archive` 取该 tag 的干净源码树（丢掉 `public/`，前端另有来源）。
2. 按 `version.py` 的 `FRONTEND_VERSION` 取前端 `dist.zip`，用发布产物自带的 digest 校验；
   `version.txt` 与后端声明不一致直接失败 —— 前后端版本不成对正是「缓存版本不一致」弹窗的根源。
3. `build_runtime.py` 按 `runtime-manifest.json` 钉住的 python-build-standalone 独立解释器解包，
   用 `uv` 按上游 `uv.lock` 装依赖，再编译中文检索扩展（用户机器上不再需要 C 编译器）；
   `make_icon.py` 用前端产物里的 1024 母版做圆角遮罩，现场生成 `AppIcon.icns`（生成物，仓库不存）。
4. `fetch_resources.py` 同步 `sites` 原生扩展与站点数据包，这两个文件不在后端 git 里。

装完用包内解释器跑一遍 `import app.main` 验收，预编译字节码，逐个签名内嵌二进制，
最后 `hdiutil` 出可拖拽 dmg。溯源信息落在 `Resources/build-info.json`。

仓库只存源码与脚本：`dist/`、`.cache/`、`host/.build/` 这三块产物（合计约 1.65 GB）以及
`__pycache__`、`.DS_Store` 全在 `.gitignore` 里；数据目录万一被 `CONFIG_DIR` 指进仓库，
`app.env`（含 API_TOKEN、GITHUB_TOKEN、CookieCloud 口令）、`user.db`、`plugin_packages/`
也有兜底规则挡着。备份脚本默认写到 `~/Documents`，别改到仓库里。

arm64 产物：`MoviePilot.app` 737 MB，`MoviePilot-v3.1.7-arm64.dmg` 453 MB（内嵌运行时 605 MB、
前端 53 MB、后端 36 MB）。

### 自动组包

`.github/workflows/macos-app.yml` 每天北京时间 09:20 看一眼上游发没发新版，和本仓库最新发布
比对：没有新版就地收工，有就在 `macos-15` runner 上跑同一套流程 —— 托管层单测、组包
（dmg + 增分包）、`lifecycle_check.sh` 整包自检，然后把 dmg、增分包、校验值发成一个
GitHub Release，Actions 构建页另留一份产物快照。手动触发可以指定上游 tag，也可以只组包不发布。
组包时把 `$GITHUB_REPOSITORY` 写进 `build-info.json` 的 `channel`，装好的应用据此认领自己的
发布通道，本仓库以外的构建用 `--release-repo` 指别处、`--no-channel` 关掉。

单测那一步在临时 venv 里跑：`packaging/mpweb_test_deps.py` 从上游 `uv.lock` 挑出托管层真正用到
的 httpx/starlette/uvicorn，按锁里的版本装 —— runner 那个 python 什么都不带，为几个测试也没
必要拉起 201 个包。

解释器、前端产物、站点资源（合计约 110 MB，都带 sha256）用 `actions/cache` 存在 `.cache`，
键取 `runtime-manifest.json` 的摘要；Python 轮子交给 setup-uv 的缓存。macOS 构建分钟按 10 倍计费，
所以「上游没动就不跑」这件事放在了 ubuntu 上的 decide 作业里做。

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
宿主自己的开关另存一份 `host-prefs.json`，上游配置页保存时会整份重写 `app.env`，
把我们的键塞进去迟早被抹掉。

## 插件目录

上游把插件市场安装的插件写进「程序目录/app/plugins」，而应用版的程序目录在 .app 里面，
换一版软件就等于把用户自装的插件一起换掉。`PluginStore` 在起进程之前把这个路径换成指向
`<数据目录>/plugin_packages` 的软链：上游照旧往那个名字写，插件却活在整包升级之外，
插件字节码也不再写进包内。

- 首启把包内目录（含上一版留下的用户插件）搬进数据目录，再建软链；两边同名以数据目录为准。
- 换数据目录（含测试用的临时目录）时重新接链，软链断掉也能自愈。
- 数据目录被清空时用 `plugin-package-init.py` 补回 `app/plugins/__init__.py`，
  `import app.plugins` 不会因此断掉；缺的插件下次启动按数据库里的安装记录自动从市场拉回。
- 接完软链之后 `codesign --verify --deep --strict` 会对这个包报
  「invalid destination for symbolic link in bundle」：`--deep` 遍历到指向包外的软链就判失败。
  出厂那份（还是真实目录）校验是干净的，签名本身没坏，重新签名也不受影响。

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

## 启动与登录

窗口不会先亮一张白页。后台两个子进程到界面端口就绪之间有几十秒（取决于插件数量），
这段时间窗口里顶一张本地启动页，探测到界面端口正常响应才整页切过去；超过 35 秒还没就绪，
启动页会改口说明后台仍在启动并继续等，后台起来页面自己接上。
关掉窗口只隐藏窗口，Dock 图标点回来是同一个窗口；窗口已经销毁时重建一个，
不会再对着释放过的 `NSWindow` 取引用而段错误。

登录页的用户名密码由宿主注入的脚本记住（`LoginAutofill`）：提交表单时记下用户名与密码，
下次登录页出现时填进空着的输入框。密码存在 WebView 的 `localStorage` 里，
明文、只在这台机器上，不想要可以在菜单 MoviePilot ▸ 记住登录密码 关掉，
关掉时会清掉已存的密码。

系统密码库里存过这套凭据的话，WebKit 会连密码一起填上，登录页自己就过去了，
停在登录页要手打的情况只会出现在密码换掉之后。
退出应用时后台要等插件收尾，最长 35 秒才被强杀，这和 CLI 版 `moviepilot stop` 的耐心一致。

## 更新

上游 v3 的更新分 `application` 与 `resources` 两类，检查、下载、校验在应用版里照常跑，
载荷落在数据目录 `temp/moviepilot-update/`；但替换程序目录那步只认 Docker ——
`apply_prepared_update()` 开头就是 `is_docker()` 守卫，CLI 的 `apply-prepared-update` 同。
受管重启 `SystemHelper.can_restart()` 也只认 Docker 的 supervisor、CLI 状态文件和 Windows。
界面里的按钮要有人接活，所以 Docker 里那个 root 更新 worker 在应用版拆成了三段：

1. 托管层拦下 `GET /api/v1/system/restart`，写一份投递文件
   （`temp/mpweb/host-request.json`）就回成功。没带登录凭证的请求原样转给后端，按常规回 401。
2. `UpdateCoordinator` 每两秒取件：只是重启就换两个子进程；数据目录里有 `install.json`
   就说明这次要顺带装更新，于是拉起包内 `python -m mpweb.updater apply-staged --relaunch`
   并交代它等自己退出，然后本进程退场 —— 主程序的可执行文件会被重新签名，
   进程还活着就会被系统连代码页一起作废。
3. `mpweb/updater.py` 落地：按清单校验 sha256、解压、换 `Resources/app` 与 `Resources/web`、
   把旧包的 `sites.*.so` 与 `user.sites.v3.bin` 带进新包（上游后端包不含这两个文件）、
   把 `app/plugins` 重新接成指向数据目录的软链、重写 `build-info.json`、整包重新签名，
   最后把应用叫回来。流水账在 `logs/update.log`，失败时包还是原来那份。

包内 `uv.lock` 摘要与装好的运行时对不上时更新器直接拒绝：那不是换源码能解决的事，
硬换上去只会起不来，这种版本得装完整安装包。`--ignore-lock-change` 留给排查用。

跟版就是组包命令换个 tag。上游是否带依赖变动由锁文件说了算：v3.1.6 → v3.1.7 就多了解析用的
类型存根包（依赖数 196 → 201），这种版本走不了增分包，只能换整包；换上之后包内
`build-info.json` 的锁摘要跟着变，后续那些运行时没动的版本就能吃 56 MB 的增分包。

自己发增量更新不必带上 605 MB 运行时：`packaging/build_delta.py` 从组装好的包里切出
`MoviePilot-<版本>-<架构>-appweb.zip`，v3.1.7 实测 56 MB（整包 dmg 453 MB），
由 `mpweb.updater apply-zip --zip <该包>` 消费。

### 应用内检查更新

取件那一步交给 `mpweb/release.py`：读本包 `build-info.json` 的 `channel.repo`，问
`api.github.com` 要最新发布，按数字段比版本，命中就下 `-appweb.zip` 和它的 `.sha256`，
校验通过才存进数据目录 `temp/mpweb/updates/`（只留最近 3 个），然后投一份
`{"action":"update","zip":…}` 给宿主 —— 剩下的路和其它更新一样，`UpdateCoordinator`
取件、拉起 `mpweb.updater apply-zip`、换包、重新签名、把应用叫回来。

传输走系统 `curl`：包内那个解释器不带根证书，换台机器 urllib 就验不过。
限流挡路时可以用上游配置页里那个 `GITHUB_TOKEN`（存在 `app.env`，值仍是默认占位
`moviepilot` 时按未配置处理）。退出码是有含义的：0 已投递、10 已最新、20 没有通道、1 出错。
30 是「有新版本，但这个包只能整包换」：包内 `uv.lock` 和增分包里的对不上，56 MB 下回来也会被
更新器退回，于是下载当场就退，结论记进 `temp/mpweb/needs-package.json`，同一个已装版本对同一个
新版本只试这一次，不会每轮检查白跑一趟。菜单里手动点「检查更新」会多带 `--interactive`，
这种情况下顺手用默认浏览器打开发布页；后台定时查只更新菜单那行字，不自己弹浏览器。

界面入口是菜单栏图标里的「检查更新」，启动 90 秒后自动查一次、之后每 12 小时一次，
`MOVPILOT_UPDATE_CHECK=0` 关掉后台检查（菜单项仍可手动查）。开发态包没有通道，菜单项直接隐藏。

界面里那个「更新」按钮不走这条路：上游的 `update/download`、`update/install` 开头都有
`can_restart()` 守卫，应用版过不了，点了只会得到一句「当前运行环境不支持升级操作」。
托管层因此把这两个接口接下来，投递一份 `{"action":"open","url":…}` 让宿主用默认浏览器打开
本仓库的 `releases/latest`，换整包本来就得由安装包动手。
`/download` 回 `success:true`，免得前端再补一句「下载失败」；`/install` 回 `success:false`
带上同一句说明，因为那时候真的什么都没装。

完整安装包是自足的：`dist/MoviePilot-<版本>-<架构>.dmg` 拖进「应用程序」就能用，安装和运行
都不要求访问 GitHub。想让应用连版本都不去查，菜单里取消「自动检查更新」即可，状态写进
`host-prefs.json`，之后只有手动点「检查更新」才会打网络。上游后端自己那套联网功能（插件市场、
站点资源、CookieCloud）不受这个开关管，也不属于打包这一层。

## 已知缺口

- 签名目前只有 ad-hoc：换 Developer ID 证书 + 公证只需给 `build_app.py --identity`，
  但内嵌 572 个二进制的逐层签名要在真证书下再验一遍。
- 增分包只带 `Resources/app` 与 `Resources/web`：托管层、Swift 壳层、内嵌运行时的改动都得换
  整包，而这些恰好也住在增分包能碰到的目录之外。CI 的 Release 同时发 dmg，应用检测到这类
  版本（锁摘要对不上）时会在下载那步就退回，菜单显示「新版本 x.y.z 要装完整安装包」。
- 首屏之后的首次设置页直接用上游前端自带的 `/initialize`，无需自建向导。
- 内嵌运行时占了 605 MB，其中 langchain/boto3 等可选 SDK 可以按插件需要再裁一轮。
- `build_runtime.py` 每次组包都重新解包解释器：CI 靠 `.cache` 免掉了下载，解包那几十秒还没省。
