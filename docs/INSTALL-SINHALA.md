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

(`192.168.1.1` වෙනුවට ඔබේ router ලිපිනය යොදන්න. Windows හි ගොනු නිවැරදිදැයි
බැලීමට: `Get-FileHash bandwatch-0.1.0-beta.1-openwrt.tar.gz` — ප්‍රතිඵලය
`SHA256SUMS` හි ඇති අගයට සමාන විය යුතුයි.)

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
12. Router **reboot** කරන්න. පසුව BandWatch ස්වයංක්‍රීයව ආරම්භ වේද? පෙර දත්ත තිබේද?
13. ජංගම දුරකථනයෙන් dashboard එක හොඳින් පෙනේද? Dark/Light theme වෙනස් වේද?
14. **WAN පැත්තෙන් ප්‍රවේශය නොමැති බව**: router එකේ Wi-Fi නොව, දුරකථනයේ
    mobile data භාවිතයෙන් `http://<router WAN IP>:8787/` විවෘත කිරීමට උත්සාහ
    කරන්න — එය **විවෘත නොවිය යුතුයි**.

## 5. තොරතුරු එකතු කිරීම (මිනිත්තු 7)

```sh
sh /tmp/bandwatch-test-round.sh after
sh /tmp/bandwatch-test-round.sh perf
sh /tmp/bandwatch-test-round.sh report
```

* `perf` විනාඩි 5 ක් ගනී — ඒ කාලයේ dashboard එක **වසා** තබන්න.
  ඉලක්කය: සාමාන්‍ය CPU < 2%, මතකය (RSS) < 30 MB.
* `after` මගින් router සැකසුම් වෙනස් නොවූ බව පරීක්ෂා කරයි
  ("OK: ... configuration unchanged").
* `report` මගින් `/tmp/bandwatch-test-report-....tar.gz` සාදයි. එය පරිගණකයට ගන්න:

```sh
scp -O root@192.168.1.1:/tmp/bandwatch-test-report-*.tar.gz .
```

MAC/IP ලිපින, උපාංග නම් සහ මුරපද ඉවත් කර ඇත. බෙදාගැනීමට පෙර ගොනු කියවා
බලා, ඔබේ ✅/⚠️/❌ සටහන් සමඟ GitHub issue එකකට ("Hardware test report" template)
අමුණන්න. **Router reboot කිරීමට පෙර** `report` ක්‍රියාත්මක කරන්න (`/tmp` reboot
එකේදී මැකේ).

## 6. ගැටලුවක් ඇත්නම්

| ගැටලුව | කළ යුතු දේ |
| --- | --- |
| Dashboard විවෘත නොවේ | `/etc/init.d/bandwatch restart` ඉන්පසු `logread -e bandwatch` |
| Password අමතක විය | `bandwatch passwd` |
| Port 8787 වෙනත් වැඩසටහනක් භාවිතා කරයි | `sh install.sh --port 8790` |
| තොරතුරු වාර්තාවක් අවශ්‍යයි | `bandwatch-diag` (ගොනුව `/tmp` හි) |

## 7. ඉවත් කිරීම / ප්‍රතිසාධනය (Recovery)

```sh
cd /tmp/bandwatch-0.1.0-beta.1
sh uninstall.sh            # වැඩසටහන ඉවත් කරයි, දත්ත තබා ගනී
sh uninstall.sh --purge    # සියලු BandWatch දත්ත ද මකා දමයි
```

* ස්ථාපකය පෙර අනුවාදයේ backup එකක් `/tmp/bandwatch-rollback-*.tar.gz` ලෙස සාදයි.
* Router එකේ කිසියම් ගැටලුවක් ඇත්නම්: BandWatch ඉවත් කර (`uninstall.sh`),
  අවශ්‍ය නම් පියවර 0 හි backup එක LuCI → System → Backup → **Restore** මගින් යොදන්න.
* `/tmp/bandwatch-0.1.0-beta.1` ෆෝල්ඩරය reboot එකකින් මැකී ඇත්නම්, archive එක නැවත
  `/tmp` වෙත පිටපත් කර `tar -xzf` කරන්න.
