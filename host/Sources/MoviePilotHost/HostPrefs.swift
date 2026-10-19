import Foundation

/// 宿主自己的开关，存在数据目录下的 host-prefs.json。
///
/// 上游的配置页保存时会整份重写 app.env，把我们的键塞进去迟早被抹掉，
/// 所以界面上的开关单独存一份。以后加开关往这个结构里补字段就行。
struct HostPrefs: Codable {
    /// 定时查发布通道。关掉之后只剩界面上手动那一下，全程不碰 GitHub。
    var autoUpdateCheck = true

    private enum CodingKeys: String, CodingKey { case autoUpdateCheck }

    /// 少键按默认值处理：老那份配置不该被一次升级就判成损坏。
    init() {}

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        autoUpdateCheck = (try? container.decode(Bool.self, forKey: .autoUpdateCheck)) ?? true
    }

    static func load(_ layout: BundleLayout) -> HostPrefs {
        guard let data = try? Data(contentsOf: layout.prefsFile),
              let prefs = try? JSONDecoder().decode(HostPrefs.self, from: data) else { return HostPrefs() }
        return prefs
    }

    func save(_ layout: BundleLayout) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(self) else { return }
        // .atomic 先写临时文件再改名：宿主随时可能被强杀，别留下半份 JSON
        try? data.write(to: layout.prefsFile, options: .atomic)
    }
}
