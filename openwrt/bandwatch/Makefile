# OpenWrt feed package for BandWatch (pure Python, architecture independent).
#
# Use this repository as a local feed in an OpenWrt SDK or buildroot:
#   echo "src-link bandwatch /path/to/BandWatch/openwrt" >> feeds.conf
#   ./scripts/feeds update bandwatch && ./scripts/feeds install -a -p bandwatch
#   make package/bandwatch/compile package/luci-app-bandwatch/compile
# Validated with the OpenWrt 25.12.0 SDK (x86-64) in CI-like conditions.

include $(TOPDIR)/rules.mk

PKG_NAME:=bandwatch
PKG_VERSION:=0.1.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=BandWatch contributors

# Repository root (this file lives in <root>/openwrt/bandwatch/).
BANDWATCH_SRC ?= $(CURDIR)/../..

include $(INCLUDE_DIR)/package.mk

define Package/bandwatch
  SECTION:=net
  CATEGORY:=Network
  TITLE:=BandWatch local traffic monitor
  URL:=https://github.com/ish4ra/BandWatch
  PKGARCH:=all
  USERID:=bandwatch=377:bandwatch=377
  DEPENDS:=+python3-light +python3-sqlite3 +python3-urllib
endef

define Package/bandwatch/description
  Lightweight, privacy-focused WAN, per-device (Bandix/nlbwmon) and aria2
  traffic monitor with a local web dashboard. No telemetry, no cloud.
endef

define Package/bandwatch/conffiles
/etc/config/bandwatch
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) $(BANDWATCH_SRC)/bandwatch $(BANDWATCH_SRC)/scripts $(BANDWATCH_SRC)/LICENSE $(PKG_BUILD_DIR)/
	$(CP) $(BANDWATCH_SRC)/openwrt/bandwatch/files $(PKG_BUILD_DIR)/owrt-files
endef

define Build/Compile
endef

define Package/bandwatch/install
	$(INSTALL_DIR) $(1)/usr/lib/bandwatch
	( cd $(PKG_BUILD_DIR) && find bandwatch -name '__pycache__' -prune -o -type f ! -name '*.pyc' -print ) | \
		while read f; do $(INSTALL_DIR) $(1)/usr/lib/bandwatch/$$$$(dirname $$$$f); $(INSTALL_DATA) $(PKG_BUILD_DIR)/$$$$f $(1)/usr/lib/bandwatch/$$$$f; done
	echo "$(PKG_VERSION)-beta.1" > $(1)/usr/lib/bandwatch/VERSION
	$(CP) $(PKG_BUILD_DIR)/owrt-files/* $(1)/
	chmod 0755 $(1)/etc/init.d/bandwatch $(1)/usr/bin/bandwatch
	$(INSTALL_DIR) $(1)/usr/sbin $(1)/usr/share/doc/bandwatch
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/scripts/bandwatch-diag.sh $(1)/usr/sbin/bandwatch-diag
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/LICENSE $(1)/usr/share/doc/bandwatch/LICENSE
endef

define Package/bandwatch/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] && exit 0
killall -HUP ubusd 2>/dev/null
i=0; while ! ubus list >/dev/null 2>&1 && [ $$i -lt 15 ]; do sleep 1; i=$$((i+1)); done
/etc/init.d/bandwatch enable
/etc/init.d/bandwatch restart
exit 0
endef

define Package/bandwatch/prerm
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] && exit 0
/etc/init.d/bandwatch stop
/etc/init.d/bandwatch disable
exit 0
endef

$(eval $(call BuildPackage,bandwatch))
