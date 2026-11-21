BandWatch 0.1.0-beta.1 - tester bundle (first hardware test: Arcadyan AW1000)
==============================================================================

සිංහල උපදෙස්: INSTALL-SINHALA.md විවෘත කරන්න (සම්පූර්ණ පරීක්ෂණ වටය එහි ඇත).

Download and check
------------------
  Downloaded from the public release page (no GitHub account needed):
  https://github.com/ish4ra/BandWatch/releases/tag/v0.1.0-beta.1

  The packages are UNSIGNED, so check them before installing. In this folder:
    Linux:    sha256sum -c SHA256SUMS
    macOS:    shasum -a 256 -c SHA256SUMS
    Windows:  Get-FileHash bandwatch-0.1.0-beta.1-openwrt.tar.gz
              (compare with the matching line in SHA256SUMS)

  Browser: use Chrome, Edge or Brave (or Safari). Firefox is not yet
  verified for this beta: https://github.com/ish4ra/BandWatch/issues/2

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
  IMPORTANT: /tmp on the router is RAM. A reboot deletes it, together with
  this helper and its reports. Copy each report to your computer BEFORE
  rebooting.

  0. LuCI -> System -> Backup / Flash Firmware -> Generate archive (keep it safe)
  1. On your computer, in this folder:
     scp -O bandwatch-0.1.0-beta.1-openwrt.tar.gz bandwatch-test-round.sh root@192.168.1.1:/tmp/
  2. ssh root@192.168.1.1
     sh /tmp/bandwatch-test-round.sh before
  3. cd /tmp && tar -xzf bandwatch-0.1.0-beta.1-openwrt.tar.gz && cd bandwatch-0.1.0-beta.1
     sh install.sh --dry-run          (changes nothing)
     sh install.sh                    (write down the password it prints)
  4. Open LuCI -> Status -> BandWatch, use the router normally, work through
     section 4 of INSTALL-SINHALA.md (or HARDWARE-TESTING.md). Do NOT reboot yet.
  5. sh /tmp/bandwatch-test-round.sh after     (shows "VERDICT: ..." for WAN isolation)
     sh /tmp/bandwatch-test-round.sh perf      (5 minutes, keep dashboard closed)
     sh /tmp/bandwatch-test-round.sh report
  6. On your computer (not on the router):
     scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .
     Check that bandwatch-pre-reboot-report-*.tar.gz is now on your computer.
  7. Reboot test - only after step 6:
     reboot                                    (on the router; wait 2-3 minutes)
     scp -O bandwatch-test-round.sh root@192.168.1.1:/tmp/    (on your computer)
     ssh root@192.168.1.1
     sh /tmp/bandwatch-test-round.sh postreboot
     scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .   (on your computer)
  8. Attach both reports, with your notes, to a "Hardware test report" issue
     on GitHub.

  WAN isolation: "VERDICT: PASS" (local check) is the conclusive result. A
  test from mobile data proves nothing if the router's WAN address starts
  with 100.64-100.127, 10., 172.16-172.31 or 192.168. (CGNAT / private).

Safety
------
  * BandWatch does not change firewall, DNS, DHCP, Wi-Fi, routing, offloading,
    Bandix or aria2 settings. "test-round after" verifies this.
  * Remove: sh uninstall.sh (keeps data)  |  sh uninstall.sh --purge (deletes data;
    refuses, removing nothing, if the data folder looks unsafe)
  * Diagnostics: bandwatch-diag (MAC/IP addresses, names and secrets removed)
  * Beta packages are unsigned: verify SHA256SUMS before installing.
