import AppKit

let arguments = CommandLine.arguments
guard arguments.count >= 3, let image = NSImage(contentsOfFile: arguments[1]) else {
    fatalError("Usage: set-macos-icon.swift ICON TARGET [TARGET ...]")
}

// Apply after signing: Finder custom icons use FinderInfo and an Icon\r resource fork.
// Based on StreamVerse's set-macos-icon.swift; retain these attributes when packaging.
for path in arguments.dropFirst(2) {
    guard FileManager.default.fileExists(atPath: path),
        NSWorkspace.shared.setIcon(image, forFile: path, options: [])
    else {
        fatalError("Cannot set the custom icon: \(path)")
    }
    print("Applied transparent icon: \(path)")
}
