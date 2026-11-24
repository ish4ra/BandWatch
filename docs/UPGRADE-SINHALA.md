# BandWatch යාවත්කාලීන කිරීම (0.1.0-beta.1 → 0.1.0-beta.2) — සිංහල

**beta.1 ඉවත් කිරීමට (uninstall) හෝ කිසිම දත්තයක් මකා දැමීමට අවශ්‍ය නැත.**
යාවත්කාලීන කිරීමෙන් පසුවත් පහත සියල්ල එලෙසම පවතී:

* traffic ඉතිහාසය (database එක)
* සැකසුම් (settings), උපාංග නම් (aliases), retention සහ quota සැකසුම්
* dashboard මුරපදය (password) සහ LuCI සම්බන්ධතාවය
* aria2 සැකසුම් සහ RPC secret
* `/etc/config/bandwatch` (port, HTTPS සැකසුම්)

English version: [UPGRADE.md](UPGRADE.md)

## 1. Download කිරීම

[v0.1.0-beta.2 release](https://github.com/ish4ra/BandWatch/releases/tag/v0.1.0-beta.2)
පිටුවෙන් (GitHub ගිණුමක් අවශ්‍ය නැත) මෙම ගොනු දෙක download කරන්න:

* `bandwatch-0.1.0-beta.2-openwrt.tar.gz`
* `SHA256SUMS`

ගොනුව නිවැරදිදැයි බලන්න — Windows PowerShell හි:

```
Get-FileHash bandwatch-0.1.0-beta.2-openwrt.tar.gz
```

පෙන්වන අගය `SHA256SUMS` ගොනුවේ ඇති අගයට **සම්පූර්ණයෙන්ම සමාන** විය යුතුයි.

## 2. Router backup (මිනිත්තු 1)

LuCI → **System → Backup / Flash Firmware → Generate archive** ඔබා backup
ගොනුව පරිගණකයේ සුරකින්න. (BandWatch දත්ත ඉවත් නොවේ; මෙය අමතර ආරක්ෂාවකි.)

## 3. යාවත්කාලීන කිරීම (පළමු ස්ථාපනය කළ ආකාරයටමයි)

පරිගණකයේ (PowerShell, download ෆෝල්ඩරය තුළ):

```
scp -O bandwatch-0.1.0-beta.2-openwrt.tar.gz root@192.168.1.1:/tmp/
```

Router එකේ (`ssh root@192.168.1.1`):

```
cd /tmp && tar -xzf bandwatch-0.1.0-beta.2-openwrt.tar.gz && cd bandwatch-0.1.0-beta.2 && sh install.sh
```

Installer එක `existing installation 0.1.0-beta.1 will be upgraded (data kept)`
ලෙස පෙන්වයි. අවසානයේ `BandWatch 0.1.0-beta.2 is installed.` පෙනේ නම්
යාවත්කාලීන කිරීම සාර්ථකයි. ගැටලුවක් ආවොත් installer එක ස්වයංක්‍රීයව beta.1
නැවත ස්ථාපනය කරයි. **මුරපදය වෙනස් නොවේ** — පැරණි මුරපදයම භාවිතා කරන්න.

(`192.168.1.1` වෙනුවට ඔබේ router ලිපිනය යොදන්න.)

## 4. පරීක්ෂා කිරීම

Dashboard එක විවෘත කර **Ctrl+F5** ඔබා පිටුව එක් වරක් reload කරන්න.
ඉන්පසු **Health** පිටුවට යන්න:

* **Database & history** කොටසේ *Started: first start after upgrade* ලෙස පෙනේ.
* මුල් මිනිත්තු කිහිපයේ "*Earlier history is stored … saved for the first time
  in about N s*" ලෙස පෙන්වීම **සාමාන්‍යයි**. එය දෝෂයක් නොවේ.

## 5. Diagnostic Report එක (එක් ක්ලික් එකක්)

SSH විධාන, scripts හෝ reboot කිරීම් **අවශ්‍ය නැත**.

1. Dashboard එකේ **Health** (හෝ **Settings**) පිටුවට යන්න.
2. **Download Diagnostic Report** බොත්තම ඔබන්න.
3. `bandwatch-report-0.1.0-beta.2-….zip` ගොනුවක් download වේ.
4. ZIP එක විවෘත කර `SUMMARY.txt` කියවන්න — පෞද්ගලික තොරතුරු (නම්, ලිපින)
   නොමැති බව ඔබම තහවුරු කරගන්න.
5. එම ZIP ගොනුව maintainer වෙත යවන්න (හෝ GitHub issue එකකට attach කරන්න).

මෙම report එකේ **මුරපද, secrets, IP ලිපින, MAC ලිපින, උපාංග නම්, Wi-Fi නම්,
URL හෝ download ගොනු නම් කිසිවක් නැත**. එය router එකේම සාදා ඔබේ බ්‍රවුසරයට
පමණක් download වේ — BandWatch කිසිවක් අන්තර්ජාලයට යවන්නේ නැත. යැවීමට පෙර
ZIP එක විවෘත කර `SUMMARY.txt` කියවිය හැක.

හොඳම ප්‍රතිඵල සඳහා: යාවත්කාලීන කිරීමෙන් **පැය 24කට පමණ පසු** report එක
download කරන්න (එවිට CPU/memory සාරාංශ සහ database සුරැකීම් තහවුරු වී ඇත).

## beta.1 වෙත ආපසු යාම (rollback)

`bandwatch-0.1.0-beta.1` ෆෝල්ඩරයේ `sh install.sh` නැවත ධාවනය කරන්න.
ඉතිහාසය සහ සැකසුම් එලෙසම පවතී.
