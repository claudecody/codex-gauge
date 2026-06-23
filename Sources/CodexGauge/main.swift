import AppKit
import Foundation

struct UsageBucket {
    let cost: Double
    let sessions: Int
    let totalTokens: Int
    let cachedInputTokens: Int
    let uncachedInputTokens: Int
    let outputTokens: Int
    let reasoningOutputTokens: Int
}

struct UsageSnapshot {
    let today: UsageBucket
    let week: UsageBucket
    let month: UsageBucket
    let updatedAt: Date
}

struct GaugeConfig {
    let weeklySoftBudget: Double
    let weeklyMaxBudget: Double

    static func load() -> GaugeConfig {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let path = "\(home)/.codex-gauge.json"
        let fallback = GaugeConfig(weeklySoftBudget: 30, weeklyMaxBudget: 100)

        guard
            let data = FileManager.default.contents(atPath: path),
            let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return fallback
        }

        return GaugeConfig(
            weeklySoftBudget: double(payload["weekly_soft_budget_usd"], defaultValue: fallback.weeklySoftBudget),
            weeklyMaxBudget: double(payload["weekly_max_budget_usd"], defaultValue: fallback.weeklyMaxBudget)
        )
    }
}

enum UsageError: Error, CustomStringConvertible {
    case missingTool(String)
    case commandFailed(String)
    case invalidJSON

    var description: String {
        switch self {
        case .missingTool(let path):
            return "Missing usage tool: \(path)"
        case .commandFailed(let detail):
            return detail
        case .invalidJSON:
            return "Could not parse codex-usage output."
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let refreshInterval: TimeInterval = 300
    private var config = GaugeConfig.load()
    private var timer: Timer?
    private var isRefreshing = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem.button?.title = "Gauge ..."
        buildMenu(status: "Loading", snapshot: nil, error: nil)
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: refreshInterval, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    @objc private func refreshFromMenu(_ sender: Any?) {
        refresh()
    }

    @objc private func openConfig(_ sender: Any?) {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let path = "\(home)/.codex-gauge.json"
        if !FileManager.default.fileExists(atPath: path) {
            let payload = """
            {
              "weekly_soft_budget_usd": 30.0,
              "weekly_max_budget_usd": 100.0
            }
            """
            try? payload.write(toFile: path, atomically: true, encoding: .utf8)
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    @objc private func quit(_ sender: Any?) {
        NSApplication.shared.terminate(nil)
    }

    private func refresh() {
        guard !isRefreshing else { return }
        isRefreshing = true
        config = GaugeConfig.load()
        statusItem.button?.title = "Gauge ..."

        DispatchQueue.global(qos: .utility).async {
            let result = Result { try self.loadSnapshot() }
            DispatchQueue.main.async {
                self.isRefreshing = false
                switch result {
                case .success(let snapshot):
                    let title = self.title(for: snapshot)
                    self.statusItem.button?.title = title
                    self.buildMenu(status: title, snapshot: snapshot, error: nil)
                case .failure(let error):
                    self.statusItem.button?.title = "Gauge ?"
                    self.buildMenu(status: "Gauge ?", snapshot: nil, error: String(describing: error))
                }
            }
        }
    }

    private func title(for snapshot: UsageSnapshot) -> String {
        let cost = snapshot.week.cost
        return "Gauge \(moneyCompact(cost))/\(moneyCompact(config.weeklyMaxBudget))"
    }

    private func buildMenu(status: String, snapshot: UsageSnapshot?, error: String?) {
        let menu = NSMenu()

        let header = NSMenuItem(title: status, action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())

        if let error {
            let item = NSMenuItem(title: error, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        } else if let snapshot {
            addBucket(menu, label: "Today", bucket: snapshot.today)
            addBucket(menu, label: "Week", bucket: snapshot.week)
            addBucket(menu, label: "Month", bucket: snapshot.month)
            menu.addItem(.separator())

            let soft = "\(money(snapshot.week.cost)) / \(money(config.weeklySoftBudget)) (\(String(format: "%.0f", percent(snapshot.week.cost, config.weeklySoftBudget)))%)"
            let max = "\(money(snapshot.week.cost)) / \(money(config.weeklyMaxBudget)) (\(String(format: "%.0f", percent(snapshot.week.cost, config.weeklyMaxBudget)))%)"
            addDisabled(menu, "Weekly soft: \(soft)")
            addDisabled(menu, "Weekly max: \(max)")
            addDisabled(menu, "Updated: \(timeFormatter.string(from: snapshot.updatedAt))")
            addDisabled(menu, "Local estimate, not official billing")
        }

        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Refresh", action: #selector(refreshFromMenu(_:)), keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "Open Config", action: #selector(openConfig(_:)), keyEquivalent: "d"))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit(_:)), keyEquivalent: "q"))
        statusItem.menu = menu
    }

    private func addBucket(_ menu: NSMenu, label: String, bucket: UsageBucket) {
        addDisabled(menu, "\(label): \(money(bucket.cost))  \(compact(bucket.totalTokens)) tokens")
    }

    private func addDisabled(_ menu: NSMenu, _ title: String) {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        menu.addItem(item)
    }

    private func loadSnapshot() throws -> UsageSnapshot {
        let today = Date()
        return UsageSnapshot(
            today: try runUsage(period: "day", since: startOfDay(today), until: today),
            week: try runUsage(period: "week", since: startOfWeek(today), until: today),
            month: try runUsage(period: "month", since: startOfMonth(today), until: today),
            updatedAt: Date()
        )
    }

    private func runUsage(period: String, since: Date, until: Date) throws -> UsageBucket {
        let tool = try usageToolPath()

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [
            tool,
            "--period", period,
            "--since", dayFormatter.string(from: since),
            "--until", dayFormatter.string(from: until),
            "--format", "json",
        ]

        let output = Pipe()
        let error = Pipe()
        process.standardOutput = output
        process.standardError = error

        try process.run()
        process.waitUntilExit()

        let outputData = output.fileHandleForReading.readDataToEndOfFile()
        let errorData = error.fileHandleForReading.readDataToEndOfFile()
        if process.terminationStatus != 0 {
            let detail = String(data: errorData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
                ?? "codex-gauge-usage.py failed"
            throw UsageError.commandFailed(detail)
        }

        guard
            let payload = try JSONSerialization.jsonObject(with: outputData) as? [String: Any],
            let total = payload["total"] as? [String: Any]
        else {
            throw UsageError.invalidJSON
        }

        return UsageBucket(
            cost: double(total["hypothetical_cost_usd"]),
            sessions: int(total["sessions"]),
            totalTokens: int(total["total_tokens"]),
            cachedInputTokens: int(total["cached_input_tokens"]),
            uncachedInputTokens: int(total["uncached_input_tokens"]),
            outputTokens: int(total["output_tokens"]),
            reasoningOutputTokens: int(total["reasoning_output_tokens"])
        )
    }

    private func usageToolPath() throws -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            ProcessInfo.processInfo.environment["CODEX_GAUGE_USAGE_TOOL"],
            Bundle.main.path(forResource: "codex-gauge-usage", ofType: "py"),
            "\(home)/.local/bin/codex-gauge-usage",
            "\(home)/.codex/tools/codex_usage.py",
        ].compactMap { $0 }

        for candidate in candidates {
            if FileManager.default.fileExists(atPath: candidate) {
                return candidate
            }
        }

        throw UsageError.missingTool(candidates.joined(separator: ", "))
    }

    private func startOfDay(_ date: Date) -> Date {
        Calendar.current.startOfDay(for: date)
    }

    private func startOfWeek(_ date: Date) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.firstWeekday = 2
        let components = calendar.dateComponents([.yearForWeekOfYear, .weekOfYear], from: date)
        return calendar.date(from: components) ?? startOfDay(date)
    }

    private func startOfMonth(_ date: Date) -> Date {
        var components = Calendar.current.dateComponents([.year, .month], from: date)
        components.day = 1
        return Calendar.current.date(from: components) ?? startOfDay(date)
    }

    private var dayFormatter: DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }

    private var timeFormatter: DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale.current
        formatter.timeStyle = .short
        formatter.dateStyle = .none
        return formatter
    }
}

private func double(_ value: Any?) -> Double {
    double(value, defaultValue: 0)
}

private func double(_ value: Any?, defaultValue: Double) -> Double {
    if let value = value as? Double { return value }
    if let value = value as? Int { return Double(value) }
    if let value = value as? String { return Double(value) ?? defaultValue }
    return defaultValue
}

private func int(_ value: Any?) -> Int {
    if let value = value as? Int { return value }
    if let value = value as? Double { return Int(value) }
    if let value = value as? String { return Int(value) ?? 0 }
    return 0
}

private func percent(_ cost: Double, _ budget: Double) -> Double {
    guard budget > 0 else { return 0 }
    return cost / budget * 100
}

private func money(_ value: Double) -> String {
    String(format: "$%.2f", value)
}

private func moneyCompact(_ value: Double) -> String {
    if value >= 1000 {
        return String(format: "$%.1fk", value / 1000)
    }
    if value >= 100 {
        return String(format: "$%.0f", value)
    }
    return String(format: "$%.2f", value)
}

private func compact(_ value: Int) -> String {
    let number = Double(value)
    if number >= 1_000_000 {
        return String(format: "%.1fM", number / 1_000_000)
    }
    if number >= 1_000 {
        return String(format: "%.1fk", number / 1_000)
    }
    return "\(value)"
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
