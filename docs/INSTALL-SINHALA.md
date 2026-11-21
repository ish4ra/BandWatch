# BandWatch — ස්ථාපනය සහ පරීක්ෂණ මාර්ගෝපදේශය (සිංහල)

මෙය BandWatch **beta** අනුවාදය (v0.1.0-beta.1) Arcadyan AW1000 router එකක
පළමු වරට පරීක්ෂා කරන අය සඳහාය. පහත පියවර අනුපිළිවෙලින් කළහොත් එක්
පරීක්ෂණ වටයක් (test round) සම්පූර්ණ කළ හැක. විධාන (commands) ඉංග්‍රීසියෙන්ම,
පිටපත් කර (copy-paste) ඇතුළත් කරන්න.

> **වැදගත්:** මෙය beta මෘදුකාංගයකි. BandWatch firewall, DNS, DHCP, Wi-Fi,
> routing, offloading, Bandix හෝ aria2 සැකසුම් **කිසිවක් වෙනස් නොකරයි**.
> එසේ වුවද ආරක්ෂාව සඳහා පළමුව backup එකක් ගන්න.

---

## 0. සූදානම (මිනිත්තු 5)

0. Tester bundle එක මෙතැනින් download කරන්න (GitHub ගිණුමක් අවශ්‍ය නැත):
   https://github.com/ish4ra/BandWatch/releases/download/v0.1.0-beta.1/bandwatch-0.1.0-beta.1-tester-bundle.zip
   ඉන්පසු zip ගොනුව extract කරන්න (right-click → Extract All).
   Dashboard එක සඳහා **Chrome, Edge හෝ Brave** බ්‍රවුසරයක් භාවිතා කරන්න
   (Firefox මෙම beta එකේ තවම තහවුරු කර නැත).
1. LuCI → **System → Backup / Flash Firmware → Generate archive** ඔබා
   backup ගොනුව පරිගණකයේ සුරකින්න.
2. Bundle එකේ ඇති ගොනු:

| ගොනුව | භාවිතය |
| --- | --- |
| `bandwatch-0.1.0-beta.1-openwrt.tar.gz` | **නිර්දේශිත** ස්ථාපකය |
| `bandwatch-0.1.0_beta1-r1.apk`, `luci-app-bandwatch-0.1.0_beta1-r1.apk` | apk පැකේජ (විකල්ප) |
| `SHA256SUMS` | ගොනු නිවැරදිදැයි පරීක්ෂාවට |
| `bandwatch-test-round.sh` | පරීක්ෂණ තොරතුරු එකතු කරන script එක |
| `HARDWARE-TESTING.md` | සම්පූර්ණ පරීක්ෂණ ලැයිස්තුව (ඉංග්‍රීසි) |

3. Windows PowerShell හි (bundle ෆෝල්ඩරය තුළ) ගොනු router එකට යවන්න:

```sh
scp -O bandwatch-0.1.0-beta.1-openwrt.tar.gz bandwatch-test-round.sh root@192.168.1.1:/tmp/
```

(`192.168.1.1` වෙනුවට ඔබේ router ලිපිනය යොදන්න.)

**ගොනු නිවැරදිදැයි පරීක්ෂා කිරීම** (පැකේජ sign කර නැති නිසා මෙය වැදගත්ය):
Windows PowerShell හි `Get-FileHash bandwatch-0.1.0-beta.1-openwrt.tar.gz`
ක්‍රියාත්මක කරන්න. පෙන්වන අගය `SHA256SUMS` ගොනුවේ ඇති අගයට **සම්පූර්ණයෙන්ම**
සමාන විය යුතුයි. (Mac: `shasum -a 256 bandwatch-0.1.0-beta.1-openwrt.tar.gz`)
සමාන නොවේ නම් ස්ථාපනය නොකර නැවත download කරන්න.

## 1. ස්ථාපනයට පෙර තොරතුරු (මිනිත්තු 1)

```sh
ssh root@192.168.1.1
sh /tmp/bandwatch-test-round.sh before
```

මෙය router සැකසුම්වල "ඇඟිලි සලකුණක්" (fingerprint) සුරකියි. පසුව BandWatch
කිසිවක් වෙනස් නොකළ බව තහවුරු කිරීමට එය භාවිතා වේ.

## 2. පූර්ව පරීක්ෂාව සහ ස්ථාපනය (මිනිත්තු 5)

```sh
cd /tmp
tar -xzf bandwatch-0.1.0-beta.1-openwrt.tar.gz
cd bandwatch-0.1.0-beta.1
sh install.sh --dry-run
```

* `--dry-run` කිසිවක් වෙනස් නොකරයි. `[FAIL]` පෙන්වුවහොත් එහි සඳහන් විධානය
  ක්‍රියාත්මක කරන්න. Python නොමැති නම්:

```sh
apk update && apk add python3-light python3-sqlite3 python3-urllib
```

ඉන්පසු ස්ථාපනය:

```sh
sh install.sh
```

අවසානයේ පෙන්වන **Dashboard ලිපිනය** සහ **Password** ලියා තබා ගන්න.
ස්ථාපනය අසාර්ථක වුවහොත් එය ස්වයංක්‍රීයව පෙර තත්ත්වයට (rollback) පත් වේ.

## 3. Dashboard විවෘත කිරීම

* LuCI → **Status → BandWatch** (මුරපදය නැතිව ඇතුළු වේ), හෝ
* බ්‍රවුසරයේ `http://192.168.1.1:8787/` (password එක යොදන්න)
* Settings → aria2 හි aria2 **RPC secret** එක ඇතුළත් කරන්න (aria2 භාවිතා කරන්නේ නම්).

## 4. පරීක්ෂණ (පැය 1–2, router සාමාන්‍ය ලෙස භාවිතා කරමින්)

සෑම අංකයක් සඳහාම ✅ (හරි), ⚠️ (සුළු ගැටලුව) හෝ ❌ (වැරදි) සටහන් කරන්න.

1. **Overview**: Download/Upload වේගය තත්පර 2–3 කට වරක් යාවත්කාලීන වේද?
2. **Speed test**: පරිගණකයෙන් speedtest එකක් කරන්න. BandWatch උපරිම වේගය
   speedtest ප්‍රතිඵලයට ආසන්නද (±10%)? සංඛ්‍යා දෙකම ලියන්න.
3. **100 MB download**: පරිගණකයෙන් ~100 MB ගොනුවක් download කරන්න. Overview හි
   "Today" අගය ~100 MB (8% ට අඩු අමතරයක් සමඟ) වැඩි වේද?
4. **Health → Collectors**: WAN = *Working* සහ interface = `wwan0` ද?
5. **Devices**: දුරකථනය / පරිගණකය නම් සමඟ පෙනේද? Wi-Fi/Ethernet නිවැරදිද?
6. දුරකථනයෙන් video එකක් බලන විට එය **Download** ලෙස පෙනේද? (Upload ලෙස
   පෙනේ නම් ❌ ලෙස සටහන් කරන්න — Bandix දිශාව පරීක්ෂාව.)
7. උපාංගයක් ක්ලික් කර **Custom** දින පරාසයක් තෝරන්න — එකතුව පෙනේද?
8. LAN තුළ ගොනු පිටපත් කිරීම (උදා: Samba) **LAN-local** ලෙස පෙනේද, Internet ලෙස නොවේද?
9. **aria2**: Today / This month කාඩ්, live වේගය සහ tasks පෙනේද?
   අඩකින් download කළ ගොනුවක් නැවත ආරම්භ කළ විට පැරණි දත්ත "Today" එකට එකතු නොවේද?
10. **History**: Today / This month එකතුව vnStat සමඟ සසඳන්න ("Compare" බොත්තම).
11. Mobile data **disconnect → connect** කරන්න: History හි අස්වාභාවික spike එකක් නැතිද?
12. ජංගම දුරකථනයෙන් dashboard එක හොඳින් පෙනේද? Dark/Light theme වෙනස් වේද?
13. **WAN පැත්තෙන් ප්‍රවේශය නොමැති බව** — ප්‍රධාන පරීක්ෂාව router එක තුළම කරයි
    (පියවර 5 හි `after` විධානය). එය `VERDICT: PASS (BandWatch listens only on
    loopback and LAN addresses)` ලෙස පෙන්විය යුතුයි. අතින් බැලීමට:

    ```sh
    netstat -ltn | grep 8787
    ```

    `127.0.0.1:8787` සහ ඔබේ LAN ලිපිනය (උදා: `192.168.1.1:8787`) පමණක් පෙනිය යුතුයි.
    `0.0.0.0:8787` හෝ `:::8787` පෙනේ නම් ⚠️ ලෙස සටහන් කරන්න.

    **අමතර (විකල්ප) පරීක්ෂාව:** දුරකථනයේ Wi-Fi off කර, mobile data මගින්
    `http://<router WAN IP>:8787/` විවෘත කිරීමට උත්සාහ කරන්න — එය විවෘත නොවිය යුතුයි.
    **සැලකිය යුතුයි:** LuCI → Status → Overview හි WAN IPv4 ලිපිනය `100.64.` – `100.127.`,
    `10.`, `172.16.` – `172.31.` හෝ `192.168.` වලින් ආරම්භ වේ නම්, ඔබේ සම්බන්ධතාවය
    **CGNAT** (ISP එකේ පොදු NAT) පිටුපස ඇත. එවිට පිටතින් router එකට කිසිසේත් ළඟා විය
    නොහැකි නිසා, පිටුව විවෘත නොවීමෙන් BandWatch ආරක්ෂිත බව **ඔප්පු නොවේ**. එවිට
    "CGNAT — external test not conclusive" ලෙස සටහන් කර, ඉහත `netstat` / `VERDICT`
    ප්‍රතිඵලය මත විශ්වාසය තබන්න.

> Router reboot පරීක්ෂාව **මෙහි නොකරන්න** — එය පියවර 6 හිදී, වාර්තාව පරිගණකයට
> ගත් පසුවයි. (`/tmp` ෆෝල්ඩරය සහ එහි ඇති script එක reboot එකකදී මැකී යයි.)

## 5. තොරතුරු එකතු කර පරිගණකයට ගැනීම — reboot කිරීමට පෙර (මිනිත්තු 10)

Router එකේ (ssh තුළ):

```sh
sh /tmp/bandwatch-test-round.sh after
sh /tmp/bandwatch-test-round.sh perf
sh /tmp/bandwatch-test-round.sh report
```

* `after` — router සැකසුම් වෙනස් නොවූ බව ("OK: ... configuration unchanged") සහ
  WAN ප්‍රවේශ පරීක්ෂාව (`VERDICT: ...`) පෙන්වයි.
* `perf` — විනාඩි 5 ක් ගනී; ඒ කාලයේ dashboard එක **වසා** තබන්න.
  ඉලක්කය: සාමාන්‍ය CPU < 2%, මතකය (RSS) < 30 MB.
* `report` — `/tmp/bandwatch-test-reports/` තුළ වාර්තා ගොනුවක් සාදයි.

දැන් **පරිගණකයේ** (router එකේ නොව — නව PowerShell / Terminal කවුළුවක්) වාර්තාව
පිටපත් කරන්න:

```sh
scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .
```

ගොනුව `bandwatch-pre-reboot-report-....tar.gz` නමින් පරිගණකයේ ෆෝල්ඩරයේ ඇති බව
බලන්න. **එය පරිගණකයේ ඇති බව තහවුරු කළ පසුව පමණක්** ඊළඟ පියවරට යන්න.

## 6. Reboot පරීක්ෂාව (මිනිත්තු 10)

1. Router එක reboot කරන්න (ssh තුළ):

```sh
reboot
```

2. විනාඩි 2–3 ක් රැඳී සිට, LuCI නැවත විවෘත වන තුරු බලන්න.
3. Reboot එකෙන් `/tmp` මැකී ඇති නිසා, script එක **නැවත** router එකට යවන්න
   (පරිගණකයේ, bundle ෆෝල්ඩරය තුළ):

```sh
scp -O bandwatch-test-round.sh root@192.168.1.1:/tmp/
```

4. Router එකට ඇතුළු වී reboot පසු තත්ත්වය එකතු කරන්න:

```sh
ssh root@192.168.1.1
sh /tmp/bandwatch-test-round.sh postreboot
```

5. පරීක්ෂා කරන්න (✅/⚠️/❌):
   * BandWatch ස්වයංක්‍රීයව ආරම්භ වූවාද? (`postreboot` හි `== service` → *running*)
   * Dashboard එකේ reboot එකට පෙර දත්ත තවමත් තිබේද? (History → This month)
   * Reboot කාලය "unmeasured" (මැනිය නොහැකි) ලෙස පෙන්වයි, ව්‍යාජ spike එකක් නැත.
   * `VERDICT: PASS` තවමත් පෙන්වයිද?
6. Reboot පසු වාර්තාවද පරිගණකයට ගන්න (පරිගණකයේ):

```sh
scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .
```

## 7. ප්‍රතිඵල බෙදාගැනීම

* ඔබට දැන් ගොනු **දෙකක්** තිබිය යුතුයි: `bandwatch-pre-reboot-report-....tar.gz`
  සහ `bandwatch-post-reboot-report-....tar.gz`.
* MAC/IP ලිපින, උපාංග නම්, Wi-Fi නම්, මුරපද, secret සහ download ගොනු නම් ඉවත් කර ඇත.
  කිසියම් පුද්ගලික දත්තයක් ඉතිරි වී ඇති බවක් script එකට පෙනුනහොත් එය වාර්තාව නොසාදා
  නවතී. එසේ වුවද බෙදාගැනීමට පෙර ගොනු විවෘත කර කියවා බලන්න.
* ඒවා ඔබේ ✅/⚠️/❌ සටහන් සමඟ GitHub issue එකකට ("Hardware test report" template) අමුණන්න.

## 8. ගැටලුවක් ඇත්නම්

| ගැටලුව | කළ යුතු දේ |
| --- | --- |
| Dashboard විවෘත නොවේ | `/etc/init.d/bandwatch restart` ඉන්පසු `logread -e bandwatch` |
| Password අමතක විය | `bandwatch passwd` |
| Port 8787 වෙනත් වැඩසටහනක් භාවිතා කරයි | `sh install.sh --port 8790` |
| තොරතුරු වාර්තාවක් අවශ්‍යයි | `bandwatch-diag` (ගොනුව `/tmp` හි) |

## 9. ඉවත් කිරීම / ප්‍රතිසාධනය (Recovery)

```sh
cd /tmp/bandwatch-0.1.0-beta.1
sh uninstall.sh            # වැඩසටහන ඉවත් කරයි, දත්ත තබා ගනී
sh uninstall.sh --purge    # සියලු BandWatch දත්ත ද මකා දමයි
```

* `--purge` ආරක්ෂාව සඳහා `/etc/bandwatch` වැනි සැබෑ BandWatch ෆෝල්ඩරයක් පමණක්
  මකයි. ෆෝල්ඩරය symbolic link එකක් නම්, පද්ධති ස්ථානයක ඇත්නම් හෝ BandWatch
  නොසෑදූ ගොනු එහි ඇත්නම්, එය **කිසිවක් නොමකා** නවතී සහ හේතුව පෙන්වයි.

* ස්ථාපකය පෙර අනුවාදයේ backup එකක් `/tmp/bandwatch-rollback-*.tar.gz` ලෙස සාදයි.
* Router එකේ කිසියම් ගැටලුවක් ඇත්නම්: BandWatch ඉවත් කර (`uninstall.sh`),
  අවශ්‍ය නම් පියවර 0 හි backup එක LuCI → System → Backup → **Restore** මගින් යොදන්න.
* `/tmp/bandwatch-0.1.0-beta.1` ෆෝල්ඩරය reboot එකකින් මැකී ඇත්නම්, archive එක නැවත
  `/tmp` වෙත පිටපත් කර `tar -xzf` කරන්න.
