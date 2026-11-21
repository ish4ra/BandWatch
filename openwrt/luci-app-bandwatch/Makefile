# LuCI integration for BandWatch (menu entry + single sign-on).
# Layout follows luci.mk conventions: root/ is copied to /, htdocs/ to /www.
include $(TOPDIR)/rules.mk

LUCI_TITLE:=LuCI support for BandWatch
LUCI_DEPENDS:=+bandwatch +luci-base
LUCI_PKGARCH:=all
PKG_LICENSE:=MIT

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage - OpenWrt buildroot signature
