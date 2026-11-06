import AppKit

extension WorkspaceController {
  /// The preview owns one persistent control surface. Inspector rebuilds never
  /// detach these controls, and comparison panes share the same footer bounds.
  func makePreviewFooter() -> NSView {
    let footer = NSView()
    let rule = separator()
    rule.translatesAutoresizingMaskIntoConstraints = false
    footer.addSubview(rule)
    NSLayoutConstraint.activate([
      rule.leadingAnchor.constraint(equalTo: footer.leadingAnchor),
      rule.trailingAnchor.constraint(equalTo: footer.trailingAnchor),
      rule.topAnchor.constraint(equalTo: footer.topAnchor),
    ])
    resolutionPopup.removeAllItems()
    resolutionPopup.addItems(withTitles: [
      L10n.tr("display.native", "Native · 100%"),
      L10n.tr("display.balanced", "Balanced · 75%"),
      L10n.tr("display.interactive", "Interactive · 50%"),
    ])
    resolutionPopup.controlSize = .mini
    resolutionPopup.target = self
    resolutionPopup.action = #selector(resolutionChanged(_:))
    resolutionPopup.setAccessibilityLabel(L10n.tr("display.quality", "Viewport quality"))
    resolutionPopup.setAccessibilityIdentifier("studio.preview.quality")
    resolutionPopup.toolTip = L10n.tr(
      "display.pixelNote",
      "Resolution changes pixels, never identity sampling. Image exports use the chosen resolution.")
    resolutionPopup.widthAnchor.constraint(equalToConstant: 145).isActive = true
    configurePreviewToggle(gridButton, symbol: "grid", title: nil,
      help: L10n.tr("display.grid", "Coordinate grid"), action: #selector(gridChanged(_:)),
      identifier: "grid", on: renderOptions.showGrid)
    configurePreviewToggle(camerasButton, symbol: "camera", title: nil,
      help: L10n.tr("preview.cameraObjects", "Camera wireframes · paths appear only for selected cameras"),
      action: #selector(camerasChanged(_:)), identifier: "cameras", on: true)
    configurePreviewToggle(trajectoryButton, symbol: "point.topleft.down.curvedto.point.bottomright.up",
      title: L10n.tr("preview.paths", "Paths"),
      help: L10n.tr("preview.pathsHint", "Show complete paths for selected objects and identities"),
      action: #selector(trajectoryChanged(_:)), identifier: "paths", on: trajectoryOptions.showPaths)
    configurePreviewToggle(trailButton, symbol: "sparkle", title: L10n.tr("preview.trails", "Trails"),
      help: L10n.tr("preview.trailsHint", "Emphasize recent motion ending at the current time"),
      action: #selector(trailsChanged(_:)), identifier: "trails", on: trajectoryOptions.showTrails)
    let row = stack([
      label(L10n.tr("preview.label", "Preview"), size: 10, secondary: true),
      resolutionPopup, gridButton, camerasButton, trajectoryButton, trailButton, previewSpacer(),
    ], vertical: false, spacing: 5)
    let firstSettingSpacer = previewSpacer(), secondSettingSpacer = previewSpacer()
    let settings = stack([
      previewNumberControl(
        title: L10n.tr("preview.pathOpacity", "Path opacity"), unit: "%", field: pathOpacityField,
        stepper: pathOpacityStepper, minimum: 0, maximum: 100, value: 18,
        action: #selector(pathOpacityChanged(_:)), identifier: "pathOpacity"),
      firstSettingSpacer,
      previewNumberControl(
        title: L10n.tr("preview.trailFrames", "Trail frames"), unit: nil, field: trailFramesField,
        stepper: trailFramesStepper, minimum: 1, maximum: 4096, value: 8,
        action: #selector(trailFramesChanged(_:)), identifier: "trailFrames"),
      secondSettingSpacer,
      previewNumberControl(
        title: L10n.tr("preview.pathDensity", "Path density"), unit: "%", field: trajectoryDensityField,
        stepper: trajectoryDensityStepper, minimum: 1, maximum: 100, value: 100,
        action: #selector(trajectoryDensityChanged(_:)), identifier: "pathDensity"),
    ], vertical: false, spacing: 4)
    firstSettingSpacer.widthAnchor.constraint(equalTo: secondSettingSpacer.widthAnchor).isActive = true
    settings.toolTip = L10n.tr("preview.densityHint",
      "Density controls trajectories only. All Gaussians remain rendered; explicitly selected identities keep their paths.")
    coordinateCaption.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular)
    coordinateCaption.textColor = .secondaryLabelColor
    coordinateCaption.lineBreakMode = .byTruncatingTail
    coordinateCaption.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    let context = stack([coordinateCaption, previewSpacer(), compareButton], vertical: false, spacing: 6)
    let content = stack([row, settings, context], spacing: 3)
    content.alignment = .leading
    [row, settings, context].forEach {
      $0.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
    }
    pin(content, in: footer, inset: 6)
    updatePreviewFooter()
    return footer
  }
  func previewSpacer() -> NSView {
    let view = NSView()
    view.setContentHuggingPriority(.init(1), for: .horizontal)
    return view
  }
  func configurePreviewToggle(_ control: NSButton, symbol: String, title: String?, help: String,
    action: Selector, identifier: String, on: Bool) {
    control.title = title ?? ""
    control.image = NSImage(systemSymbolName: symbol, accessibilityDescription: help)
    control.imagePosition = title == nil ? .imageOnly : .imageLeading
    control.bezelStyle = .texturedRounded
    control.setButtonType(.pushOnPushOff)
    control.controlSize = .mini
    control.font = .systemFont(ofSize: 11)
    control.state = on ? .on : .off
    control.target = self
    control.action = action
    control.toolTip = help
    control.setAccessibilityLabel(help)
    control.setAccessibilityIdentifier("studio.preview." + identifier)
  }
  func previewNumberControl(title: String, unit: String?, field: NSTextField, stepper: NSStepper,
    minimum: Double, maximum: Double, value: Double, action: Selector, identifier: String) -> NSView {
    let caption = label(title, size: 10, secondary: true)
    configureField(field, label: title, width: 43)
    field.controlSize = .mini
    field.alignment = .right
    field.target = self
    field.action = action
    field.cell?.sendsActionOnEndEditing = true
    field.setAccessibilityIdentifier("studio.preview." + identifier)
    stepper.minValue = minimum
    stepper.maxValue = maximum
    stepper.increment = 1
    stepper.doubleValue = value
    stepper.autorepeat = true
    stepper.valueWraps = false
    stepper.controlSize = .mini
    stepper.target = self
    stepper.action = action
    stepper.setAccessibilityLabel(title)
    stepper.setAccessibilityIdentifier("studio.preview." + identifier + ".stepper")
    let group = stack([caption, field, stepper], vertical: false, spacing: 3)
    if let unit { group.addArrangedSubview(label(unit, size: 10, secondary: true)) }
    return group
  }
  func updatePreviewFooter() {
    trajectoryButton.state = trajectoryOptions.showPaths ? .on : .off
    trailButton.state = trajectoryOptions.showTrails ? .on : .off
    if pathOpacityField.currentEditor() == nil {
      pathOpacityField.stringValue = L10n.number(Double(trajectoryOptions.pathOpacity * 100), digits: 1)
    }
    if trailFramesField.currentEditor() == nil { trailFramesField.stringValue = String(trajectoryOptions.trailFrames) }
    if trajectoryDensityField.currentEditor() == nil { trajectoryDensityField.stringValue = String(trajectoryOptions.densityPercent) }
    pathOpacityField.toolTip = L10n.tr("preview.opacityRange", "Complete-path opacity · 0–100%")
    trailFramesField.toolTip = L10n.tr("preview.trailRange", "Recent source-frame intervals · 1–4096")
    trajectoryDensityField.toolTip = L10n.tr("preview.densityHint",
      "Density controls trajectories only. All Gaussians remain rendered; explicitly selected identities keep their paths.")
    pathOpacityStepper.doubleValue = Double(trajectoryOptions.pathOpacity * 100)
    trailFramesStepper.integerValue = trajectoryOptions.trailFrames
    trajectoryDensityStepper.integerValue = trajectoryOptions.densityPercent
    // Scientific comparison deliberately excludes decorative preview overlays.
    [resolutionPopup, gridButton, camerasButton, trajectoryButton, trailButton,
      pathOpacityField, pathOpacityStepper, trailFramesField, trailFramesStepper,
      trajectoryDensityField, trajectoryDensityStepper].forEach { $0.isEnabled = !comparisonActive }
  }
  func publishTrajectoryOptions() {
    updatePreviewFooter()
    actions.onTrajectoryOptionsChanged?(trajectoryOptions)
  }
  @objc func trailsChanged(_ sender: Any?) {
    trajectoryOptions.showTrails = trailButton.state == .on
    publishTrajectoryOptions()
  }
  private func previewNumber(_ sender: Any?, field: NSTextField, stepper: NSStepper,
    minimum: Double, maximum: Double, integer: Bool) -> Double? {
    let value = sender as? NSStepper === stepper ? stepper.doubleValue : L10n.parse(field.stringValue)
    guard let value, value.isFinite, value >= minimum, value <= maximum,
      !integer || value.rounded() == value else {
      setStatus(L10n.tr("preview.invalidSetting", "Enter a value within the preview control's range."))
      updatePreviewFooter()
      return nil
    }
    return value
  }
  @objc func pathOpacityChanged(_ sender: Any?) {
    guard let value = previewNumber(sender, field: pathOpacityField, stepper: pathOpacityStepper,
      minimum: 0, maximum: 100, integer: false) else { return }
    trajectoryOptions.pathOpacity = Float(value / 100)
    publishTrajectoryOptions()
  }
  @objc func trailFramesChanged(_ sender: Any?) {
    guard let value = previewNumber(sender, field: trailFramesField, stepper: trailFramesStepper,
      minimum: 1, maximum: 4096, integer: true) else { return }
    trajectoryOptions.trailFrames = Int(value)
    publishTrajectoryOptions()
  }
  @objc func trajectoryDensityChanged(_ sender: Any?) {
    guard let value = previewNumber(sender, field: trajectoryDensityField, stepper: trajectoryDensityStepper,
      minimum: 1, maximum: 100, integer: true) else { return }
    trajectoryOptions.densityPercent = Int(value)
    publishTrajectoryOptions()
  }
}
