# BandWatch — ස්ථාපනය සහ පරීක්ෂණ මාර්ගෝපදේශය (සිංහල)

මෙය BandWatch **beta** අනුවාදය (v0.1.0-beta.1) පළමු වරට router එකක
පරීක්ෂා කරන අය සඳහා කෙටි මාර්ගෝපදේශයකි. විධාන (commands) ඉංග්‍රීසියෙන්ම
ඇතුළත් කරන්න.

> **වැදගත්:** මෙය beta මෘදුකාංගයකි. ස්ථාපනයට පෙර LuCI → System →
> Backup / Flash Firmware → **Generate archive** මගින් router සැකසුම්වල
> backup එකක් ගන්න. BandWatch firewall, DNS, DHCP, Wi-Fi හෝ routing
> සැකසුම් කිසිවක් වෙනස් නොකරයි.

## 1. අවශ්‍ය දේ

* OpenWrt 25.12 (උදා: Arcadyan AW1000) හෝ 24.10
* router එකට SSH ප්‍රවේශය (Windows PowerShell හි `ssh` සහ `scp` භාවිතා කළ හැක)
* router එකේ අවම වශයෙන් 10 MB පමණ නිදහස් ඉඩ (Python ඇතුළුව)

## 2. ගොනු router එකට පිටපත් කිරීම

GitHub හි ලැබෙන `bandwatch-beta` artifact එක download කර unzip කරන්න.
ඉන්පසු ඔබේ පරිගණකයේ:

```sh
scp -O bandwatch-0.1.0-beta.1-openwrt.tar.gz root@192.168.1.1:/tmp/
```

(`192.168.1.1` වෙනුවට ඔබේ router ලිපිනය යොදන්න.)

## 3. පූර්ව පරීක්ෂාව (කිසිවක් වෙනස් නොකරයි)

```sh
ssh root@192.168.1.1
cd /tmp
tar -xzf bandwatch-0.1.0-beta.1-openwrt.tar.gz
cd bandwatch-0.1.0-beta.1
sh install.sh --dry-run
```

`[FAIL]` පේළියක් පෙන්වුවහොත් එහි සඳහන් විධානය ක්‍රියාත්මක කරන්න.
උදාහරණයක් ලෙස Python නොමැති නම්:

```sh
apk update && apk add python3-light python3-sqlite3 python3-urllib
```

## 4. ස්ථාපනය

```sh
sh install.sh
```

අවසානයේ dashboard ලිපිනය සහ **මුරපදය (Password)** පෙන්වයි — එය ලියා
තබා ගන්න. කිසියම් පියවරක් අසාර්ථක වුවහොත් ස්ථාපකය ස්වයංක්‍රීයව පෙර
තත්ත්වයට (rollback) පත් කරයි.

Dashboard විවෘත කිරීම:

* බ්‍රවුසරයේ `http://192.168.1.1:8787/`
* හෝ LuCI → **Status → BandWatch** (මුරපදයක් නොමැතිව ඇතුළු වේ)

මුරපදය වෙනස් කිරීමට: `bandwatch passwd`

## 5. පරීක්ෂා කළ යුතු දේ

1. **Overview** පිටුවේ Download/Upload වේගය තත්පර 2–3 කට වරක් යාවත්කාලීන වේද?
2. පරිගණකයෙන් speedtest එකක් කරන විට BandWatch වේගය ආසන්න වශයෙන් සමානද?
3. **Health → Collectors** හි WAN = *Working* ද? WAN interface නම `wwan0` ද?
4. Bandix ස්ථාපනය කර ඇත්නම් **Devices** පිටුවේ උපාංග සහ ඒවායේ භාවිතය පෙනේද?
   (Download/Upload මාරු වී ඇති බවක් පෙනේ නම් වාර්තා කරන්න.)
5. aria2 භාවිතා කරන්නේ නම් **Settings → aria2** හි RPC secret එක ඇතුළත් කර,
   **aria2** පිටුවේ download වේගය පෙනේදැයි බලන්න.
6. **History** පිටුවේ Today / This month එකතුව vnStat සමඟ සසඳන්න (“Compare” බොත්තම).
7. router එක reboot කර පැය කිහිපයකට පසු History හි දත්ත තවමත් තිබේද?
8. ජංගම දුරකථනයෙන් dashboard එක හොඳින් පෙනේද?
9. `top` විධානයෙන් BandWatch CPU භාවිතය (සාමාන්‍යයෙන් 2% ට අඩු විය යුතුයි) සහ
   මතකය (30 MB ට අඩු විය යුතුයි) පරීක්ෂා කරන්න.

සම්පූර්ණ ලැයිස්තුව: [HARDWARE-TESTING.md](HARDWARE-TESTING.md)

## 6. ගැටලුවක් ඇත්නම්

```sh
bandwatch-diag
```

මෙය `/tmp/bandwatch-diag-....txt` ගොනුවක් සාදයි. MAC/IP ලිපින, hostnames
සහ මුරපද ඉවත් කර ඇත. බෙදාගැනීමට පෙර ගොනුව කියවා බලා GitHub issue එකකට
අමුණන්න. ලොග් බැලීමට: `logread -e bandwatch`

## 7. ඉවත් කිරීම

```sh
cd /tmp/bandwatch-0.1.0-beta.1
sh uninstall.sh            # වැඩසටහන ඉවත් කරයි, දත්ත තබා ගනී
sh uninstall.sh --purge    # සියලු දත්ත ද මකා දමයි
```
