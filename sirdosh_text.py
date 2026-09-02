"""
sirdosh_text — SIRDOSH uchun TOZA matn yordamchilari (DB, Telegram, Gemini'ga bog'liq emas).
bot.py dan ajratilgan (modullashtirish 1-bosqich). Faqat str -> natija funksiyalari.
Sinash: python3 -c "import sirdosh_text" — hech qanday env/kutubxona kerak emas.
"""
import html as _html
import re


_CYR2LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
})


def _detect_reply_override(text: str) -> str | None:
    """Foydalanuvchi shu xabarda javob turini so'ragan bo'lsa — aniqlaymiz.
    Masalan: "matnda javob ber" -> text, "ovozli xabarda javob ber" -> voice.
    Kirillcha yozilgan bo'lsa ham tushunadi."""
    t = text.lower().translate(_CYR2LAT)
    text_kw = (
        "matnda javob", "matn bilan javob", "matnda ber", "matnda yoz",
        "yozib javob", "yozma javob", "matnli javob", "tekstda javob", "matnda ayt",
        "matn tarzida", "tekst tarzida", "matn shaklida", "matn ko'rinishida",
        "matnda qaytar", "yozuvda javob",
    )
    voice_kw = (
        "ovozli javob", "ovozda javob", "ovozli xabarda", "ovoz bilan javob",
        "golosda javob", "golos bilan javob", "audio javob", "ovozli qilib", "ovozda ayt",
        "ovozli xabar tarzida", "ovozli xabar bilan", "ovozli xabar qilib",
        "ovoz tarzida", "ovoz shaklida", "ovozda qaytar", "ovozda ber", "golosda ayt",
    )
    if any(k in t for k in text_kw):
        return "text"
    if any(k in t for k in voice_kw):
        return "voice"
    return None


def _is_edit_instruction(text: str) -> bool:
    """Matn rasmni TAHRIRLASH buyrug'imi? (savol/tahlil emas)"""
    t = text.lower().translate(_CYR2LAT)
    edit_kw = (
        "o'zgartir", "ozgartir", "tahrir", "tahrirla", "edit", "qo'sh", "qosh",
        "olib tashla", "o'chir", "ochir", "remove", "add", "fon", "background",
        "orqa fon", "rang", "rangini", "color", "uslub", "style", "stil",
        "chiroyli", "chiroyliroq", "yaxshila", "enhance", "sifat", "yorug'",
        "qorong'i", "kattalashtir", "kichiklashtir", "kes", "crop", "ko'zoynak",
        "soch", "kiyim", "ko'ylak", "kulgili", "anime", "rasm qilib", "surat qil",
        "qora oq", "qora-oq", "eskirtir", "yoshartir", "qilib ber", "qilib yubor",
        "ko'k", "kok", "qizil", "yashil", "sariq", "oq qil", "qora qil", "kul rang",
        "chap", "o'ng", "yuqori", "past", "kattaroq", "kichikroq", "yorqin", "xira",
    )
    return any(k in t for k in edit_kw)


def _is_analysis_question(text: str) -> bool:
    """Matn rasmni TUSHUNTIRISH/O'QISH so'rovimi?"""
    t = text.lower().translate(_CYR2LAT)
    q_kw = (
        "nima", "kim", "necha", "qancha", "o'qi", "oqi", "tahlil", "chek",
        "matn", "yozilgan", "tarjima", "nima deb", "ayt", "tushuntir", "bu qanaqa",
    )
    return t.strip().endswith("?") or any(k in t for k in q_kw)


def _mentions_image(text: str) -> bool:
    t = text.lower().translate(_CYR2LAT)
    kw = ("rasm", "surat", "foto", "photo", "image", "logo", "dizayn", "banner",
          "shunday qil", "shunga o'xshash", "shunga oxshash", "buni", "bunga")
    return any(k in t for k in kw)


def _wants_pro(text: str) -> bool:
    """"Professional", "sifatli", "pro" desa — kuchli (qimmatroq) rasm modeli."""
    t = text.lower().translate(_CYR2LAT)
    return any(k in t for k in ("professional", "profesional", "sifatli", "yuqori sifat", " pro ", "mukammal"))


_TEMPLATE_SAVE_RE = re.compile(r"^\s*(?:shablon|template)\s*[:\-]?\s*(?:sifatida\s+saqla\s*|saqla\s*)?(.*)$", re.I)


def _parse_template_save(caption: str) -> str | None:
    """Caption 'shablon: banner' / 'shablon banner' / 'shablon sifatida saqla banner' -> 'banner'."""
    c = caption.strip().translate(_CYR2LAT)
    m = _TEMPLATE_SAVE_RE.match(c)
    if not m:
        return None
    name = m.group(1).strip().strip(":").strip()
    return (name or "asosiy").lower()


def _find_template_ref(text: str, names: list[str]) -> str | None:
    """Matnda 'shablon' + saqlangan nom bo'lsa — nomni qaytaradi; bitta shablon bo'lsa uni."""
    t = text.lower().translate(_CYR2LAT)
    if "shablon" not in t and "template" not in t:
        return None
    for n in names:
        if n and n in t:
            return n
    return names[0] if len(names) == 1 else None


_ERROR_MARKERS = (
    "traceback (most recent", "exception", "error:", "errno", "at line", ", line ", "stack trace",
    "stacktrace", "clienterror", "typeerror", "nameerror", "valueerror", "keyerror", "attributeerror",
    "syntaxerror", "indexerror", "importerror", "modulenotfound", "nullpointer", "undefined is not",
    "cannot read prop", "npm err", "build failed", "exit code", "fatal:", "panic:", "segmentation fault",
    "status_code", "http 500", "http 400", "http 404", "500 internal", "invalid_argument", "unhandled",
    "failed to", "error 4", "error 5", "e/androidruntime", "gradle", "compilation failed",
)


def _looks_like_error(text: str) -> bool:
    """Xabar traceback/log/xato matniga o'xshaydimi? (o'zbekcha 'xato' so'zi hisobga olinmaydi)"""
    if len(text) < 25:
        return False
    t = text.lower()
    return any(m in t for m in _ERROR_MARKERS)


# Eslatma: qisqa tokenlar ("ui", "ux") bu ro'yxatda EMAS — ular substring sifatida
# "bui prompt" kabi so'zlarga tushib ketadi; ular _DESIGN_WORDS_RE da so'z chegarasi bilan tekshiriladi.
_DESIGN_STRONG = ("claude design", "design prompt", "dizayn prompt",
                  "dizayn uchun prompt", "landing uchun prompt", "sayt uchun prompt")
_DESIGN_WORDS_RE = re.compile(
    r"\b(landing|lending|sayt|website|web-?site|ui|ux|dizayn|design|figma|lovable|v0|framer|"
    r"ekran|screen|mockup|maket|interfeys|interface|banner|logo)\b"
)


def _looks_like_design_request(text: str) -> bool:
    """"Claude Design uchun prompt ber", "landing page prompt" kabi so'rovlarni taniydi."""
    t = text.lower().translate(_CYR2LAT)
    if any(k in t for k in _DESIGN_STRONG):
        return True
    return "prompt" in t and bool(_DESIGN_WORDS_RE.search(t))


_FENCE_RE = re.compile(r"```([\w+.-]*)[ \t]*\n(.*?)```", re.S)


def _md_inline_html(t: str) -> str:
    """Oddiy matn qismini HTML'ga: escape + `code`, **bold**, # sarlavha."""
    t = _html.escape(t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", t, flags=re.M)
    return t


def _md_to_html(text: str) -> str:
    """Model javobidagi Markdown'ni Telegram HTML'ga o'giradi (kod bloklari saqlanadi)."""
    out, pos = [], 0
    for m in _FENCE_RE.finditer(text):
        out.append(_md_inline_html(text[pos:m.start()]))
        lang, code = m.group(1), _html.escape(m.group(2).rstrip("\n"))
        cls = f' class="language-{lang}"' if lang else ""
        out.append(f"<pre><code{cls}>{code}</code></pre>")
        pos = m.end()
    out.append(_md_inline_html(text[pos:]))
    return "".join(out)


def _split_chunks(text: str, limit: int = 3500) -> list[str]:
    """Matnni Telegram limitiga bo'ladi — KOD BLOKINI O'RTASIDAN KESMAYDI
    (kerak bo'lsa blokni yopib, keyingi bo'lakda qayta ochadi)."""
    chunks, cur, size, in_code, lang = [], [], 0, False, ""
    for line in text.split("\n"):
        st = line.strip()
        is_fence = st.startswith("```")
        if size + len(line) + 1 > limit and cur:
            if in_code and not is_fence:
                cur.append("```")
                chunks.append("\n".join(cur))
                cur, size = ["```" + lang], len(lang) + 4
            else:
                chunks.append("\n".join(cur))
                cur, size = [], 0
        if is_fence:
            if not in_code:
                in_code, lang = True, st[3:].strip()
            else:
                in_code = False
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks
