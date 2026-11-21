BandWatch 0.1.0-beta.1 - tester bundle (first hardware test: Arcadyan AW1000)
==============================================================================

සිංහල උපදෙස්: INSTALL-SINHALA.md විවෘත කරන්න (සම්පූර්ණ පරීක්ෂණ වටය එහි ඇත).

What is in this folder
----------------------
  bandwatch-0.1.0-beta.1-openwrt.tar.gz   RECOMMENDED installer (checks, backup, rollback)
  bandwatch-0.1.0_beta1-r1.apk            OpenWrt 25.12 package (alternative)
  luci-app-bandwatch-0.1.0_beta1-r1.apk   LuCI menu + single sign-on (alternative)
  *.ipk                                   OpenWrt 24.10 packages (not needed on 25.12)
  SHA256SUMS                              checksums of the files above
  bandwatch-test-round.sh                 collects test results (read-only)
  INSTALL-SINHALA.md                      step-by-step guide in Sinhala
  INSTALL.md / TROUBLESHOOTING.md         full guides in English
  HARDWARE-TESTING.md                     complete acceptance checklist

One test round (about 2 hours, mostly normal router use)
--------------------------------------------------------
  0. LuCI -> System -> Backup / Flash Firmware -> Generate archive (keep it safe)
  1. scp -O bandwatch-0.1.0-beta.1-openwrt.tar.gz bandwatch-test-round.sh root@192.168.1.1:/tmp/
  2. ssh root@192.168.1.1
     sh /tmp/bandwatch-test-round.sh before
  3. cd /tmp && tar -xzf bandwatch-0.1.0-beta.1-openwrt.tar.gz && cd bandwatch-0.1.0-beta.1
     sh install.sh --dry-run          (changes nothing)
     sh install.sh                    (write down the password it prints)
  4. Open LuCI -> Status -> BandWatch, use the router normally, work through
     section 4 of INSTALL-SINHALA.md (or HARDWARE-TESTING.md).
  5. sh /tmp/bandwatch-test-round.sh after
     sh /tmp/bandwatch-test-round.sh perf      (5 minutes, keep dashboard closed)
     sh /tmp/bandwatch-test-round.sh report    (BEFORE rebooting)
  6. Copy /tmp/bandwatch-test-report-*.tar.gz to your computer and attach it,
     with your notes, to a "Hardware test report" issue on GitHub.

Safety
------
  * BandWatch does not change firewall, DNS, DHCP, Wi-Fi, routing, offloading,
    Bandix or aria2 settings. "test-round after" verifies this.
  * Remove: sh uninstall.sh (keeps data)  |  sh uninstall.sh --purge (deletes data)
  * Diagnostics: bandwatch-diag (MAC/IP addresses, names and secrets removed)
  * Beta packages are unsigned: verify SHA256SUMS before installing.
