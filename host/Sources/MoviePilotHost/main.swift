import AppKit

let application = NSApplication.shared
let appDelegate = AppDelegate()
application.delegate = appDelegate
// 菜单栏常驻：关窗口只是收起界面，服务仍在跑。
application.setActivationPolicy(.regular)
application.run()
