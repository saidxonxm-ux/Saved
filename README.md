# 🤖 Media Bot

Link orqali video/foto yuklash, qo'shiq qidirish, krujochka, Shazam, 3 til (UZ/RU/EN), majburiy obuna va admin panel.

## Imkoniyatlar
- 🔗 Link → video/foto (Instagram, TikTok, YouTube, X, Pinterest va 1000+ sayt), karusel albom sifatida
- 🎵 Qo'shiq nomi yozilsa → qidiruv natijalari → MP3
- 🎬 Video → avto-krujochka + musiqa aniqlash (sozlanadi: /settings)
- 🎤 Audio/ovozli xabar → Shazam, «To'liq qo'shiqni yuklash» tugmasi
- ⚡ Bir xil link qayta yuborilsa — darhol (file_id kesh)
- 📌 Majburiy obuna: cheksiz kanal, ommaviy/yopiq, forward orqali qo'shish
- 🛠 /admin: statistika, reklama (tugma, til bo'yicha, to'xtatish), ban, foydalanuvchiga xabar, CSV, texnik ish rejimi, kunlik limit

## Railway
1. New Project → Deploy from GitHub repo
2. Variables: `BOT_TOKEN`, `ADMIN_IDS`, `DATA_DIR=/data`
3. Volume → mount path `/data`
4. Replicas = 1

Majburiy obuna uchun botni kanalga **admin** qiling.
Instagram/YouTube bloklasa: `cookies.txt` ni volume'ga qo'ying va `COOKIES_FILE=/data/cookies.txt` bering.

## Qo'shimcha o'zgaruvchilar (ixtiyoriy)
- `GROQ_API_KEY` — ovozli xabarni aniq matnga o'girish (console.groq.com, bepul)
- `PROXY_URL` — YouTube bloklasa, yashash proksisi (`http://user:pass@host:port`)
- `COOKIES_B64` — cookies.txt ning base64 ko'rinishi (Instagram/YouTube uchun)
- `PREMIUM_STARS`, `PREMIUM_DAYS`, `MAX_VIDEO_MIN`, `MAX_JOBS`
