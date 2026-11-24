BandWatch 0.1.0-beta.2 - tester bundle
======================================

සිංහල උපදෙස්: UPGRADE-SINHALA.md (beta.1 සිට යාවත්කාලීන කිරීම සහ එක් ක්ලික්
diagnostic report) සහ INSTALL-SINHALA.md (සම්පූර්ණ පරීක්ෂණ වටය).

Already running beta.1? Upgrade - do NOT uninstall, nothing is lost
-------------------------------------------------------------------
  1. Check the file (packages are unsigned). In this folder:
       Windows:  Get-FileHash bandwatch-0.1.0-beta.2-openwrt.tar.gz
                 (compare with the matching line in SHA256SUMS)
       Linux:    sha256sum -c SHA256SUMS      macOS: shasum -a 256 -c SHA256SUMS
  2. On your computer:
       scp -O bandwatch-0.1.0-beta.2-openwrt.tar.gz root@192.168.1.1:/tmp/
  3. On the router (ssh root@192.168.1.1):
       cd /tmp && tar -xzf bandwatch-0.1.0-beta.2-openwrt.tar.gz && cd bandwatch-0.1.0-beta.2 && sh install.sh
  4. Open the dashboard, press Ctrl+F5 once. Your password and history are kept.
  Details: UPGRADE.md / UPGRADE-SINHALA.md

Sending a report: ONE CLICK, no SSH
-----------------------------------
  Dashboard -> Health -> "Download Diagnostic Report" -> send the ZIP file.
  Best after about 24 hours of normal use. The report contains no passwords,
  secrets, IP/MAC addresses, device or Wi-Fi names, URLs or file names, and
  BandWatch never uploads anything by itself. SUMMARY.txt inside the ZIP
  shows exactly what was checked.

  Browser: Chrome, Edge or Brave (or Safari). Firefox status:
  https://github.com/ish4ra/BandWatch/issues/2

Fresh installation
------------------
  See INSTALL-SINHALA.md / INSTALL.md (same installer, sh install.sh).

What is in this folder
----------------------
  bandwatch-0.1.0-beta.2-openwrt.tar.gz   RECOMMENDED installer / upgrader (checks, backup, rollback)
  bandwatch-0.1.0_beta2-r1.apk            OpenWrt 25.12 package (alternative)
  luci-app-bandwatch-0.1.0_beta2-r1.apk   LuCI menu + single sign-on (alternative)
  *.ipk                                   OpenWrt 24.10 packages (not needed on 25.12)
  SHA256SUMS                              checksums of the files above
  UPGRADE.md / UPGRADE-SINHALA.md         upgrade + one-click report guide
  bandwatch-test-round.sh                 OPTIONAL full acceptance helper (read-only)
  INSTALL-SINHALA.md, INSTALL.md, TROUBLESHOOTING.md, HARDWARE-TESTING.md

Optional: full acceptance test round (only if the maintainer asks)
------------------------------------------------------------------
One test round (about 2 hours, mostly normal router use)
--------------------------------------------------------
  IMPORTANT: /tmp on the router is RAM. A reboot deletes it, together with
  this helper and its reports. Copy each report to your computer BEFORE
  rebooting.

  0. LuCI -> System -> Backup / Flash Firmware -> Generate archive (keep it safe)
  1. On your computer, in this folder:
     scp -O bandwatch-0.1.0-beta.2-openwrt.tar.gz bandwatch-test-round.sh root@192.168.1.1:/tmp/
  2. ssh root@192.168.1.1
     sh /tmp/bandwatch-test-round.sh before
  3. cd /tmp && tar -xzf bandwatch-0.1.0-beta.2-openwrt.tar.gz && cd bandwatch-0.1.0-beta.2
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
  * Diagnostics: dashboard -> Health -> Download Diagnostic Report (or bandwatch-diag)
  * Beta packages are unsigned: verify SHA256SUMS before installing.
