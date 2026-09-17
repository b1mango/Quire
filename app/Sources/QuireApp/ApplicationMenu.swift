import AppKit

func makeApplicationMenu() -> NSMenu {
    let menu = NSMenu()
    let application = NSMenu(title: "卷帙")
    application.addItem(
        withTitle: "隐藏卷帙", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
    let hideOthers = application.addItem(
        withTitle: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)),
        keyEquivalent: "h")
    hideOthers.keyEquivalentModifierMask = [.command, .option]
    application.addItem(
        withTitle: "显示全部", action: #selector(NSApplication.unhideAllApplications(_:)),
        keyEquivalent: ""
    )
    application.addItem(.separator())
    application.addItem(
        withTitle: "退出卷帙", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    let applicationItem = menu.addItem(withTitle: "卷帙", action: nil, keyEquivalent: "")
    applicationItem.submenu = application

    // Nil targets dispatch through the responder chain to WKWebView's focused editor.
    let edit = NSMenu(title: "编辑")
    edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
    let redo = edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z")
    redo.keyEquivalentModifierMask = [.command, .shift]
    edit.addItem(.separator())
    edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    let editItem = menu.addItem(withTitle: "编辑", action: nil, keyEquivalent: "")
    editItem.submenu = edit
    return menu
}
