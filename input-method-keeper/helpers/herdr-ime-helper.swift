import Carbon
import Cocoa
import Foundation

struct ImeHelperError: Error, CustomStringConvertible {
    let description: String
}

func usage() -> String {
    return """
    usage:
      herdr-ime-helper current
      herdr-ime-helper list
      herdr-ime-helper select <input_source_id> [--refresh] [--wait-ms N]
      herdr-ime-helper refresh [--wait-ms N]
    """
}

func fail(_ message: String) throws -> Never {
    throw ImeHelperError(description: message)
}

func property<T>(_ source: TISInputSource, _ key: CFString, as type: T.Type) -> T? {
    guard let rawValue = TISGetInputSourceProperty(source, key) else {
        return nil
    }
    let value = Unmanaged<AnyObject>.fromOpaque(rawValue).takeUnretainedValue()
    return value as? T
}

func sourceID(_ source: TISInputSource) -> String {
    property(source, kTISPropertyInputSourceID, as: String.self) ?? ""
}

func sourceName(_ source: TISInputSource) -> String {
    property(source, kTISPropertyLocalizedName, as: String.self) ?? ""
}

func sourceLanguages(_ source: TISInputSource) -> [String] {
    property(source, kTISPropertyInputSourceLanguages, as: [String].self) ?? []
}

func isSelectCapable(_ source: TISInputSource) -> Bool {
    property(source, kTISPropertyInputSourceIsSelectCapable, as: Bool.self) ?? false
}

func allInputSources() -> [TISInputSource] {
    let list = TISCreateInputSourceList(nil, false).takeRetainedValue()
    return (list as? [TISInputSource] ?? []).filter(isSelectCapable)
}

func currentInputSource() throws -> TISInputSource {
    guard let unmanaged = TISCopyCurrentKeyboardInputSource() else {
        try fail("failed to read current input source")
    }
    return unmanaged.takeRetainedValue()
}

func findInputSource(_ id: String) -> TISInputSource? {
    allInputSources().first { sourceID($0) == id }
}

func selectInputSource(_ id: String) throws {
    guard let source = findInputSource(id) else {
        try fail("input source not found: \(id)")
    }
    let status = TISSelectInputSource(source)
    if status != noErr {
        try fail("TISSelectInputSource failed for \(id): \(status)")
    }
}

func parseWaitMs(_ args: [String], defaultWaitMs: Int = 150) throws -> Int {
    var waitMs = defaultWaitMs
    var index = 0
    while index < args.count {
        if args[index] == "--wait-ms" {
            guard index + 1 < args.count else {
                try fail("--wait-ms requires a value")
            }
            guard let parsed = Int(args[index + 1]), parsed >= 0 else {
                try fail("--wait-ms must be a non-negative integer")
            }
            waitMs = parsed
            index += 2
        } else {
            index += 1
        }
    }
    return waitMs
}

func hasFlag(_ args: [String], _ flag: String) -> Bool {
    args.contains(flag)
}

func refreshInputContext(waitMs: Int) {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    guard let screen = NSScreen.main else {
        return
    }

    let width: CGFloat = 3
    let height: CGFloat = 3
    let frame = screen.visibleFrame
    let rect = NSRect(
        x: frame.maxX - width - 8,
        y: frame.minY + 8,
        width: width,
        height: height
    )
    let window = NSWindow(
        contentRect: rect,
        styleMask: [.titled],
        backing: .buffered,
        defer: false
    )
    window.isOpaque = true
    window.backgroundColor = NSColor.purple
    window.titlebarAppearsTransparent = true
    window.level = .screenSaver
    window.collectionBehavior = [.canJoinAllSpaces, .stationary]
    window.makeKeyAndOrderFront(nil)
    app.activate(ignoringOtherApps: true)

    DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(waitMs)) {
        window.close()
        app.terminate(nil)
    }
    app.run()
}

func run(_ argv: [String]) throws {
    guard argv.count >= 2 else {
        try fail(usage())
    }

    let command = argv[1]
    let args = Array(argv.dropFirst(2))

    switch command {
    case "current":
        print(sourceID(try currentInputSource()))
    case "list":
        for source in allInputSources() {
            let id = sourceID(source)
            let name = sourceName(source)
            let languages = sourceLanguages(source).joined(separator: ",")
            print("\(id)\t\(name)\t\(languages)")
        }
    case "select":
        guard let id = args.first, !id.hasPrefix("--") else {
            try fail("select requires an input source id")
        }
        let optionArgs = Array(args.dropFirst())
        try selectInputSource(id)
        if hasFlag(optionArgs, "--refresh") {
            refreshInputContext(waitMs: try parseWaitMs(optionArgs))
        }
    case "refresh":
        refreshInputContext(waitMs: try parseWaitMs(args))
    case "-h", "--help", "help":
        print(usage())
    default:
        try fail("unknown command: \(command)\n\(usage())")
    }
}

do {
    try run(CommandLine.arguments)
} catch let error as ImeHelperError {
    fputs(error.description + "\n", stderr)
    exit(2)
} catch {
    fputs(String(describing: error) + "\n", stderr)
    exit(1)
}
