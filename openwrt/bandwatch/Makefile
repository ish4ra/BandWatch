# OpenWrt feed package for BandWatch (pure Python, architecture independent).
# Usage: add this repository as a feed or copy openwrt/bandwatch into
# package/ of an OpenWrt 24.10/25.12 SDK, then: make package/bandwatch/compile
# NOTE: this Makefile has not yet been built inside an OpenWrt SDK; the CI
# produces equivalent apk/ipk packages with scripts/build.py.

include $(TOPDIR)/rules.mk

PKG_NAME:=bandwatch
PKG_VERSION:=0.1.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=BandWatch contributors

PKG_SOURCE_PROTO:=git
PKG_SOURCE_URL:=https://github.com/ish4ra/BandWatch.git
PKG_SOURCE_VERSION:=develop
PKG_MIRROR_HASH:=skip

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

define Build/Compile
endef

define Package/bandwatch/install
	$(INSTALL_DIR) $(1)/usr/lib/bandwatch/bandwatch
	( cd $(PKG_BUILD_DIR) && find bandwatch -type f ! -name '*.pyc' ) | \
		while read f; do $(INSTALL_DIR) $(1)/usr/lib/bandwatch/$$$$(dirname $$$$f); $(CP) $(PKG_BUILD_DIR)/$$$$f $(1)/usr/lib/bandwatch/$$$$f; done
	echo "$(PKG_VERSION)-beta.1" > $(1)/usr/lib/bandwatch/VERSION
	$(CP) $(PKG_BUILD_DIR)/openwrt/bandwatch/files/* $(1)/
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/scripts/bandwatch-diag.sh $(1)/usr/sbin/bandwatch-diag
endef

define Package/bandwatch/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] && exit 0
killall -HUP ubusd 2>/dev/null
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
