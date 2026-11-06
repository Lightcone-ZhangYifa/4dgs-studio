import AppKit

@main
final class NativeViewerApplication: NSObject, NSApplicationDelegate {
  private var workspace: WorkspaceController?
  private var documentController: NativeDocumentController?
  private var pendingURLs: [URL] = []

  static func main() {
    let application = NSApplication.shared
    let delegate = NativeViewerApplication()
    application.delegate = delegate
    application.setActivationPolicy(.regular)
    withExtendedLifetime(delegate) { application.run() }
  }

  func applicationDidFinishLaunching(_ notification: Notification) {
    NSWindow.allowsAutomaticWindowTabbing = false
    let workspace = WorkspaceController()
    self.workspace = workspace
    documentController = NativeDocumentController(workspace: workspace)
    buildMenus(workspace)
    workspace.showWindow(nil)
    NSApp.activate(ignoringOtherApps: true)
    if !pendingURLs.isEmpty {
      workspace.actions.onOpenURLs?(pendingURLs)
      pendingURLs.removeAll()
    } else {
      // NSUserDefaults launch arguments include values such as "zh-Hans";
      // only scene paths are document-open arguments.
      let arguments = CommandLine.arguments.dropFirst().filter {
        !$0.hasPrefix("-") && URL(fileURLWithPath: $0).pathExtension.lowercased() == "gsv"
      }
      if let path = arguments.first { workspace.actions.onOpenURLs?([URL(fileURLWithPath: path)]) }
    }
  }

  func application(_ application: NSApplication, open urls: [URL]) {
    guard let workspace else {
      pendingURLs.append(contentsOf: urls)
      return
    }
    workspace.showWindow(nil)
    workspace.actions.onOpenURLs?(urls)
  }

  func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool
  {
    if !flag { workspace?.showWindow(nil) }
    return true
  }
  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
  func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    documentController?.canClose() == false ? .terminateCancel : .terminateNow
  }
  func applicationWillTerminate(_ notification: Notification) {
    documentController?.shutdown()
  }

  private func buildMenus(_ workspace: WorkspaceController) {
    let menu = NSMenu()
    func submenu(_ title: String) -> NSMenu {
      let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
      let child = NSMenu(title: title)
      item.submenu = child
      menu.addItem(item)
      return child
    }
    func add(
      _ menu: NSMenu, _ title: String, _ action: Selector, _ key: String = "",
      modifiers: NSEvent.ModifierFlags = [.command], target: AnyObject? = nil
    ) {
      let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
      item.keyEquivalentModifierMask = modifiers
      item.target = target ?? workspace
      menu.addItem(item)
    }
    let application = submenu(AppIdentity().name)
    add(
      application, L10n.tr("menu.about", "About 4DGS Studio"),
      #selector(showAbout(_:)), target: self)
    application.addItem(.separator())
    let languageItem = NSMenuItem(
      title: L10n.tr("language.title", "Language"), action: nil, keyEquivalent: "")
    let languageMenu = NSMenu(title: languageItem.title)
    for (code, title) in [
      ("system", L10n.tr("language.system", "Follow System")), ("en", "English"),
      ("zh-Hans", "简体中文"),
    ] {
      let item = NSMenuItem(title: title, action: #selector(changeLanguage(_:)), keyEquivalent: "")
      item.target = self
      item.representedObject = code
      item.state = L10n.preferredLanguage == code ? .on : .off
      languageMenu.addItem(item)
    }
    languageItem.submenu = languageMenu
    application.addItem(languageItem)
    application.addItem(.separator())
    add(
      application, L10n.tr("menu.hide", "Hide 4DGS Studio"), #selector(NSApplication.hide(_:)), "h",
      target: NSApp)
    add(
      application, L10n.tr("menu.hideOthers", "Hide Others"),
      #selector(NSApplication.hideOtherApplications(_:)), "h", modifiers: [.command, .option],
      target: NSApp)
    add(
      application, L10n.tr("menu.showAll", "Show All"),
      #selector(NSApplication.unhideAllApplications(_:)), target: NSApp)
    application.addItem(.separator())
    add(
      application, L10n.tr("menu.quit", "Quit 4DGS Studio"), #selector(NSApplication.terminate(_:)),
      "q", target: NSApp)

    let file = submenu(L10n.tr("menu.file", "File"))
    add(
      file, L10n.tr("file.open", "Open Scene…"), #selector(WorkspaceController.openDocument(_:)),
      "o")
    add(
      file, L10n.tr("file.screenshot", "Save Screenshot…"),
      #selector(WorkspaceController.exportScreenshot(_:)), "s", modifiers: [.command, .shift])
    add(
      file, L10n.tr("file.workspaceSnapshot", "Save Workspace Snapshot…"),
      #selector(WorkspaceController.exportWorkspace(_:)), "s",
      modifiers: [.command, .option, .shift])
    file.addItem(.separator())
    add(
      file, L10n.tr("menu.closeWindow", "Close Window"), #selector(NSWindow.performClose(_:)), "w",
      target: workspace.window)

    let edit = submenu(L10n.tr("menu.edit", "Edit"))
    for (title, selector, key) in [
      (L10n.tr("edit.undo", "Undo"), "undo:", "z"), (L10n.tr("edit.redo", "Redo"), "redo:", "Z"),
      (L10n.tr("edit.cut", "Cut"), "cut:", "x"), (L10n.tr("edit.copy", "Copy"), "copy:", "c"),
      (L10n.tr("edit.paste", "Paste"), "paste:", "v"),
      (L10n.tr("edit.selectAll", "Select All"), "selectAll:", "a"),
    ] {
      let item = NSMenuItem(title: title, action: Selector(selector), keyEquivalent: key)
      edit.addItem(item)  // First-responder chain preserves native text editing.
    }

    let view = submenu(L10n.tr("menu.view", "View"))
    add(
      view, L10n.tr("view.frameScene", "Frame Scene"),
      #selector(WorkspaceController.resetCamera(_:)), "r")
    add(
      view, L10n.tr("view.focusSelection", "Focus Selection"),
      #selector(WorkspaceController.focusSelection(_:)), "f")
    view.addItem(.separator())
    add(view, L10n.tr("view.top", "Top"), #selector(WorkspaceController.cameraTop(_:)))
    add(view, L10n.tr("view.front", "Front"), #selector(WorkspaceController.cameraFront(_:)))
    add(view, L10n.tr("view.right", "Right"), #selector(WorkspaceController.cameraRight(_:)))
    add(
      view, L10n.tr("view.isometric", "Isometric"),
      #selector(WorkspaceController.cameraIsometric(_:)))
    view.addItem(.separator())
    add(
      view, L10n.tr("view.toggleOutliner", "Toggle Scene Sidebar"),
      #selector(WorkspaceController.toggleScenePane(_:)), "1")
    add(
      view, L10n.tr("view.toggleInspector", "Toggle Inspector"),
      #selector(WorkspaceController.toggleInspector(_:)), "2")
    add(
      view, L10n.tr("view.fullscreen", "Enter Full Screen"),
      #selector(NSWindow.toggleFullScreen(_:)), "f", modifiers: [.command, .control],
      target: workspace.window)

    add(
      view, L10n.tr("view.previous", "Previous View"),
      #selector(WorkspaceController.previousView(_:)), "[", modifiers: [.command, .option])
    add(
      view, L10n.tr("view.next", "Next View"), #selector(WorkspaceController.nextView(_:)), "]",
      modifiers: [.command, .option])

    let cameras = submenu(L10n.tr("menu.camera", "Camera"))
    add(
      cameras, L10n.tr("camera.createFromView", "Create Camera from View"),
      #selector(WorkspaceController.createCamera(_:)), "k", modifiers: [.command, .shift])
    add(
      cameras, L10n.tr("camera.recordAtTime", "Record Keyframe at Current Time"),
      #selector(WorkspaceController.recordCameraKeyframe(_:)), "k")
    add(
      cameras, L10n.tr("camera.saveAnimation", "Save Camera Animation…"),
      #selector(WorkspaceController.saveCameras(_:)))
    add(
      cameras, L10n.tr("camera.importAnimation", "Import Camera Animation…"),
      #selector(WorkspaceController.importCameras(_:)))
    cameras.addItem(.separator())
    add(
      cameras, L10n.tr("camera.undo", "Undo Camera Edit"),
      #selector(WorkspaceController.undoCamera(_:)), "z", modifiers: [.command, .option])
    add(
      cameras, L10n.tr("camera.redo", "Redo Camera Edit"),
      #selector(WorkspaceController.redoCamera(_:)), "z", modifiers: [.command, .option, .shift])

    let playback = submenu(L10n.tr("menu.playback", "Playback"))
    add(
      playback, L10n.tr("timeline.playPause", "Play / Pause"),
      #selector(WorkspaceController.togglePlayback(_:)))
    add(
      playback, L10n.tr("timeline.previousFrame", "Previous Frame"),
      #selector(WorkspaceController.previousFrame(_:)))
    add(
      playback, L10n.tr("timeline.nextFrame", "Next Frame"),
      #selector(WorkspaceController.nextFrame(_:)))
    add(
      playback, L10n.tr("timeline.firstFrame", "First Frame"),
      #selector(WorkspaceController.firstFrame(_:)))
    add(
      playback, L10n.tr("timeline.lastFrame", "Last Frame"),
      #selector(WorkspaceController.lastFrame(_:)))

    let window = submenu(L10n.tr("menu.window", "Window"))
    add(
      window, L10n.tr("window.minimize", "Minimize"), #selector(NSWindow.performMiniaturize(_:)),
      "m", target: workspace.window)
    add(
      window, L10n.tr("window.zoom", "Zoom"), #selector(NSWindow.performZoom(_:)),
      target: workspace.window)
    let help = submenu(L10n.tr("menu.help", "Help"))
    add(
      help, L10n.tr("nav.help", "Navigation guide"),
      #selector(WorkspaceController.showNavigationHelp(_:)), "?", modifiers: [.command])
    if AppIdentity().authorProfileURL != nil {
      help.addItem(.separator())
      add(help, L10n.tr("about.github", "GitHub"),
        #selector(openAuthorProfile(_:)), target: self)
    }
    NSApp.helpMenu = help
    NSApp.windowsMenu = window
    NSApp.mainMenu = menu
  }
  @objc private func showAbout(_ sender: Any?) {
    NSApp.orderFrontStandardAboutPanel(options: AppIdentity().aboutPanelOptions(icon: NSApp.applicationIconImage))
  }
  @objc private func openAuthorProfile(_ sender: Any?) {
    guard let url = AppIdentity().authorProfileURL else { return }
    NSWorkspace.shared.open(url)
  }
  @objc private func changeLanguage(_ sender: NSMenuItem) {
    guard let code = sender.representedObject as? String else { return }
    L10n.setPreferredLanguage(code)
    for item in sender.menu?.items ?? [] {
      item.state = (item.representedObject as? String) == code ? .on : .off
    }
    let alert = NSAlert()
    alert.messageText = L10n.tr("language.restartTitle", "Language preference saved")
    alert.informativeText = L10n.tr(
      "language.restartMessage",
      "The new language will be used the next time 4DGS Studio opens. Your current scene and unsaved camera animation stay open."
    )
    alert.addButton(withTitle: L10n.tr("common.ok", "OK"))
    if let window = workspace?.window {
      alert.beginSheetModal(for: window)
    } else {
      alert.runModal()
    }
  }

}
