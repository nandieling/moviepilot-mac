import Foundation

/// 启动期间顶在窗口里的本地页面。
///
/// 后端和前端托管层都是子进程，界面端口就绪要一两秒。这段时间直接加载界面地址只会得到
/// 连接失败，窗口在几张白页之间来回跳，看起来像卡死。先用这份本地页顶着，探到界面能正常
/// 响应再整页切过去，用户看到的是一次跳转而不是一段白屏。
enum LaunchSplash {
    static func html(reason: Reason) -> String {
        let title: String
        let detail: String
        let spinner: String
        switch reason {
        case .starting:
            title = "正在启动后台服务"
            detail = "要装载插件与站点资源，插件装得多时会慢一些；起来之后页面会自己接上。"
            spinner = "<div class=\"spinner\"></div>"
        case .timedOut:
            title = "后台服务仍在启动"
            detail = "首次启动要装载插件与站点资源，可能得一两分钟；起来之后页面会自己接上。"
                       + "等得太久可以从菜单栏图标打开日志目录排查，或选择「重启后台服务」。"
            spinner = "<div class=\"mark\">!</div>"
        }
        return """
        <!doctype html>
        <html lang="zh-Hans"><head><meta charset="utf-8">
        <style>
          html,body{height:100%;margin:0}
          body{background:#14151a;color:#e8e9ee;font:14px/1.6 -apple-system,"PingFang SC",sans-serif;
               display:flex;align-items:center;justify-content:center;-webkit-user-select:none;user-select:none}
          .box{text-align:center}
          .spinner{width:26px;height:26px;margin:0 auto 16px;border-radius:50%;
                   border:2px solid rgba(232,233,238,.18);border-top-color:#4d8bf4;
                   animation:spin .9s linear infinite}
          .mark{width:26px;height:26px;line-height:24px;margin:0 auto 16px;border-radius:50%;
                border:1px solid rgba(232,233,238,.28);color:#f0b45b;font-weight:600}
          .name{font-size:15px;letter-spacing:.08em;color:#9aa0ae}
          .title{font-size:19px;margin:6px 0 4px}
          .detail{color:#7d8492;font-size:12.5px;max-width:30em;margin:0 auto}
          @keyframes spin{to{transform:rotate(360deg)}}
        </style></head>
        <body><div class="box">\(spinner)
          <div class="name">MOVIEPILOT</div>
          <div class="title">\(title)</div>
          <div class="detail">\(detail)</div>
        </div></body></html>
        """
    }

    enum Reason {
        case starting
        case timedOut
    }
}
