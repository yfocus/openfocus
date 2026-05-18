# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

FLOAT_BALL_BG = "#064e3b"
FLOAT_BALL_PANEL_BG = "#06140f"
FLOAT_BALL_CARD_BG = "#0b2118"
FLOAT_BALL_BORDER = "#1f6f50"
FLOAT_BALL_TEXT = "#ecfdf5"
FLOAT_BALL_MUTED = "#a7f3d0"
FLOAT_BALL_ACCENT = "#34d399"
SUMMARY_PATH = "/api/agent_activity/summary?limit=30"
READY_FILE_ENV = "OPENFOCUS_FLOAT_BALL_READY_FILE"


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list_of_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]


def _safe_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _counts(summary: dict) -> tuple[int, int]:
    counts = _as_dict(summary.get("counts") if isinstance(summary, dict) else {})
    running = _safe_count(counts.get("running"))
    waiting = _safe_count(counts.get("waiting"))
    if running or waiting:
        return running, waiting
    sections = _section_items(summary if isinstance(summary, dict) else {})
    return len(sections["running"]), len(sections["waiting"])


def _summary() -> dict[str, Any]:
    raw = os.environ.get("OPENFOCUS_FLOAT_BALL_SUMMARY_JSON") or "{}"
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _signal_ready() -> None:
    path = str(os.environ.get(READY_FILE_ENV) or "").strip()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("ready\n")
    except OSError:
        pass


def _absolute_url(base_url: str, raw_url: object) -> str:
    raw = str(raw_url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme:
        return raw
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return raw
    if raw.startswith("/"):
        return f"{base}{raw}"
    return f"{base}/{raw}"


def _fetch_summary(base_url: str, *, timeout: float = 2.5) -> dict[str, Any]:
    url = _absolute_url(base_url, SUMMARY_PATH)
    if not url:
        return {}
    try:
        req = urllib.request.Request(
            url,
            headers={"Cache-Control": "no-store", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _post_url(url: str, *, timeout: float = 2.5) -> bool:
    if not str(url or "").strip():
        return False
    try:
        req = urllib.request.Request(str(url), data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def _bucket_items(summary: dict[str, Any], bucket: str) -> list[dict[str, Any]]:
    buckets = _as_dict(summary.get("buckets"))
    if bucket == "waiting":
        direct = _as_list_of_dicts(buckets.get("waiting"))
        if not direct:
            direct = _as_list_of_dicts(buckets.get("completed"))
        if direct:
            return direct
    direct = _as_list_of_dicts(buckets.get(bucket))
    if direct:
        return direct
    items = _as_list_of_dicts(summary.get("items"))
    if bucket == "waiting":
        return [
            x
            for x in items
            if str(x.get("bucket") or "") in {"waiting", "completed"}
        ]
    return [x for x in items if str(x.get("bucket") or "") == bucket]


def _section_items(summary: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "running": _bucket_items(summary, "running"),
        "waiting": _bucket_items(summary, "waiting"),
        "next_move": _bucket_items(summary, "next_move"),
    }


def _clean_text(value: object, *, max_len: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def _agent_label(item: dict[str, Any]) -> str:
    return _clean_text(item.get("agent_name") or item.get("agent_runtime"), max_len=64)


def _state_label(item: dict[str, Any]) -> str:
    bucket = str(item.get("bucket") or "")
    typ = str(item.get("type") or "")
    if bucket == "running":
        return "Running"
    if bucket == "next_move":
        return "Recommended"
    if typ == "review_ready":
        return "Review ready"
    if typ == "waiting":
        waiting_kind = str(item.get("waiting_kind") or "")
        if "approval" in waiting_kind:
            return "Waiting approval"
        if "confirmation" in waiting_kind:
            return "Waiting confirmation"
        return "Waiting input"
    if typ == "stale":
        return "Stale"
    if typ == "canceled":
        return "Canceled"
    if typ == "failed":
        return "Failed"
    if typ == "blocked":
        return "Waiting input"
    return "Completed"


def _parse_iso(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _duration_text(item: dict[str, Any], *, now: dt.datetime | None = None) -> str:
    started = _parse_iso(item.get("state_since") or item.get("created_at"))
    if started is None:
        return ""
    current = now or dt.datetime.now(dt.timezone.utc)
    seconds = max(0, int((current - started).total_seconds()))
    if seconds < 30:
        return "for now"
    minutes = seconds // 60
    if minutes < 60:
        return f"for {minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"for {hours}h"
    return f"for {hours // 24}d"


def _primary_action(item: dict[str, Any]) -> tuple[str, str]:
    action = _as_dict(item.get("action"))
    url = str(action.get("primary_url") or action.get("fallback_url") or "").strip()
    label = str(action.get("primary_label") or action.get("fallback_label") or "Open")
    return label, url


def _dismiss_path(item: dict[str, Any]) -> str:
    return str(item.get("dismiss_url") or "").strip()


def _item_title(item: dict[str, Any]) -> str:
    return _clean_text(item.get("title") or item.get("task_title") or "Untitled", max_len=120)


SWIFT_HELPER = r"""
import AppKit
import Foundation

let argv = CommandLine.arguments
func arg(_ idx: Int, _ fallback: String = "") -> String {
    return idx < argv.count ? argv[idx] : fallback
}

let selfPath = arg(0)
if !selfPath.isEmpty {
    try? FileManager.default.removeItem(atPath: selfPath)
}

let baseURL = arg(3).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
let initialRunningText = arg(4, "0")
let initialWaitingText = arg(5, "0")
let readyFile = ProcessInfo.processInfo.environment["OPENFOCUS_FLOAT_BALL_READY_FILE"] ?? ""
let ballColor = NSColor(red: 0.024, green: 0.306, blue: 0.231, alpha: 0.97)
let panelColor = NSColor(red: 0.024, green: 0.078, blue: 0.055, alpha: 0.98)
let cardColor = NSColor(red: 0.043, green: 0.129, blue: 0.094, alpha: 1.0)
let borderColor = NSColor(red: 0.122, green: 0.435, blue: 0.314, alpha: 1.0)
let textColor = NSColor(red: 0.925, green: 0.992, blue: 0.961, alpha: 1.0)
let mutedColor = NSColor(red: 0.655, green: 0.953, blue: 0.816, alpha: 1.0)
let accentColor = NSColor(red: 0.204, green: 0.827, blue: 0.600, alpha: 1.0)
let summaryPath = "/api/agent_activity/summary?limit=30"

func signalReady() {
    if readyFile.isEmpty { return }
    try? "ready\n".write(toFile: readyFile, atomically: true, encoding: .utf8)
}

func clean(_ value: Any?, maxLen: Int = 180) -> String {
    var text = ""
    if let s = value as? String {
        text = s
    } else if let v = value {
        text = "\(v)"
    }
    text = text.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
        .trimmingCharacters(in: .whitespacesAndNewlines)
    if text.count > maxLen {
        let idx = text.index(text.startIndex, offsetBy: maxLen - 3)
        return String(text[..<idx]).trimmingCharacters(in: .whitespacesAndNewlines) + "..."
    }
    return text
}

func asDict(_ value: Any?) -> [String: Any] {
    return value as? [String: Any] ?? [:]
}

func asItems(_ value: Any?) -> [[String: Any]] {
    return value as? [[String: Any]] ?? []
}

func summaryFromEnv() -> [String: Any] {
    guard let raw = ProcessInfo.processInfo.environment["OPENFOCUS_FLOAT_BALL_SUMMARY_JSON"],
          let data = raw.data(using: .utf8),
          let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return [:]
    }
    return payload
}

func normalizedURL(_ rawValue: Any?) -> URL? {
    let raw = clean(rawValue, maxLen: 2048)
    if raw.isEmpty { return nil }
    if let url = URL(string: raw), url.scheme != nil {
        return url
    }
    if baseURL.isEmpty { return URL(string: raw) }
    if raw.hasPrefix("/") {
        return URL(string: baseURL + raw)
    }
    return URL(string: baseURL + "/" + raw)
}

func fetchSummary(_ completion: @escaping ([String: Any]) -> Void) {
    guard let url = normalizedURL(summaryPath) else {
        completion([:])
        return
    }
    var req = URLRequest(url: url)
    req.cachePolicy = .reloadIgnoringLocalCacheData
    req.timeoutInterval = 3
    URLSession.shared.dataTask(with: req) { data, _, _ in
        var payload: [String: Any] = [:]
        if let data = data,
           let decoded = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            payload = decoded
        }
        DispatchQueue.main.async { completion(payload) }
    }.resume()
}

func postURL(_ url: URL, _ completion: @escaping () -> Void) {
    var req = URLRequest(url: url)
    req.httpMethod = "POST"
    req.timeoutInterval = 3
    URLSession.shared.dataTask(with: req) { _, _, _ in
        DispatchQueue.main.async { completion() }
    }.resume()
}

func bucketItems(_ summary: [String: Any], _ bucket: String) -> [[String: Any]] {
    let buckets = asDict(summary["buckets"])
    if bucket == "waiting" {
        let waiting = asItems(buckets["waiting"])
        if !waiting.isEmpty { return waiting }
        let completed = asItems(buckets["completed"])
        if !completed.isEmpty { return completed }
    }
    let direct = asItems(buckets[bucket])
    if !direct.isEmpty { return direct }
    let items = asItems(summary["items"])
    if bucket == "waiting" {
        return items.filter {
            let b = clean($0["bucket"])
            return b == "waiting" || b == "completed"
        }
    }
    return items.filter { clean($0["bucket"]) == bucket }
}

func counts(_ summary: [String: Any]) -> (Int, Int) {
    let c = asDict(summary["counts"])
    let r = max(0, Int(clean(c["running"])) ?? 0)
    let w = max(0, Int(clean(c["waiting"])) ?? 0)
    if r > 0 || w > 0 { return (r, w) }
    return (bucketItems(summary, "running").count, bucketItems(summary, "waiting").count)
}

func itemTitle(_ item: [String: Any]) -> String {
    let title = clean(item["title"], maxLen: 120)
    if !title.isEmpty { return title }
    let task = clean(item["task_title"], maxLen: 120)
    return task.isEmpty ? "Untitled" : task
}

func agentLabel(_ item: [String: Any]) -> String {
    let a = clean(item["agent_name"], maxLen: 64)
    if !a.isEmpty { return a }
    return clean(item["agent_runtime"], maxLen: 64)
}

func stateLabel(_ item: [String: Any]) -> String {
    let bucket = clean(item["bucket"])
    let typ = clean(item["type"])
    if bucket == "running" { return "Running" }
    if bucket == "next_move" { return "Recommended" }
    if typ == "review_ready" { return "Review ready" }
    if typ == "waiting" {
        let wk = clean(item["waiting_kind"])
        if wk.contains("approval") { return "Waiting approval" }
        if wk.contains("confirmation") { return "Waiting confirmation" }
        return "Waiting input"
    }
    if typ == "stale" { return "Stale" }
    if typ == "canceled" { return "Canceled" }
    if typ == "failed" { return "Failed" }
    if typ == "blocked" { return "Waiting input" }
    return "Completed"
}

let isoWithFraction: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()
let isoPlain = ISO8601DateFormatter()

func parseDate(_ raw: String) -> Date? {
    if let d = isoWithFraction.date(from: raw) { return d }
    return isoPlain.date(from: raw)
}

func durationText(_ item: [String: Any]) -> String {
    let raw = clean(item["state_since"]).isEmpty ? clean(item["created_at"]) : clean(item["state_since"])
    guard let date = parseDate(raw) else { return "" }
    let seconds = max(0, Int(Date().timeIntervalSince(date)))
    if seconds < 30 { return "for now" }
    let minutes = seconds / 60
    if minutes < 60 { return "for \(minutes)m" }
    let hours = minutes / 60
    if hours < 48 { return "for \(hours)h" }
    return "for \(hours / 24)d"
}

func primaryURL(_ item: [String: Any]) -> URL? {
    let action = asDict(item["action"])
    if let url = normalizedURL(action["primary_url"]) { return url }
    return normalizedURL(action["fallback_url"])
}

func primaryLabel(_ item: [String: Any]) -> String {
    let action = asDict(item["action"])
    let label = clean(action["primary_label"])
    if !label.isEmpty { return label }
    let fallback = clean(action["fallback_label"])
    return fallback.isEmpty ? "Open" : fallback
}

func dismissURL(_ item: [String: Any]) -> URL? {
    return normalizedURL(item["dismiss_url"])
}

final class ClosureSleeve: NSObject {
    let closure: () -> Void
    init(_ closure: @escaping () -> Void) {
        self.closure = closure
    }
    @objc func invoke(_ sender: Any?) {
        closure()
    }
}

final class ClickSurface: NSView {
    weak var owner: AppDelegate?
    private var downPoint: NSPoint = .zero
    private var moved = false

    override var acceptsFirstResponder: Bool { true }

    override func mouseDown(with event: NSEvent) {
        downPoint = event.locationInWindow
        moved = false
    }

    override func mouseDragged(with event: NSEvent) {
        let point = event.locationInWindow
        if abs(point.x - downPoint.x) < 4 && abs(point.y - downPoint.y) < 4 {
            return
        }
        moved = true
        owner?.closePopover()
        guard let window = self.window else { return }
        let screenPoint = NSEvent.mouseLocation
        window.setFrameOrigin(NSPoint(x: screenPoint.x - downPoint.x, y: screenPoint.y - downPoint.y))
    }

    override func mouseUp(with event: NSEvent) {
        if moved {
            moved = false
            return
        }
        owner?.togglePopover()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: NSPanel?
    var popoverPanel: NSPanel?
    var rootView: NSView?
    var badge: NSTextField?
    var summary: [String: Any] = summaryFromEnv()
    var sleeves: [ClosureSleeve] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildBall()
        updateBadge()
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.refreshSummary(render: false)
        }
    }

    func buildBall() {
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 900, height: 700)
        let rect = NSRect(x: screen.maxX - 206, y: screen.minY + 82, width: 172, height: 58)
        let panel = NSPanel(
            contentRect: rect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true

        let view = ClickSurface(frame: NSRect(x: 0, y: 0, width: 172, height: 58))
        view.owner = self
        view.wantsLayer = true
        view.layer?.backgroundColor = ballColor.cgColor
        view.layer?.cornerRadius = 18
        view.layer?.borderWidth = 1
        view.layer?.borderColor = borderColor.cgColor

        let title = NSTextField(labelWithString: "Inbox")
        title.frame = NSRect(x: 14, y: 31, width: 140, height: 18)
        title.font = NSFont.boldSystemFont(ofSize: 13)
        title.textColor = textColor
        view.addSubview(title)

        let badge = NSTextField(labelWithString: "R \(initialRunningText)   W \(initialWaitingText)")
        badge.frame = NSRect(x: 14, y: 10, width: 140, height: 18)
        badge.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        badge.textColor = mutedColor
        view.addSubview(badge)

        panel.contentView = view
        panel.makeFirstResponder(view)
        panel.orderFrontRegardless()
        self.panel = panel
        self.rootView = view
        self.badge = badge
        signalReady()
    }

    func updateBadge() {
        let c = counts(summary)
        badge?.stringValue = "R \(min(c.0, 99))   W \(min(c.1, 99))"
    }

    func refreshSummary(render: Bool) {
        fetchSummary { [weak self] payload in
            guard let self = self else { return }
            if !payload.isEmpty {
                self.summary = payload
                self.updateBadge()
            }
            if render {
                self.renderPopover()
            }
        }
    }

    @objc func togglePopover() {
        if let pop = popoverPanel, pop.isVisible {
            pop.orderOut(nil)
            return
        }
        showPopover()
        refreshSummary(render: true)
    }

    @objc func closePopover() {
        popoverPanel?.orderOut(nil)
    }

    @objc func openDashboard() {
        if let url = normalizedURL("/goals") {
            NSWorkspace.shared.open(url)
        }
    }

    func showPopover() {
        let w: CGFloat = 380
        let h: CGFloat = 520
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 900, height: 700)
        let ballFrame = panel?.frame ?? NSRect(x: screen.maxX - 206, y: screen.minY + 82, width: 172, height: 58)
        var x = ballFrame.maxX - w
        x = min(max(x, screen.minX + 8), screen.maxX - w - 8)
        var y = ballFrame.maxY + 8
        if y + h > screen.maxY {
            y = ballFrame.minY - h - 8
        }
        y = min(max(y, screen.minY + 8), screen.maxY - h - 8)

        let pop = popoverPanel ?? NSPanel(
            contentRect: NSRect(x: x, y: y, width: w, height: h),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        pop.setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
        pop.level = .floating
        pop.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        pop.isOpaque = false
        pop.backgroundColor = .clear
        pop.hasShadow = true
        popoverPanel = pop
        renderPopover()
        pop.orderFrontRegardless()
    }

    func label(_ text: String, frame: NSRect, font: NSFont, color: NSColor) -> NSTextField {
        let f = NSTextField(wrappingLabelWithString: text)
        f.frame = frame
        f.font = font
        f.textColor = color
        f.backgroundColor = .clear
        return f
    }

    func renderPopover() {
        guard let pop = popoverPanel else { return }
        sleeves.removeAll()
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 380, height: 520))
        root.wantsLayer = true
        root.layer?.backgroundColor = panelColor.cgColor
        root.layer?.cornerRadius = 12
        root.layer?.borderWidth = 1
        root.layer?.borderColor = borderColor.cgColor

        let title = label("Attention Inbox", frame: NSRect(x: 14, y: 486, width: 220, height: 20), font: NSFont.boldSystemFont(ofSize: 14), color: textColor)
        root.addSubview(title)
        let c = counts(summary)
        let sub = label("R = running, W = waiting / review   R \(c.0)  W \(c.1)", frame: NSRect(x: 14, y: 466, width: 260, height: 18), font: NSFont.systemFont(ofSize: 11), color: mutedColor)
        root.addSubview(sub)
        let close = NSButton(title: "Close", target: self, action: #selector(closePopover))
        close.frame = NSRect(x: 286, y: 477, width: 80, height: 28)
        root.addSubview(close)
        let dashboard = NSButton(title: "Dashboard", target: self, action: #selector(openDashboard))
        dashboard.frame = NSRect(x: 286, y: 449, width: 80, height: 24)
        root.addSubview(dashboard)

        let running = bucketItems(summary, "running")
        let waiting = bucketItems(summary, "waiting")
        let next = bucketItems(summary, "next_move")
        let sections: [(String, [[String: Any]], String)] = [
            ("Running spaces", running, ""),
            ("Waiting / review", waiting, ""),
            ("NextMove recommendations", next, "no suggestion.")
        ]
        func cardHeight(_ item: [String: Any]) -> CGFloat {
            clean(item["summary"]).isEmpty ? 104 : 128
        }
        var contentHeight: CGFloat = 18
        for section in sections {
            contentHeight += 26
            if section.1.isEmpty {
                contentHeight += 30
            } else {
                for item in section.1 {
                    contentHeight += cardHeight(item) + 8
                }
            }
            contentHeight += 8
        }
        contentHeight = max(contentHeight, 444)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 380, height: 444))
        scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder
        scroll.backgroundColor = panelColor
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 360, height: contentHeight))
        content.wantsLayer = true
        content.layer?.backgroundColor = panelColor.cgColor
        var y = contentHeight - 18

        func addSectionTitle(_ text: String) {
            y -= 16
            let f = label(text, frame: NSRect(x: 14, y: y, width: 330, height: 16), font: NSFont.boldSystemFont(ofSize: 11), color: mutedColor)
            content.addSubview(f)
            y -= 8
        }

        func addEmpty(_ text: String) {
            y -= 22
            let f = label(text, frame: NSRect(x: 18, y: y, width: 318, height: 18), font: NSFont.systemFont(ofSize: 12), color: mutedColor)
            content.addSubview(f)
            y -= 8
        }

        func addButton(_ title: String, frame: NSRect, action: @escaping () -> Void, to view: NSView) {
            let sleeve = ClosureSleeve(action)
            sleeves.append(sleeve)
            let button = NSButton(title: title, target: sleeve, action: #selector(ClosureSleeve.invoke(_:)))
            button.frame = frame
            view.addSubview(button)
        }

        func addCard(_ item: [String: Any]) {
            let h = cardHeight(item)
            y -= h
            let card = NSView(frame: NSRect(x: 12, y: y, width: 336, height: h))
            card.wantsLayer = true
            card.layer?.backgroundColor = cardColor.cgColor
            card.layer?.cornerRadius = 8
            card.layer?.borderWidth = 1
            card.layer?.borderColor = borderColor.cgColor

            card.addSubview(label(itemTitle(item), frame: NSRect(x: 10, y: h - 30, width: 316, height: 18), font: NSFont.boldSystemFont(ofSize: 12), color: textColor))
            let agent = agentLabel(item)
            let goal = clean(item["goal_title"], maxLen: 80)
            let meta = [agent, goal].filter { !$0.isEmpty }.joined(separator: " / ")
            card.addSubview(label(meta.isEmpty ? "No goal" : meta, frame: NSRect(x: 10, y: h - 50, width: 316, height: 16), font: NSFont.systemFont(ofSize: 10), color: mutedColor))
            let duration = durationText(item)
            let state = duration.isEmpty ? stateLabel(item) : "\(stateLabel(item)) / \(duration)"
            card.addSubview(label(state, frame: NSRect(x: 10, y: h - 69, width: 316, height: 16), font: NSFont.systemFont(ofSize: 11), color: accentColor))
            let summaryText = clean(item["summary"], maxLen: 140)
            if !summaryText.isEmpty {
                card.addSubview(label(summaryText, frame: NSRect(x: 10, y: 34, width: 316, height: 32), font: NSFont.systemFont(ofSize: 11), color: mutedColor))
            }
            if let url = primaryURL(item) {
                addButton(primaryLabel(item), frame: NSRect(x: 10, y: 8, width: 128, height: 24), action: {
                    NSWorkspace.shared.open(url)
                }, to: card)
            }
            if let url = dismissURL(item) {
                addButton("Dismiss", frame: NSRect(x: 146, y: 8, width: 82, height: 24), action: { [weak self] in
                    postURL(url) {
                        self?.refreshSummary(render: true)
                    }
                }, to: card)
            }
            content.addSubview(card)
            y -= 8
        }

        for section in sections {
            addSectionTitle(section.0)
            if section.1.isEmpty {
                if !section.2.isEmpty {
                    addEmpty(section.2)
                }
            } else {
                for item in section.1 {
                    addCard(item)
                }
            }
            y -= 8
        }

        scroll.documentView = content
        content.scroll(NSPoint(x: 0, y: contentHeight - scroll.contentSize.height))
        root.addSubview(scroll)
        pop.contentView = root
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
"""


def _exec_swift_helper(args: argparse.Namespace, running: int, waiting: int) -> int:
    swift = shutil.which("swift")
    if not swift:
        return 2
    cache_dir = os.path.join(tempfile.gettempdir(), "openfocus-swift-module-cache")
    with contextlib.suppress(OSError):
        os.makedirs(cache_dir, exist_ok=True)
    os.environ.setdefault("CLANG_MODULE_CACHE_PATH", cache_dir)
    fd, path = tempfile.mkstemp(prefix="openfocus-float-ball-", suffix=".swift")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(SWIFT_HELPER)
    os.execv(
        swift,
        [
            swift,
            path,
            "--",
            str(args.browser_session_id or ""),
            str(args.openfocus_base_url or ""),
            str(running),
            str(waiting),
        ],
    )
    return 2


def _run_tk_helper(args: argparse.Namespace, summary: dict[str, Any]) -> int:
    try:
        import tkinter as tk
    except Exception:
        return 2

    base_url = str(args.openfocus_base_url or "").strip()
    state: dict[str, Any] = {
        "summary": summary,
        "popover": None,
        "refreshing": False,
        "drag_x": 0,
        "drag_y": 0,
        "drag_root_x": 0,
        "drag_root_y": 0,
        "drag_moved": False,
    }

    root = tk.Tk()
    root.title("OpenFocus Inbox")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg=FLOAT_BALL_BG)
    with contextlib.suppress(Exception):
        root.overrideredirect(True)

    width, height = 172, 58
    x = max(8, root.winfo_screenwidth() - width - 34)
    y = max(8, root.winfo_screenheight() - height - 90)
    root.geometry(f"{width}x{height}+{x}+{y}")

    frame = tk.Frame(
        root,
        bg=FLOAT_BALL_BG,
        padx=12,
        pady=8,
        highlightbackground=FLOAT_BALL_BORDER,
        highlightthickness=1,
    )
    frame.pack(fill="both", expand=True)
    title = tk.Label(
        frame,
        text="Inbox",
        fg=FLOAT_BALL_TEXT,
        bg=FLOAT_BALL_BG,
        font=("Helvetica", 13, "bold"),
    )
    title.grid(row=0, column=0, sticky="w")
    badge = tk.Label(
        frame,
        text="",
        fg=FLOAT_BALL_MUTED,
        bg=FLOAT_BALL_BG,
        font=("Menlo", 11),
    )
    badge.grid(row=1, column=0, sticky="w")

    def popover_exists() -> bool:
        pop = state.get("popover")
        try:
            return bool(pop is not None and pop.winfo_exists())
        except Exception:
            return False

    def update_badge() -> None:
        running, waiting = _counts(_as_dict(state.get("summary")))
        badge.configure(text=f"R {min(running, 99)}   W {min(waiting, 99)}")

    def close_popover() -> None:
        pop = state.get("popover")
        if pop is not None:
            try:
                pop.destroy()
            except Exception:
                pass
        state["popover"] = None

    def open_url(url: str) -> None:
        if url:
            webbrowser.open(url, new=2)

    def open_dashboard() -> None:
        open_url(_absolute_url(base_url, "/goals"))

    def render_item(parent: Any, item: dict[str, Any]) -> None:
        card = tk.Frame(
            parent,
            bg=FLOAT_BALL_CARD_BG,
            padx=10,
            pady=8,
            highlightbackground=FLOAT_BALL_BORDER,
            highlightthickness=1,
        )
        card.pack(fill="x", pady=(0, 8))
        title_label = tk.Label(
            card,
            text=_item_title(item),
            fg=FLOAT_BALL_TEXT,
            bg=FLOAT_BALL_CARD_BG,
            font=("Helvetica", 12, "bold"),
            justify="left",
            anchor="w",
            wraplength=314,
        )
        title_label.pack(fill="x", anchor="w")
        meta_bits = [x for x in (_agent_label(item), _clean_text(item.get("goal_title"), max_len=80)) if x]
        meta_label = tk.Label(
            card,
            text=" / ".join(meta_bits) if meta_bits else "No goal",
            fg=FLOAT_BALL_MUTED,
            bg=FLOAT_BALL_CARD_BG,
            font=("Helvetica", 10),
            justify="left",
            anchor="w",
            wraplength=314,
        )
        meta_label.pack(fill="x", anchor="w", pady=(3, 0))
        duration = _duration_text(item)
        state_label = tk.Label(
            card,
            text=f"{_state_label(item)}{(' / ' + duration) if duration else ''}",
            fg=FLOAT_BALL_ACCENT,
            bg=FLOAT_BALL_CARD_BG,
            font=("Helvetica", 10),
            justify="left",
            anchor="w",
        )
        state_label.pack(fill="x", anchor="w", pady=(3, 0))
        summary_text = _clean_text(item.get("summary"), max_len=160)
        if summary_text:
            tk.Label(
                card,
                text=summary_text,
                fg=FLOAT_BALL_MUTED,
                bg=FLOAT_BALL_CARD_BG,
                font=("Helvetica", 11),
                justify="left",
                anchor="w",
                wraplength=314,
            ).pack(fill="x", anchor="w", pady=(5, 0))

        actions = tk.Frame(card, bg=FLOAT_BALL_CARD_BG)
        actions.pack(fill="x", pady=(8, 0), anchor="w")
        action_label, action_path = _primary_action(item)
        action_url = _absolute_url(base_url, action_path)
        if action_url:
            tk.Button(
                actions,
                text=action_label,
                command=lambda url=action_url: open_url(url),
                bg=FLOAT_BALL_ACCENT,
                fg="#04130d",
                activebackground="#6ee7b7",
                relief="flat",
                padx=8,
                pady=3,
            ).pack(side="left")
            for widget in (card, title_label, meta_label, state_label):
                widget.bind("<Double-Button-1>", lambda _event, url=action_url: open_url(url))
        dismiss_path = _dismiss_path(item)
        dismiss_url = _absolute_url(base_url, dismiss_path)
        if dismiss_url:
            tk.Button(
                actions,
                text="Dismiss",
                command=lambda url=dismiss_url: dismiss_item(url),
                bg=FLOAT_BALL_CARD_BG,
                fg=FLOAT_BALL_MUTED,
                activebackground=FLOAT_BALL_BORDER,
                activeforeground=FLOAT_BALL_TEXT,
                relief="groove",
                padx=8,
                pady=3,
            ).pack(side="left", padx=(6, 0))

    def render_section(parent: Any, label: str, items: list[dict[str, Any]], empty: str = "") -> None:
        section = tk.Frame(parent, bg=FLOAT_BALL_PANEL_BG)
        section.pack(fill="x", pady=(0, 10))
        tk.Label(
            section,
            text=label,
            fg=FLOAT_BALL_MUTED,
            bg=FLOAT_BALL_PANEL_BG,
            font=("Helvetica", 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        if not items and empty:
            tk.Label(
                section,
                text=empty,
                fg=FLOAT_BALL_MUTED,
                bg=FLOAT_BALL_PANEL_BG,
                font=("Helvetica", 11),
                anchor="w",
            ).pack(fill="x", pady=(5, 0))
        for item in items:
            render_item(section, item)

    def render_popover() -> None:
        if not popover_exists():
            return
        pop = state["popover"]
        for child in pop.winfo_children():
            child.destroy()

        header = tk.Frame(pop, bg=FLOAT_BALL_PANEL_BG, padx=12, pady=10)
        header.pack(fill="x")
        tk.Label(
            header,
            text="Attention Inbox",
            fg=FLOAT_BALL_TEXT,
            bg=FLOAT_BALL_PANEL_BG,
            font=("Helvetica", 14, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        running, waiting = _counts(_as_dict(state.get("summary")))
        tk.Label(
            header,
            text=f"R = running, W = waiting / review   R {running}  W {waiting}",
            fg=FLOAT_BALL_MUTED,
            bg=FLOAT_BALL_PANEL_BG,
            font=("Helvetica", 10),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        tk.Button(
            header,
            text="Close",
            command=close_popover,
            bg=FLOAT_BALL_CARD_BG,
            fg=FLOAT_BALL_TEXT,
            activebackground=FLOAT_BALL_BORDER,
            activeforeground=FLOAT_BALL_TEXT,
            relief="flat",
            padx=8,
        ).grid(row=0, column=1, sticky="e", padx=(10, 0))
        tk.Button(
            header,
            text="Dashboard",
            command=open_dashboard,
            bg=FLOAT_BALL_CARD_BG,
            fg=FLOAT_BALL_TEXT,
            activebackground=FLOAT_BALL_BORDER,
            activeforeground=FLOAT_BALL_TEXT,
            relief="flat",
            padx=8,
        ).grid(row=1, column=1, sticky="e", padx=(10, 0), pady=(4, 0))
        header.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(pop, bg=FLOAT_BALL_PANEL_BG, highlightthickness=0, width=380, height=444)
        scrollbar = tk.Scrollbar(pop, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=FLOAT_BALL_PANEL_BG, padx=12, pady=4)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        sections = _section_items(_as_dict(state.get("summary")))
        render_section(body, "Running spaces", sections["running"])
        render_section(body, "Waiting / review", sections["waiting"])
        render_section(body, "NextMove recommendations", sections["next_move"], "no suggestion.")

    def refresh_async(*, render: bool) -> None:
        if state.get("refreshing"):
            return
        state["refreshing"] = True

        def worker() -> None:
            payload = _fetch_summary(base_url)

            def apply() -> None:
                state["refreshing"] = False
                if payload:
                    state["summary"] = payload
                    update_badge()
                if render and popover_exists():
                    render_popover()

            try:
                root.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def dismiss_item(url: str) -> None:
        def worker() -> None:
            _post_url(url)
            payload = _fetch_summary(base_url)

            def apply() -> None:
                if payload:
                    state["summary"] = payload
                    update_badge()
                if popover_exists():
                    render_popover()

            try:
                root.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def toggle_popover(_event=None) -> None:
        if popover_exists():
            close_popover()
            return
        pop = tk.Toplevel(root)
        pop.title("OpenFocus Attention Inbox")
        pop.configure(bg=FLOAT_BALL_PANEL_BG)
        pop.attributes("-topmost", True)
        pop.resizable(False, True)
        state["popover"] = pop
        root.update_idletasks()
        panel_w, panel_h = 380, 520
        rx, ry = root.winfo_x(), root.winfo_y()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        px = max(8, min(rx + width - panel_w, screen_w - panel_w - 8))
        py = ry + height + 8
        if py + panel_h > screen_h - 8:
            py = max(8, ry - panel_h - 8)
        pop.geometry(f"{panel_w}x{panel_h}+{px}+{py}")
        render_popover()
        refresh_async(render=True)

    def schedule_refresh() -> None:
        refresh_async(render=popover_exists())
        root.after(15000, schedule_refresh)

    def start_drag(event: Any) -> None:
        state["drag_x"] = event.x
        state["drag_y"] = event.y
        state["drag_root_x"] = event.x_root
        state["drag_root_y"] = event.y_root
        state["drag_moved"] = False

    def drag(event: Any) -> None:
        if (
            abs(event.x_root - int(state.get("drag_root_x") or 0)) < 4
            and abs(event.y_root - int(state.get("drag_root_y") or 0)) < 4
        ):
            return
        state["drag_moved"] = True
        nx = max(0, event.x_root - int(state.get("drag_x") or 0))
        ny = max(0, event.y_root - int(state.get("drag_y") or 0))
        root.geometry(f"+{nx}+{ny}")
        if popover_exists():
            close_popover()

    def finish_click(event: Any) -> None:
        if state.get("drag_moved"):
            state["drag_moved"] = False
            return
        toggle_popover(event)

    for widget in (root, frame, title, badge):
        widget.bind("<Button-1>", start_drag)
        widget.bind("<B1-Motion>", drag)
        widget.bind("<ButtonRelease-1>", finish_click)

    update_badge()
    root.update_idletasks()
    root.lift()
    _signal_ready()
    root.after(15000, schedule_refresh)
    root.mainloop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-session-id", required=True)
    parser.add_argument("--openfocus-base-url", required=True)
    args = parser.parse_args()

    summary = _summary()
    running, waiting = _counts(summary)
    backend = str(os.environ.get("OPENFOCUS_FLOAT_BALL_BACKEND") or "").strip().lower()
    if backend == "swift":
        return _exec_swift_helper(args, running, waiting)
    return _run_tk_helper(args, summary)


if __name__ == "__main__":
    raise SystemExit(main())
