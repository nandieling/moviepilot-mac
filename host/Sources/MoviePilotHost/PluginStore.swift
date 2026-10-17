import Darwin
import Foundation
import os.log

/// 把包内插件目录改成指向数据目录的软链。
///
/// 上游把插件市场安装的插件写进「程序目录/app/plugins」，而 mac 版的程序目录就在 .app 里，
/// 换一版软件等于把这个目录连用户自装的插件一起换掉。让上游照旧往这个名字写、
/// 但这个名字指向数据目录，插件就活在同版本升级之外，字节码也不再写进包内。
enum PluginStore {
    private static let log = Logger(subsystem: BundleLayout.bundleIdentifier, category: "plugin-store")

    /// 幂等：已就绪时什么都不做。失败只记日志，退回包内真实目录照样能跑。
    static func prepare(layout: BundleLayout) {
        guard layout.isPackaged else { return }
        let fm = FileManager.default
        let mirror = layout.pluginMirror.standardizedFileURL
        let marker = mirror.appendingPathComponent("__init__.py")
        let inBundle = layout.backendRoot.appendingPathComponent("app/plugins").standardizedFileURL
        do {
            try fm.createDirectory(at: marker.deletingLastPathComponent(), withIntermediateDirectories: true)
            seedPackageMarker(into: marker, seed: layout.pluginPackageSeed, fileManager: fm)
            // 必须用 lstat：软链目标被换掉或已删除时，fileExists 会跟着链接判定成「不存在」，
            // 后面建链接就会撞上「文件已存在」，重定位从此再也修不回来。
            var status = stat()
            if Darwin.lstat(inBundle.path, &status) == 0 {
                let isLink = (status.st_mode & S_IFMT) == S_IFLNK
                if isLink, let destination = try? fm.destinationOfSymbolicLink(atPath: inBundle.path),
                   resolve(destination, relativeTo: inBundle) == mirror {
                    return
                }
                if isLink {
                    log.warning("包内插件目录指向别处，重建软链")
                } else {
                    transfer(from: inBundle, to: mirror, fileManager: fm)
                }
                try fm.removeItem(at: inBundle)
            }
            try fm.createSymbolicLink(at: inBundle, withDestinationURL: mirror)
            log.info("插件目录已指向 \(mirror.path, privacy: .public)")
        } catch {
            log.error("插件目录重定位失败，本轮改用包内目录：\(error.localizedDescription, privacy: .public)")
        }
    }

    /// 上游那份 `app/plugins/__init__.py` 只有一句文档字符串；搬走后若数据目录被清空，
    /// 就按组包时留下的出厂副本补回来，保证 `import app.plugins` 始终成立。
    private static func seedPackageMarker(into marker: URL, seed: URL, fileManager fm: FileManager) {
        guard !fm.fileExists(atPath: marker.path) else { return }
        if fm.fileExists(atPath: seed.path) {
            try? fm.copyItem(at: seed, to: marker)
        } else {
            fm.createFile(atPath: marker.path, contents: Data())
        }
    }

    private static func resolve(_ destination: String, relativeTo base: URL) -> URL {
        if (destination as NSString).isAbsolutePath {
            return URL(fileURLWithPath: destination).standardizedFileURL
        }
        return base.deletingLastPathComponent().appendingPathComponent(destination).standardizedFileURL
    }

    /// 旧版本的包内目录里可能已经有用户插件，先搬出去再建软链。
    /// 两边同名时以数据目录里那份为准：包内那份只是上一次运行留下的副本。
    private static func transfer(from source: URL, to mirror: URL, fileManager fm: FileManager) {
        guard let names = try? fm.contentsOfDirectory(atPath: source.path) else { return }
        for name in names where name != ".DS_Store" {
            let from = source.appendingPathComponent(name)
            let to = mirror.appendingPathComponent(name)
            if fm.fileExists(atPath: to.path) {
                try? fm.removeItem(at: from)
            } else {
                try? fm.moveItem(at: from, to: to)
            }
        }
    }
}
