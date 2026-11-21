# LuCI integration for BandWatch (menu entry + single sign-on).
# Self-contained (does not need the LuCI feed's luci.mk), so it builds in a
# plain SDK. Layout: root/ is installed to /, htdocs/ to /www.

include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-bandwatch
PKG_VERSION:=0.1.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_MAINTAINER:=BandWatch contributors

include $(INCLUDE_DIR)/package.mk

define Package/luci-app-bandwatch
  SECTION:=luci
  CATEGORY:=LuCI
  SUBMENU:=3. Applications
  TITLE:=LuCI support for BandWatch
  DEPENDS:=+bandwatch +luci-base
  PKGARCH:=all
endef

define Package/luci-app-bandwatch/description
  Status -> BandWatch menu entry with single sign-on into the BandWatch dashboard.
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)
endef

define Build/Compile
endef

define Package/luci-app-bandwatch/install
	$(INSTALL_DIR) $(1)/usr/share/luci/menu.d $(1)/usr/share/rpcd/acl.d $(1)/usr/libexec/rpcd $(1)/www/luci-static/resources/view/bandwatch
	$(INSTALL_DATA) ./root/usr/share/luci/menu.d/luci-app-bandwatch.json $(1)/usr/share/luci/menu.d/
	$(INSTALL_DATA) ./root/usr/share/rpcd/acl.d/luci-app-bandwatch.json $(1)/usr/share/rpcd/acl.d/
	$(INSTALL_BIN) ./root/usr/libexec/rpcd/luci.bandwatch $(1)/usr/libexec/rpcd/
	$(INSTALL_DATA) ./htdocs/luci-static/resources/view/bandwatch/dashboard.js $(1)/www/luci-static/resources/view/bandwatch/
endef

define Package/luci-app-bandwatch/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] && exit 0
rm -f /tmp/luci-indexcache /tmp/luci-indexcache.*
/etc/init.d/rpcd reload
exit 0
endef

$(eval $(call BuildPackage,luci-app-bandwatch))
