import AppKit
import simd

extension WorkspaceController {
  func makeInspector() {
    selectionTitle.font = .systemFont(ofSize: 14, weight: .semibold)
    selectionTitle.lineBreakMode = .byTruncatingMiddle
    selectionTitle.isSelectable = true
    selectionSubtitle.font = .systemFont(ofSize: 10)
    selectionSubtitle.textColor = .secondaryLabelColor
    selectionSubtitle.maximumNumberOfLines = 3
    infoLine.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular)
    infoLine.textColor = .secondaryLabelColor
    infoLine.maximumNumberOfLines = 3
    infoLine.isSelectable = true
    let heading = stack(
      [
        label(
          L10n.tr("inspector.title", "INSPECTOR"), size: 10, weight: .semibold, secondary: true),
        selectionTitle, selectionSubtitle, infoLine,
      ], spacing: 6)
    inspectorTabs.segmentCount = 3
    inspectorTabs.trackingMode = .selectOne
    inspectorTabs.selectedSegment = 0
    inspectorTabs.controlSize = .small
    inspectorTabs.target = self
    inspectorTabs.action = #selector(inspectorTabChanged(_:))
    for (i, title) in [
      L10n.tr("inspector.selection", "Selection"), L10n.tr("inspector.viewport", "Viewport"),
      L10n.tr("inspector.document", "Document"),
    ].enumerated() { inspectorTabs.setLabel(title, forSegment: i) }
    inspectorScroll.documentView = inspectorBody
    inspectorScroll.hasVerticalScroller = true
    inspectorScroll.drawsBackground = false
    inspectorScroll.contentView.postsBoundsChangedNotifications = true
    NotificationCenter.default.addObserver(
      self, selector: #selector(inspectorDidScroll(_:)),
      name: NSView.boundsDidChangeNotification, object: inspectorScroll.contentView)
    inspectorBody.translatesAutoresizingMaskIntoConstraints = false
    inspectorStack.orientation = .vertical
    inspectorStack.alignment = .leading
    inspectorStack.spacing = 18
    inspectorStack.translatesAutoresizingMaskIntoConstraints = false
    inspectorBody.addSubview(inspectorStack)
    [heading, inspectorTabs, inspectorScroll].forEach {
      $0.translatesAutoresizingMaskIntoConstraints = false
      rightPane.addSubview($0)
    }
    NSLayoutConstraint.activate([
      heading.leadingAnchor.constraint(equalTo: rightPane.leadingAnchor, constant: 16),
      heading.trailingAnchor.constraint(equalTo: rightPane.trailingAnchor, constant: -16),
      heading.topAnchor.constraint(equalTo: rightPane.topAnchor, constant: 14),
      selectionSubtitle.widthAnchor.constraint(equalTo: heading.widthAnchor),
      infoLine.widthAnchor.constraint(equalTo: heading.widthAnchor),
      inspectorTabs.leadingAnchor.constraint(equalTo: heading.leadingAnchor),
      inspectorTabs.trailingAnchor.constraint(equalTo: heading.trailingAnchor),
      inspectorTabs.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 12),
      inspectorScroll.topAnchor.constraint(equalTo: inspectorTabs.bottomAnchor, constant: 12),
      inspectorScroll.leadingAnchor.constraint(equalTo: rightPane.leadingAnchor),
      inspectorScroll.trailingAnchor.constraint(equalTo: rightPane.trailingAnchor),
      inspectorScroll.bottomAnchor.constraint(equalTo: rightPane.bottomAnchor),
      inspectorBody.leadingAnchor.constraint(equalTo: inspectorScroll.contentView.leadingAnchor),
      inspectorBody.trailingAnchor.constraint(equalTo: inspectorScroll.contentView.trailingAnchor),
      inspectorBody.topAnchor.constraint(equalTo: inspectorScroll.contentView.topAnchor),
      inspectorStack.leadingAnchor.constraint(equalTo: inspectorBody.leadingAnchor, constant: 16),
      inspectorStack.trailingAnchor.constraint(
        equalTo: inspectorBody.trailingAnchor, constant: -16),
      inspectorStack.topAnchor.constraint(equalTo: inspectorBody.topAnchor, constant: 4),
      inspectorStack.bottomAnchor.constraint(equalTo: inspectorBody.bottomAnchor, constant: -20),
    ])
    rebuildInspector()
  }
  func inspectorNote(_ text: String) -> NSTextField {
    let v = NSTextField(wrappingLabelWithString: text)
    v.font = .systemFont(ofSize: 11)
    v.textColor = .secondaryLabelColor
    v.isSelectable = true
    return v
  }
  func section(_ title: String, _ views: [NSView]) {
    let heading = label(title, size: 11, weight: .semibold)
    let body = stack([heading] + views, spacing: 8)
    inspectorStack.addArrangedSubview(body)
    body.widthAnchor.constraint(equalTo: inspectorStack.widthAnchor).isActive = true
    for view in views {
      if view is NSTextField || view is NSStackView {
        view.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
      }
    }
  }
  func property(_ key: String, _ title: String, value: String = "—", multiline: Bool = false)
    -> NSView
  {
    let name = label(title, size: 10, secondary: true)
    if !multiline { name.widthAnchor.constraint(equalToConstant: 88).isActive = true }
    let field =
      multiline ? NSTextField(wrappingLabelWithString: value) : NSTextField(labelWithString: value)
    field.font =
      multiline
      ? .monospacedSystemFont(ofSize: 10, weight: .regular)
      : .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
    field.isSelectable = true
    field.lineBreakMode = multiline ? .byWordWrapping : .byTruncatingMiddle
    field.toolTip = value
    field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    inspectorValues[key] = field
    let row = stack([name, field], vertical: multiline, spacing: multiline ? 5 : 10)
    if multiline {
      field.widthAnchor.constraint(equalTo: row.widthAnchor).isActive = true
    } else {
      row.alignment = .top
    }
    field.setAccessibilityLabel(title)
    return row
  }
  @objc func makePrimarySelection(_ sender: NSButton) {
    guard selectionSet.items.indices.contains(sender.tag) else { return }
    actions.onSelectSelections?(StudioSelectionSet(items: selectionSet.items,
      primary: selectionSet.items[sender.tag]))
  }
  @objc func inspectorDidScroll(_ notification: Notification) {
    if !inspectorRestoringScroll { inspectorScrollPosition = inspectorScroll.contentView.bounds.origin }
  }
  func rebuildInspector() {
    updatePreviewFooter()
    let changedContext = lastInspectorSelection != selectionSet
      || lastInspectorTab != inspectorTabs.selectedSegment
      || lastInspectorComparisonActive != comparisonActive
    if changedContext { inspectorScrollPosition = .zero }
    lastInspectorSelection = selectionSet
    lastInspectorTab = inspectorTabs.selectedSegment
    lastInspectorComparisonActive = comparisonActive
    // Preserve the user's anchor across pending/completed value refreshes, even
    // if a temporary message briefly makes the document shorter than the clip.
    inspectorRestoringScroll = true
    defer { inspectorRestoringScroll = false }
    for v in inspectorStack.arrangedSubviews {
      inspectorStack.removeArrangedSubview(v)
      v.removeFromSuperview()
    }
    inspectorValues.removeAll()
    refreshSelectionHeading()
    if comparisonActive && inspectorTabs.selectedSegment == 0 { buildComparisonInspector() }
    switch inspectorTabs.selectedSegment {
    case 1: buildViewportInspector()
    case 2: buildDocumentInspector()
    default: buildSelectionInspector()
    }
    inspectorScroll.layoutSubtreeIfNeeded()
    inspectorScroll.contentView.scroll(to: inspectorScrollPosition)
    inspectorScroll.reflectScrolledClipView(inspectorScroll.contentView)
  }
  func selectionItemTitle(_ item: StudioSelection) -> String {
    let object = scene?.objects.first { $0.id == item.objectID }
    switch item {
    case .document: return L10n.tr("selection.none", "No object selected")
    case .object: return object?.label ?? item.objectID ?? "—"
    case .element(_, let id):
      let kind = object?.type == "scaffold" ? L10n.tr("element.node", "Node") : L10n.tr("element.gaussian", "Gaussian")
      return kind + " " + String(id) + " · " + (object?.label ?? item.objectID ?? "—")
    case .camera(_, let entry):
      return scene?.cameraNames.indices.contains(entry) == true
        ? scene!.cameraNames[entry] : object?.label ?? item.objectID ?? "—"
    }
  }
  func refreshSelectionHeading() {
    defer {
      selectionTitle.toolTip = selectionSet.items.map(selectionItemTitle).joined(separator: "\n")
      if selectionSet.items.count > 1 {
        selectionTitle.stringValue = L10n.format("selection.multiple", "%d selected", selectionSet.items.count)
        selectionSubtitle.stringValue = L10n.format("selection.primary", "Primary: %@", selectionItemTitle(selection))
      }
    }
    let object = scene?.objects.first { $0.id == selection.objectID }
    if let info = elementInfo {
      selectionTitle.stringValue =
        (info.isScaffold
          ? L10n.tr("element.node", "Node") : L10n.tr("element.gaussian", "Gaussian"))
        + "  \(info.id)"
      selectionSubtitle.stringValue = info.sourceLabel ?? object?.label ?? ""
      infoLine.stringValue =
        L10n.format(
          "info.position", "XYZ  %@",
          [info.position.x, info.position.y, info.position.z].map {
            L10n.number(Double($0), digits: 5)
          }.joined(separator: "  ")) + "\n"
        + L10n.format(
          "info.time", "Time %@ · %@", L10n.number(currentTime),
          L10n.tr("unit." + (scene?.timeUnit ?? "second"), scene?.timeUnit ?? "second"))
    } else if let object {
      selectionTitle.stringValue = cameraInspection?.name ?? object.label
      selectionSubtitle.stringValue =
        L10n.tr("type." + object.type, object.type) + " · "
        + (object.authored
          ? L10n.tr("object.editable", "Editable camera")
          : L10n.tr("object.readonly", "Read-only source object"))
      infoLine.stringValue = L10n.format(
        "info.domain", "Space %@ · Clock %@", object.space ?? "—", object.clock ?? "—")
    } else {
      selectionTitle.stringValue = L10n.tr("selection.none", "No object selected")
      selectionSubtitle.stringValue = L10n.tr(
        "selection.hint", "Select an object in the Outliner or click a primitive in the viewport.")
      infoLine.stringValue = scene?.name ?? ""
    }
  }
  func buildSelectionInspector() {
    if selectionSet.items.count > 1 {
      var members = [NSView]()
      for (index, item) in selectionSet.items.enumerated() {
        let choice = button(selectionItemTitle(item), #selector(makePrimarySelection(_:)),
          symbol: item == selection ? "circle.inset.filled" : "circle")
        choice.tag = index
        choice.lineBreakMode = .byTruncatingMiddle
        choice.toolTip = selectionItemTitle(item)
        choice.setAccessibilityLabel(L10n.format("selection.inspectMember", "Inspect %@", selectionItemTitle(item)))
        members.append(choice)
      }
      members.append(inspectorNote(L10n.tr("selection.mixedHint",
        "Command-click toggles selection. Choose a member here to inspect it while retaining the other selected items.")))
      section(L10n.tr("selection.members", "Selection members"), members)
    }
    guard let id = selection.objectID, let object = scene?.objects.first(where: { $0.id == id })
    else {
      section(
        L10n.tr("inspector.selection", "Selection"),
        [
          inspectorNote(
            L10n.tr(
              "selection.help",
              "Select a camera to inspect calibration and pose. Select a Gaussian or scaffold node to inspect its current attributes and trajectory. Selection does not switch the viewport camera."
            )),
          button(
            L10n.tr("camera.create", "Create camera"), #selector(createCamera(_:)), symbol: "camera"
          ),
        ])
      return
    }
    if let info = elementInfo {
      section(
        L10n.tr("inspector.element", "Selected element"),
        [
          property("identity", L10n.tr("property.identity", "Identity"), value: String(info.id)),
          property("position", L10n.tr("property.position", "Position XYZ")),
          property("valid", L10n.tr("property.present", "Present")),
          property("confidence", L10n.tr("property.confidence", "Confidence")),
        ])
      if !info.isScaffold {
        section(
          L10n.tr("inspector.gaussian", "Gaussian attributes"),
          [
            property("scale", L10n.tr("property.sigma", "σ XYZ")),
            property("quaternion", L10n.tr("property.quaternion", "Rotation WXYZ"), multiline: true),
            property("opacity", L10n.tr("property.opacity", "Opacity")),
            property("color", L10n.tr("property.color", "Stored RGB")),
            property("colorSpace", L10n.tr("property.colorSpace", "Color domain")),
            inspectorNote(L10n.tr(
              "element.colorNote",
              "Gaussian color before compositing, camera color correction and image clipping. Source values may lie outside [0,1]; they are shown unchanged."
            )),
          ])
      } else {
        section(
          L10n.tr("inspector.scaffold", "Scaffold node"),
          [
            inspectorNote(
              L10n.tr(
                "scaffold.glyphNote",
                "Glyph size, rotation, color and opacity are display settings, not measured node attributes."
              ))
          ])
      }
      focusButton.title = L10n.tr("view.focus", "Frame selection")
      focusButton.target = self
      focusButton.action = #selector(focusSelection(_:))
      focusButton.bezelStyle = .rounded
      section(
        L10n.tr("selection.actions", "Selection actions"),
        [
          focusButton,
          button(L10n.tr("selection.copy", "Copy attributes"), #selector(copySelection(_:))),
        ])
      updateElementValues()
    }
    if object.encoding == "gsv.observation.camera-rgb" {
      section(L10n.tr("compare.observation", "Camera observation"),[
        button(L10n.tr("compare.open", "Compare render with camera observation"),#selector(compareSelectedObservation(_:)),symbol:"rectangle.split.2x1")
      ])
    }
    if object.encoding == "gsv.render.view" {
      section(L10n.tr("render.viewSelector","Render view and source profile"),[
        button(L10n.tr("render.previewAction","Preview render view"),#selector(previewSelectedRenderView(_:)),symbol:"viewfinder")
      ])
    }
    if object.type == "camera" {
      if cameraInspection != nil {
        buildCameraInspector(object)
      } else {
        section(
          L10n.tr("camera.rig", "Camera rig"),
          [
            inspectorNote(
              L10n.tr(
                "camera.rigHint",
                "Expand this camera object to select an individual view. Its calibration and pose will follow the playhead."
              ))
          ])
      }
    }
    section(
      L10n.tr("inspector.object", "Object"),
      [
        property("objectID", L10n.tr("property.objectID", "Object ID"), value: object.id),
        property(
          "encoding", L10n.tr("property.encoding", "Encoding"), value: object.encoding == "gsv.gaussian.persistent-identity" ? L10n.tr("representation.identityPersistent", "Identity-Persistent Gaussian Trajectories") + "\n" + object.encoding : object.encoding,
          multiline: true),
        property("space", L10n.tr("property.space", "Space"), value: object.space ?? "—"),
        property("clock", L10n.tr("property.clock", "Clock"), value: object.clock ?? "—"),
        property("role", L10n.tr("property.role", "Role"), value: object.role ?? "—"),
      ])
    if object.type == "gaussian" || object.type == "scaffold" {
      idField.placeholderString = L10n.tr("selection.exactID", "Exact element ID")
      idField.target = self
      idField.action = #selector(findID(_:))
      configureField(idField, label: L10n.tr("selection.exactID", "Exact element ID"))
      section(
        L10n.tr("selection.find", "Find in this object"),
        [
          stack(
            [idField, button(L10n.tr("selection.findButton", "Find"), #selector(findID(_:)))],
            vertical: false, spacing: 6)
        ])
    }
    if !object.supported {
      section(
        L10n.tr("object.storage", "Stored representation"),
        [
          inspectorNote(
            L10n.tr(
              "object.unsupported",
              "Native parameters are preserved and inspectable. Direct rendering of this encoding is unavailable; use its identity-persistent trajectory object when provided."
            ))
        ])
    }
    section(
      L10n.tr("inspector.provenance", "Provenance"),
      [
        button(L10n.tr("inspector.raw", "View object descriptor…"), #selector(showDescriptor(_:))),
        inspectorNote(
          L10n.tr(
            "object.sourceSafe",
            "Source reconstruction data is read-only. Display settings do not change stored values."
          )),
      ])
  }
  private func transformComponents(_ fields: [NSTextField], axes: [String]) -> NSStackView {
    // These fields survive inspector rebuilds. Detach them from their previous
    // arranged hierarchy before installing constraints against new columns.
    let columns = zip(fields, axes).map { field, axis -> NSStackView in
      (field.superview as? NSStackView)?.removeArrangedSubview(field)
      field.removeFromSuperview()
      field.translatesAutoresizingMaskIntoConstraints = false
      field.setContentHuggingPriority(.defaultLow, for: .horizontal)
      field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
      field.usesSingleLineMode = true
      let axisLabel = label(axis, size: 10, weight: .semibold, secondary: true)
      let column = stack([axisLabel, field], spacing: 3)
      let width = field.widthAnchor.constraint(equalTo: column.widthAnchor)
      width.identifier = "studio.transform.componentWidth"
      width.isActive = true
      return column
    }
    let row = stack(columns, vertical: false, spacing: 6)
    row.distribution = .fillEqually
    row.alignment = .top
    row.detachesHiddenViews = false
    return row
  }
  func buildCameraInspector(_ object: WorkspaceObjectInfo) {
    guard let camera = cameraInspection else { return }
    section(
      L10n.tr("camera.view", "Camera"),
      [
        button(
          L10n.tr("camera.lookThrough", "Look through this camera"),
          #selector(lookThroughSelection(_:)), symbol: "viewfinder"),
        property("cameraTime", L10n.tr("camera.evaluatedTime", "Sample time")),
        property("cameraSize", L10n.tr("camera.imageSize", "Image size")),
        property("cameraPixel", L10n.tr("camera.pixelCenter", "Pixel centers")),
        property("cameraPosition", L10n.tr("property.position", "Position XYZ")),
        property("cameraCapture",L10n.tr("camera.captureContract","Camera and render contract"),value:camera.captureDetails,multiline:true),
      ])
    updateCameraValues(camera)
    if !camera.followsActiveClock {
      section(
        L10n.tr("camera.foreignClock", "Independent clock"),
        [
          inspectorNote(
            L10n.tr(
              "camera.foreignClockHint",
              "This camera uses another clock. The inspector shows its first sample; no implicit clock conversion is applied."
            ))
        ])
    }
    guard object.authored else {
      buildCameraCalibration(camera)
      return
    }
    cameraNameField.stringValue = editingCameraName ?? object.label
    configureField(cameraNameField, label: L10n.tr("camera.name", "Camera name"))
    cameraNameField.target = self
    cameraNameField.action = #selector(renameCamera(_:))
    let rename = stack(
      [cameraNameField, button(L10n.tr("common.apply", "Apply"), #selector(renameCamera(_:)))],
      vertical: false, spacing: 6)
    cameraFOVField.stringValue = editingFOV.map { L10n.number($0, digits: 2) } ?? ""
    configureField(cameraFOVField, label: L10n.tr("camera.fov", "Horizontal FOV · degrees"))
    cameraFOVField.target = self
    cameraFOVField.action = #selector(cameraFOVChanged(_:))
    let fov = stack(
      [
        cameraFOVField,
        button(L10n.tr("camera.keyFOV", "Key FOV"), #selector(cameraFOVChanged(_:))),
      ], vertical: false, spacing: 6)
    section(
      L10n.tr("camera.authoring", "Camera authoring"),
      [
        rename, label(L10n.tr("camera.fov", "Horizontal FOV · degrees"), size: 10, secondary: true),
        fov,
      ])
    for (i, f) in positionFields.enumerated() {
      configureField(f, label: "XYZ".map { String($0) }[i])
      f.stringValue = L10n.number(Double(camera.pose[3][i]), digits: 6)
    }
    let r = simd_float3x3(
      columns: (
        SIMD3(camera.pose[0].x, camera.pose[0].y, camera.pose[0].z),
        SIMD3(camera.pose[1].x, camera.pose[1].y, camera.pose[1].z),
        SIMD3(camera.pose[2].x, camera.pose[2].y, camera.pose[2].z)
      ))
    let q = simd_quatf(r)
    let values = [q.real, q.imag.x, q.imag.y, q.imag.z]
    for (i, f) in rotationFields.enumerated() {
      configureField(f, label: ["W", "X", "Y", "Z"][i])
      f.stringValue = L10n.number(Double(values[i]), digits: 6)
    }
    section(
      L10n.tr("camera.transform", "Transform at playhead"),
      [
        label(L10n.tr("property.position", "Position XYZ"), size: 10, secondary: true),
        transformComponents(positionFields, axes: ["X", "Y", "Z"]),
        label(L10n.tr("property.quaternion", "Rotation WXYZ"), size: 10, secondary: true),
        transformComponents(rotationFields, axes: ["W", "X", "Y", "Z"]),
        button(
          L10n.tr("camera.keyTransform", "Key transform"), #selector(cameraTransformChanged(_:))),
        inspectorNote(
          L10n.tr(
            "camera.transformHint",
            "Coordinates use the declared scene space. Rotation is a normalized quaternion; source cameras are never edited."
          )),
      ])
    cameraKeys.controlSize = .small
    cameraKeys.target = self
    cameraKeys.action = #selector(jumpCameraKey(_:))
    cameraKeys.setAccessibilityLabel(L10n.tr("camera.keys", "Camera keyframes"))
    refreshKeyList()
    configureField(keyTimeField, label: L10n.tr("camera.keyTime", "Keyframe time"))
    keyTimeField.target = self
    keyTimeField.action = #selector(retimeCameraKey(_:))
    section(
      L10n.tr("camera.keys", "Camera keyframes"),
      [
        cameraKeys,
        stack(
          [
            keyTimeField,
            button(L10n.tr("camera.retime", "Retime"), #selector(retimeCameraKey(_:))),
          ], vertical: false, spacing: 6),
        stack(
          [
            button(L10n.tr("camera.keyDelete", "Delete key"), #selector(deleteCameraKey(_:))),
            button(L10n.tr("camera.delete", "Delete camera"), #selector(deleteCamera(_:))),
          ], vertical: false, spacing: 6),
        inspectorNote(
          L10n.tr(
            "camera.interpolation",
            "Position and intrinsics interpolate linearly; orientation uses shortest-arc SLERP. Outside the key range, endpoints hold."
          )),
      ])
    section(
      L10n.tr("camera.saveSection", "Camera animation file"),
      [
        button(L10n.tr("camera.save", "Save Camera Animation…"), #selector(saveCameras(_:))),
        button(L10n.tr("camera.import", "Import Camera Animation…"), #selector(importCameras(_:))),
      ])
    buildCameraCalibration(camera)
  }
  func buildCameraCalibration(_ camera: WorkspaceCameraInspection) {
    section(
      L10n.tr("camera.calibration", "Calibration"),
      [
        property("cameraK", L10n.tr("camera.intrinsics", "K · pixels"), multiline: true),
        property("cameraPose", L10n.tr("camera.pose", "Camera → world"), multiline: true),
      ])
    updateCameraValues(camera)
  }
  func buildViewportInspector() {
    orbitStylePopup.removeAllItems()
    orbitStylePopup.addItems(withTitles: [
      L10n.tr("navigation.turntable", "Turntable · world up"),
      L10n.tr("navigation.trackball", "Trackball · free rotation"),
    ])
    orbitStylePopup.selectItem(at: trackballOrbit ? 1 : 0)
    orbitStylePopup.target = self
    orbitStylePopup.action = #selector(orbitStyleChanged(_:))
    configureToggle(
      pivotButton, L10n.tr("navigation.showPivot", "Show orbit center"),
      #selector(pivotVisibilityChanged(_:)), renderOptions.showOrbitCenter)
    section(
      L10n.tr("navigation.orbitCenter", "Orbit center"),
      [
        orbitStylePopup,
        property("orbitCenter", L10n.tr("property.position", "Position XYZ")),
        pivotButton,
        button(
          L10n.tr("navigation.pivotSelection", "Use selection as center"),
          #selector(pivotSelection(_:))),
        button(
          L10n.tr("navigation.pivotView", "Use visible scene depth"), #selector(pivotView(_:))),
        inspectorNote(
          L10n.tr(
            "navigation.pivotHint",
            "The amber cross is the fixed orbit center. Picking a primitive sets its position as the center. F frames the selection. Pan moves the center with the view; changing the source camera estimates a new center in front of it."
          )),
      ])
    updateOrbitCenter(orbitCenter)

    for (key, title, min, max, value) in [
      (
        "scale", L10n.tr("display.size", "Gaussian size"), 0.1, 4.0,
        Double(renderOptions.gaussianScale)
      ),
      (
        "opacity", L10n.tr("display.opacity", "Opacity multiplier"), 0.05, 2.0,
        Double(renderOptions.opacityScale)
      ),
      (
        "exposure", L10n.tr("display.exposure", "Exposure multiplier"), 0.1, 4.0,
        Double(renderOptions.exposure)
      ),
    ] {
      let slider = NSSlider(
        value: value, minValue: min, maxValue: max, target: self,
        action: #selector(renderSliderChanged(_:)))
      slider.identifier = .init(key)
      slider.setAccessibilityLabel(title)
      section(title, [property(key, title, value: L10n.number(value, digits: 2) + "×"), slider])
    }
    configureField(nearClipField, label: L10n.tr("display.nearClip", "Near clip in scene units"))
    nearClipField.stringValue = L10n.number(Double(renderOptions.nearClip), digits: 5)
    nearClipField.target = self
    nearClipField.action = #selector(nearClipFieldChanged(_:))
    section(
      L10n.tr("display.clipping", "Clipping"),
      [
        nearClipField,
        inspectorNote(
          L10n.tr(
            "display.clipHint", "Near distance uses original scene units. Valid range: 0.0001–100.")
        ),
      ])
    backgroundPopup.removeAllItems()
    backgroundPopup.addItems(withTitles: [
      L10n.tr("background.studio", "Studio"), L10n.tr("background.black", "Black"),
      L10n.tr("background.gray", "Gray"), L10n.tr("background.white", "White"),
    ])
    backgroundPopup.target = self
    backgroundPopup.action = #selector(backgroundChanged(_:))
    backgroundPopup.controlSize = .small
    section(
      L10n.tr("preview.background", "Preview background"),
      [backgroundPopup])
    section(
      L10n.tr("nav.title", "Navigation"),
      [
        button(L10n.tr("nav.help", "Navigation guide"), #selector(showNavigationHelp(_:))),
        stack(
          [
            button(L10n.tr("view.previous", "Previous View"), #selector(previousView(_:))),
            button(L10n.tr("view.next", "Next View"), #selector(nextView(_:))),
          ], vertical: false, spacing: 6),
        inspectorNote(
          L10n.tr(
            "nav.axisNote",
            "The viewport up-axis controls navigation and the grid. Stored coordinates and source camera calibration remain unchanged."
          )),
      ])
  }
  func configureToggle(_ button: NSButton, _ title: String, _ action: Selector, _ on: Bool) {
    button.title = title
    button.target = self
    button.action = action
    button.state = on ? .on : .off
    button.controlSize = .small
  }
  func buildDocumentInspector() {
    section(
      L10n.tr("document.summary", "Document"),
      [
        property(
          "filename", L10n.tr("document.file", "File"), value: scene?.url.lastPathComponent ?? "—",
          multiline: true),
        property(
          "objects", L10n.tr("document.objects", "Objects"),
          value: String(scene?.objects.count ?? 0)),
        property(
          "points", L10n.tr("document.primitives", "Visible IDs"),
          value: (scene?.pointCount ?? 0).formatted()),
        property(
          "samples", L10n.tr("document.samples", "Time samples"),
          value: String(scene?.frameCount ?? 0)),
      ])
    section(
      L10n.tr("document.coordinates", "Coordinate system"),
      [
        property("space", L10n.tr("property.space", "Space"), value: scene?.activeSpace ?? "—"),
        inspectorNote(scene?.spaceDescription ?? ""),
      ])
    section(
      L10n.tr("document.clock", "Time domain"),
      [
        property("clock", L10n.tr("property.clock", "Clock"), value: scene?.activeClock ?? "—"),
        property(
          "units", L10n.tr("property.units", "Units"),
          value: L10n.tr("unit." + (scene?.timeUnit ?? "second"), scene?.timeUnit ?? "second")),
        inspectorNote(scene?.clockDescription ?? ""),
        inspectorNote(
          L10n.tr(
            "document.clockNote",
            "Sample index and document time are different. Preview speed never changes source timestamps or declares a capture frame rate."
          )),
      ])
    section(
      L10n.tr("document.storage", "Storage & display"),
      [
        inspectorNote(scene?.description ?? ""),
        button(L10n.tr("camera.import", "Import Camera Animation…"), #selector(importCameras(_:))),
        button(L10n.tr("camera.save", "Save Camera Animation…"), #selector(saveCameras(_:))),
      ])
  }
  func updateElementValues() {
    guard let info = elementInfo else { return }
    func vector(_ a: [Float]) -> String {
      a.map { L10n.number(Double($0), digits: 6) }.joined(separator: "  ")
    }
    let colorDomain: String
    switch info.colorSpace {
    case "srgb": colorDomain = L10n.tr("colorSpace.srgb", "sRGB")
    case "linear-srgb": colorDomain = L10n.tr("colorSpace.linear-srgb", "Linear sRGB")
    default: colorDomain = info.colorSpace ?? L10n.tr("common.notProvided", "Not provided")
    }
    let values = [
      "identity": String(info.id),
      "position": vector([info.position.x, info.position.y, info.position.z]),
      "valid": info.valid
        ? L10n.tr("common.yes", "Yes") : L10n.tr("element.absent", "Absent at this time"),
      "confidence": info.confidence.map { L10n.number(Double($0), digits: 5) }
        ?? L10n.tr("common.notProvided", "Not provided"),
      "scale": vector([info.scales.x, info.scales.y, info.scales.z]),
      "opacity": L10n.number(Double(info.opacity), digits: 5),
      "color": vector([info.color.x, info.color.y, info.color.z]),
      "colorSpace": colorDomain,
      "quaternion": info.quaternion.map { vector([$0.x, $0.y, $0.z, $0.w]) } ?? "—",
    ]
    for (key, value) in values {
      inspectorValues[key]?.stringValue = value
      inspectorValues[key]?.toolTip = value
    }
  }
  func updateCameraValues(_ camera: WorkspaceCameraInspection) {
    func row(_ a: [Float]) -> String {
      a.map { L10n.number(Double($0), digits: 5) }.joined(separator: "   ")
    }
    let values = [
      "cameraTime": L10n.time(camera.sampleTime),
      "cameraCapture":camera.captureDetails,
      "cameraSize": "\(Int(camera.size.x)) × \(Int(camera.size.y)) px",
      "cameraPixel": camera.integerPixelCenters
        ? L10n.tr("pixel.integer", "Integer") : L10n.tr("pixel.half", "Half pixel"),
      "cameraPosition": row([camera.pose[3].x, camera.pose[3].y, camera.pose[3].z]),
      "cameraK": (0..<3).map { r in row((0..<3).map { camera.intrinsics[$0][r] }) }.joined(
        separator: "\n"),
      "cameraPose": (0..<4).map { r in row((0..<4).map { camera.pose[$0][r] }) }.joined(
        separator: "\n"),
    ]
    for (key, value) in values {
      inspectorValues[key]?.stringValue = value
      inspectorValues[key]?.toolTip = value
    }
    if camera.authored {
      let r = simd_float3x3(
        columns: (
          SIMD3(camera.pose[0].x, camera.pose[0].y, camera.pose[0].z),
          SIMD3(camera.pose[1].x, camera.pose[1].y, camera.pose[1].z),
          SIMD3(camera.pose[2].x, camera.pose[2].y, camera.pose[2].z)
        ))
      let q = simd_quatf(r)
      for (i, f) in positionFields.enumerated() where f.currentEditor() == nil {
        f.stringValue = L10n.number(Double(camera.pose[3][i]), digits: 6)
      }
      for (i, f) in rotationFields.enumerated() where f.currentEditor() == nil {
        f.stringValue = L10n.number(Double([q.real, q.imag.x, q.imag.y, q.imag.z][i]), digits: 6)
      }
    }
  }
  func refreshKeyList() {
    let selected = max(0, min(cameraKeys.indexOfSelectedItem, editingCameraTimes.count - 1))
    cameraKeys.removeAllItems()
    cameraKeys.addItems(
      withTitles: editingCameraTimes.enumerated().map {
        L10n.format(
          "camera.keyLabel", "Key %d · %@", $0.offset + 1, L10n.time($0.element))
      })
    if !editingCameraTimes.isEmpty {
      cameraKeys.selectItem(at: selected)
      if keyTimeField.currentEditor() == nil {
        keyTimeField.stringValue = L10n.time(editingCameraTimes[selected])
      }
    }
  }
  @objc func inspectorTabChanged(_ sender: Any?) { rebuildInspector() }
  @objc func copySelection(_ sender: Any?) {
    let lines = inspectorValues.keys.sorted().map {
      $0 + ": " + (inspectorValues[$0]?.stringValue ?? "")
    }
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(
      selectionTitle.stringValue + "\n" + lines.joined(separator: "\n"), forType: .string)
  }
  @objc func showDescriptor(_ sender: Any?) {
    descriptorPanel?.close()
    let panel = NSPanel(
      contentRect: NSRect(x: 0, y: 0, width: 650, height: 540),
      styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
    descriptorPanel = panel
    panel.title = L10n.tr("inspector.rawTitle", "Object descriptor")
    panel.isReleasedWhenClosed = false
    let scroll = NSScrollView(frame: panel.contentView!.bounds)
    scroll.autoresizingMask = [.width, .height]
    scroll.hasVerticalScroller = true
    let text = NSTextView(frame: scroll.bounds)
    text.isEditable = false
    text.isSelectable = true
    text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
    text.string = objectMetadata
    text.autoresizingMask = [.width]
    text.textContainer?.widthTracksTextView = true
    scroll.documentView = text
    panel.contentView?.addSubview(scroll)
    panel.center()
    panel.makeKeyAndOrderFront(nil)
  }
}
