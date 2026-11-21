#!/bin/sh
# Security-sensitive code checks run by CI (and locally before committing).
cd "$(dirname "$0")/.." || exit 1
FAIL=0
bad() { echo "FAIL: $1"; FAIL=1; }

# No shell execution, eval or exec in the application.
if grep -rnE "shell=True|os\.system\(|os\.popen\(|\beval\(|\bexec\(" bandwatch --include=*.py; then
	bad "shell/eval/exec usage in Python code"
fi
# Dashboard: no inline scripts, no remote assets, no inline event handlers.
if grep -rnE "<script>|https?://(cdn|fonts\.|unpkg|jsdelivr|googleapis)" bandwatch/web; then
	bad "inline script or remote asset in the dashboard"
fi
if grep -rnE "\son(click|load|error|mouseover)=" bandwatch/web --include=*.html --include=*.js; then
	bad "inline event handler"
fi
# The CSP must not allow inline code.
if grep -n "unsafe-inline\|unsafe-eval" bandwatch/server.py; then
	bad "CSP allows inline code"
fi
# No private keys, tokens, databases or real router dumps committed.
if grep -rnIE --exclude-dir=.git --exclude-dir=dist --exclude-dir=node_modules \
	-e "-----BEGIN [A-Z ]*PRIVATE KEY-----" -e "ghp_[A-Za-z0-9]{30,}" -e "github_pat_[A-Za-z0-9_]{30,}" .; then
	bad "credential-like material found"
fi
FOUND=$(find . \( -path ./.git -o -path ./dist -o -path ./tests/frontend/node_modules \) -prune -o \
	\( -name "*.db" -o -name "*.db-wal" -o -name "auth.json" -o -name "secrets.json" -o -name "bandwatch-diag-*.txt" \) -print)
if [ -n "$FOUND" ]; then
	echo "$FOUND"
	bad "database, secret or diagnostics file committed"
fi
# Valid JSON for shipped configuration files.
for f in openwrt/bandwatch/files/usr/share/acl.d/*.json openwrt/luci-app-bandwatch/root/usr/share/*/*/*.json bandwatch/web/manifest.webmanifest tests/fixtures/*.json; do
	python3 -m json.tool "$f" >/dev/null 2>&1 || bad "invalid JSON: $f"
done
[ "$FAIL" = 0 ] && echo "security checks passed"
exit "$FAIL"
