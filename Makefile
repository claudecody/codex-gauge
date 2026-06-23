.PHONY: build install uninstall check clean

APP := build/CodexGauge.app

build:
	mkdir -p "$(APP)/Contents/MacOS" "$(APP)/Contents/Resources"
	swiftc -framework AppKit -framework Foundation Sources/CodexGauge/main.swift -o "$(APP)/Contents/MacOS/CodexGauge"
	cp packaging/Info.plist "$(APP)/Contents/Info.plist"
	cp scripts/codex-gauge-usage.py "$(APP)/Contents/Resources/codex-gauge-usage.py"
	chmod +x "$(APP)/Contents/MacOS/CodexGauge" "$(APP)/Contents/Resources/codex-gauge-usage.py"
	codesign --force --deep --sign - "$(APP)"

install:
	scripts/install.sh

uninstall:
	scripts/uninstall.sh

check:
	swiftc -typecheck -framework AppKit -framework Foundation Sources/CodexGauge/main.swift
	python3 -m py_compile scripts/codex-gauge-usage.py
	plutil -lint packaging/Info.plist

clean:
	rm -rf build dist
