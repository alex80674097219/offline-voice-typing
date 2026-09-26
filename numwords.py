# -*- coding: utf-8 -*-
"""Числівники словами -> цифри, після розпізнавання. Українська, російська,
англійська, німецька.

Parakeet пише числа словами ("тринадцять відсотків", "сім тисяч п'ятсот").
Перетворюємо обережно, щоб "один момент" не став "1 момент":

  - значення >= 10                    "двадцять років" -> "20 років"
  - або число з 2+ слів               "дві тисячі"     -> "2000"
  - або далі йде одиниця виміру       "два метри"      -> "2 метри"
  - інакше лишаємо словом             "через два дні"  -> без змін

Порядкові ("п'ятого", "двадцятий") не чіпаємо. Ніяких запитів у мережу,
нуль мілісекунд.
"""
import re

_UNITS = {
    # uk
    "нуль": 0, "один": 1, "одна": 1, "одне": 1, "одну": 1, "одного": 1, "одній": 1,
    "два": 2, "дві": 2, "двох": 2, "двом": 2, "двома": 2,
    "три": 3, "трьох": 3, "трьом": 3, "трьома": 3,
    "чотири": 4, "чотирьох": 4, "чотирьом": 4,
    "п'ять": 5, "пять": 5, "п'яти": 5, "п'ятьох": 5,
    "шість": 6, "шести": 6, "шістьох": 6,
    "сім": 7, "семи": 7, "сімох": 7,
    "вісім": 8, "восьми": 8, "вісьмох": 8,
    "дев'ять": 9, "девять": 9, "дев'яти": 9,
    "десять": 10, "десяти": 10,
    "одинадцять": 11, "одинадцяти": 11, "дванадцять": 12, "дванадцяти": 12,
    "тринадцять": 13, "тринадцяти": 13, "чотирнадцять": 14, "чотирнадцяти": 14,
    "п'ятнадцять": 15, "п'ятнадцяти": 15, "шістнадцять": 16, "шістнадцяти": 16,
    "сімнадцять": 17, "сімнадцяти": 17, "вісімнадцять": 18, "вісімнадцяти": 18,
    "дев'ятнадцять": 19, "дев'ятнадцяти": 19,
    # ru
    "ноль": 0, "одно": 1, "одной": 1, "одном": 1,
    "две": 2, "двух": 2, "трех": 3, "трёх": 3, "четыре": 4, "четырех": 4, "четырёх": 4,
    "пяти": 5, "шесть": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8,
    "девяти": 9, "десяти": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19,
    # en
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
    # de
    "null": 0, "eins": 1, "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
    "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15,
    "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19,
}
_TENS = {
    "двадцять": 20, "двадцяти": 20, "тридцять": 30, "тридцяти": 30, "сорок": 40,
    "сорока": 40, "п'ятдесят": 50, "п'ятдесяти": 50, "шістдесят": 60, "шістдесяти": 60,
    "сімдесят": 70, "сімдесяти": 70, "вісімдесят": 80, "вісімдесяти": 80,
    "дев'яносто": 90, "дев'яноста": 90, "п'ятьдесят": 50, "п'ятдесять": 50, "пятдесят": 50,
    "двадцать": 20, "двадцати": 20, "тридцать": 30, "тридцати": 30,
    "пятьдесят": 50, "пятидесяти": 50, "шестьдесят": 60, "шестидесяти": 60,
    "семьдесят": 70, "семидесяти": 70, "восемьдесят": 80, "восьмидесяти": 80,
    "девяносто": 90, "девяноста": 90,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
    "zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50,
    "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90,
}
_HUNDREDS = {
    "сто": 100, "ста": 100, "двісті": 200, "двохсот": 200, "триста": 300, "трьохсот": 300,
    "чотириста": 400, "п'ятсот": 500, "п'ятисот": 500, "шістсот": 600, "сімсот": 700,
    "вісімсот": 800, "дев'ятсот": 900,
    "двести": 200, "двухсот": 200, "трехсот": 300, "четыреста": 400, "пятьсот": 500,
    "пятисот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900, "девятсот": 900,
}
_MULT_HUNDREDS = {"hundred": 100, "hundert": 100}
_SCALES = {
    "тисяча": 1000, "тисячі": 1000, "тисяч": 1000, "тисячу": 1000, "тисячами": 1000,
    "тысяча": 1000, "тысячи": 1000, "тысяч": 1000, "тысячу": 1000,
    "мільйон": 10**6, "мільйони": 10**6, "мільйонів": 10**6,
    "миллион": 10**6, "миллиона": 10**6, "миллионов": 10**6,
    "thousand": 1000, "million": 10**6, "tausend": 1000,
}
_HALF = {"півтора": 1.5, "півтори": 1.5, "полтора": 1.5, "полторы": 1.5}

# Після числа - одиниця виміру: тоді переводимо навіть "два" і "три".
_UNIT_AFTER = re.compile(
    r"^(кіловат|киловат|кв|кw|kw|ват|вт|мвт|квт|ампер|вольт|герц|"
    r"відсот|процент|%|грн|гривн|євро|евро|долар|доллар|франк|chf|eur|usd|"
    r"метр|м\b|см|мм|км|кілометр|километр|квадрат|кв\.|"
    r"тонн|кг|кілограм|килограм|грам|літр|литр|"
    r"штук|шт|одиниц|единиц|модул|панел|батаре|інвертор|инвертор|"
    r"годин|часов|часа|хвилин|минут|секунд|днів|дней|тижн|недел|місяц|месяц|рок|лет|год|"
    r"ранку|утра|вечора|вечера|"
    r"percent|kw|kwh|volt|amp|meter|km|kg|hour|minute|day|week|month|year|"
    r"prozent|stunde|minute|tag|woche|monat|jahr|stück|meter|kilo)",
    re.IGNORECASE)

# Далі порядковий числівник ("двадцять шостого року") - це дата, не число.
_ORDINAL_AFTER = re.compile(
    r"^[\w'’]+(ого|ому|ий|ій|ої|ою|ім|ый|ой|ая|ое|ые|ом|ым|ую)\b|"
    r"^(first|second|third|\w+th)\b|^\w+(ste|sten|ster|stes)\b", re.IGNORECASE | re.UNICODE)

_WORD = re.compile(r"[a-zA-Zа-яА-ЯіїєґІЇЄҐёЁäöüßÄÖÜ'’]+|\d+")


def _de_split(w):
    """fünfundzwanzig -> [20, 5]; siebenhundert -> [7*100]; zweitausend -> [2*1000]."""
    m = re.fullmatch(r"([a-zäöü]+)und([a-zäöü]+)", w)
    if m and m.group(1) in _UNITS and m.group(2) in _TENS:
        return [("t", _TENS[m.group(2)]), ("u", _UNITS[m.group(1)])]
    m = re.fullmatch(r"([a-zäöü]+)(hundert|tausend)", w)
    if m and m.group(1) in _UNITS:
        v = _UNITS[m.group(1)]
        return [("u", v), ("H", 100) if m.group(2) == "hundert" else ("s", 1000)]
    return None


def _classify(word):
    w = word.lower().replace("’", "'")
    if w.isdigit():
        return ("d", int(w))
    if w in _HALF:
        return ("half", _HALF[w])
    if w in _UNITS:
        return ("u", _UNITS[w])
    if w in _TENS:
        return ("t", _TENS[w])
    if w in _HUNDREDS:
        return ("h", _HUNDREDS[w])
    if w in _MULT_HUNDREDS:
        return ("H", _MULT_HUNDREDS[w])
    if w in _SCALES:
        return ("s", _SCALES[w])
    return None


def _value(tokens):
    """[("u",7),("s",1000),("h",500)] -> 7500. Повертає None, якщо
    послідовність не схожа на число (дві одиниці поспіль тощо)."""
    total = 0
    cur = 0
    last_kind = None
    for kind, v in tokens:
        if kind == "half":
            if cur or last_kind:
                return None
            cur = v
        elif kind == "u":
            if last_kind in ("u", "half"):
                return None
            if last_kind == "t" and v >= 10:
                return None
            cur += v
        elif kind == "t":
            if last_kind in ("t", "u", "half"):
                return None
            cur += v
        elif kind == "d":                     # цифри вже є: "23 тысячи"
            if last_kind is not None:
                return None
            cur = v
        elif kind == "h":                     # "п'ятсот" - самостійне слово
            if last_kind in ("h", "H", "u", "t", "d"):
                return None                   # "два семьсот", "пятьдесят сто" - не число
            cur += v
        elif kind == "H":                     # "two hundred", "zweihundert"
            if last_kind in ("h", "H", "t"):
                return None
            cur = (cur if cur else 1) * v
        elif kind == "s":
            if cur == 0:
                cur = 1
            total += cur * v
            cur = 0
        last_kind = kind
    total += cur
    return total


def _fmt(v):
    if isinstance(v, float) and not v.is_integer():
        return ("%.1f" % v).replace(".", ",")
    s = str(int(v))
    if len(s) >= 5:                            # 52000 -> 52 000
        s = "{:,}".format(int(v)).replace(",", " ")
    return s


def convert(text):
    if not text:
        return text
    out = []
    pos = 0
    words = list(_WORD.finditer(text))
    i = 0
    while i < len(words):
        # збираємо максимальну послідовність числових слів
        seq = []
        j = i
        while j < len(words):
            w = words[j].group(0)
            c = _classify(w)
            parts = None
            if c is None and re.search(r"[äöüa-z]", w.lower()):
                parts = _de_split(w.lower())
            if c is None and parts is None:
                break
            # між словами має бути лише пробіл/дефіс
            if seq and not re.fullmatch(r"[ \-]+", text[words[j-1].end():words[j].start()]):
                break
            seq.extend(parts if parts else [c])
            j += 1
        if not seq:
            i += 1
            continue
        val = _value(seq)
        nwords = j - i
        after = text[words[j-1].end():].lstrip()
        has_unit = bool(_UNIT_AFTER.match(after))
        if _ORDINAL_AFTER.match(after) and not has_unit:
            i = j
            continue
        before = text[:words[i].start()]
        # "один" сам по собі без одиниці - майже завжди не число ("один момент")
        lone_one = (nwords == 1 and seq[0][0] == "u" and seq[0][1] == 1)
        # "місяць-півтора", "годину-півтори" - діапазон, лишаємо словами
        hyphenated = before.endswith("-")
        # самі цифри без слів - нічого міняти; "мільйон" сам по собі - теж
        only_digits = all(k == "d" for k, _ in seq)
        lone_million = (nwords == 1 and seq[0][0] == "s" and seq[0][1] >= 10**6)
        ok = (val is not None and not lone_one and not hyphenated
              and not only_digits and not lone_million
              and (val >= 10 or nwords >= 2 or has_unit
                   or seq[0][0] == "half"))
        if ok:
            out.append(text[pos:words[i].start()])
            out.append(_fmt(val))
            pos = words[j-1].end()
        i = j
    out.append(text[pos:])
    return "".join(out)


if __name__ == "__main__":
    # python numwords.py  - швидка самоперевірка
    # python numwords.py dictate.log  - прогін по всіх фразах із лога
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1:
        changed = total = 0
        for l in open(sys.argv[1], encoding="utf-8", errors="replace"):
            if "] phrase " not in l or "-> " not in l:
                continue
            t = l.split("-> ", 1)[1].rstrip()
            c = convert(t)
            total += 1
            if c != t:
                changed += 1
                print("  %s\n    -> %s" % (t[:120], c[:120]))
        print("\nchanged %d of %d phrases" % (changed, total))
    else:
        for t in ("сім тисяч п'ятсот гривень", "через дві години", "один момент",
                  "Викторон восемь киловатт", "пятьдесят две тысячи",
                  "дві тисячі двадцять шостого року", "місяць-півтора",
                  "fünfundzwanzig Prozent", "two hundred fifty panels"):
            print("%-40s -> %s" % (t, convert(t)))
