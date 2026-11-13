"""行動記録の週間帳票（PDF）を生成する（Python標準ライブラリのみ）

- 指定期間を含む「月曜日〜日曜日」の7日間を1ページとする
- A4横。列は 日付 / 時刻（FROM〜TO）/ 行動 / メモ
- 日本語は PDF 標準の日本語フォント（平成角ゴシック W5, Adobe-Japan1）を参照する。
  フォントは埋め込まず、表示側（Acrobat / Chrome / Edge / macOS プレビュー等）のフォントで描画される
"""
import datetime
import zlib

# ---------------------------------------------------------------------------
# 最小限の PDF 書き出し
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = 841.89, 595.28   # A4横（pt）
FONT_NAME = "HeiseiKakuGo-W5"
ENCODING = "UniJIS-UCS2-HW-H"     # Unicode(UCS-2) → CID。英数字は半角幅

WEEK_JA = "月火水木金土日"
CATEGORY_JA = {"sleep": "睡眠", "meal": "食事", "medicine": "服薬", "out": "外出", "other": "その他"}
NO_END_CATEGORIES = {"medicine"}   # 時刻のみ表示する行動
# 出力日時は日本時間で記載する（サーバー・コンテナのタイムゾーンに依存しない。日本は夏時間なし）
JST = datetime.timezone(datetime.timedelta(hours=9), "JST")


def text_width(s, size):
    """文字列の描画幅（pt）。英数字・半角カナは半角、それ以外は全角とみなす"""
    w = 0
    for ch in s:
        o = ord(ch)
        w += 0.5 if o < 0x7F or 0xFF61 <= o <= 0xFF9F else 1.0
    return w * size


def encode_text(s):
    """PDF文字列（UTF-16BE の16進）。BMP外の文字は「〓」に置換"""
    s = "".join(ch if ord(ch) <= 0xFFFF else "〓" for ch in s)
    return "<" + s.encode("utf-16-be").hex().upper() + ">"


def fmt_num(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Page:
    """1ページ分の描画命令。座標は左上原点（pt）で受け取り、PDF座標に変換する"""

    def __init__(self):
        self.ops = []

    def _y(self, y):
        return PAGE_H - y

    def text(self, x, y, s, size=10, color=(0, 0, 0), align="left"):
        """y はベースライン位置"""
        if not s:
            return
        if align == "right":
            x -= text_width(s, size)
        elif align == "center":
            x -= text_width(s, size) / 2
        r, g, b = color
        self.ops.append(
            f"BT {fmt_num(r)} {fmt_num(g)} {fmt_num(b)} rg /F1 {fmt_num(size)} Tf "
            f"{fmt_num(x)} {fmt_num(self._y(y))} Td {encode_text(s)} Tj ET"
        )

    def line(self, x1, y1, x2, y2, width=0.5, gray=0):
        self.ops.append(
            f"{fmt_num(gray)} G {fmt_num(width)} w {fmt_num(x1)} {fmt_num(self._y(y1))} m "
            f"{fmt_num(x2)} {fmt_num(self._y(y2))} l S"
        )

    def fill_rect(self, x, y, w, h, gray):
        self.ops.append(f"{fmt_num(gray)} g {fmt_num(x)} {fmt_num(self._y(y + h))} {fmt_num(w)} {fmt_num(h)} re f")

    def content(self):
        return "\n".join(self.ops).encode("ascii")


def build_pdf(pages, title):
    """Page のリストから PDF バイト列を作る"""
    objs = []  # objs[i] が オブジェクト番号 i+1

    def add(body):
        objs.append(body)
        return len(objs)

    catalog = add(None)
    pages_id = add(None)
    font_desc = add(
        f"<< /Type /FontDescriptor /FontName /{FONT_NAME} /Flags 4 /FontBBox [-92 -250 1010 922] "
        "/ItalicAngle 0 /Ascent 752 /Descent -221 /CapHeight 737 /StemV 114 >>".encode()
    )
    cid_font = add(
        f"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /{FONT_NAME} "
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> "
        f"/FontDescriptor {font_desc} 0 R /DW 1000 /W [1 95 500 231 632 500] >>".encode()
    )
    font = add(
        f"<< /Type /Font /Subtype /Type0 /BaseFont /{FONT_NAME}-{ENCODING} /Encoding /{ENCODING} "
        f"/DescendantFonts [{cid_font} 0 R] >>".encode()
    )
    page_ids = []
    for page in pages:
        data = zlib.compress(page.content())
        stream = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
        page_ids.append(add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {fmt_num(PAGE_W)} {fmt_num(PAGE_H)}] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {stream} 0 R >>".encode()
        ))
    objs[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>".encode()
    )
    objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    now = datetime.datetime.now(JST).strftime("D:%Y%m%d%H%M%S+09'00'")
    info = add(
        f"<< /Title <FEFF{title.encode('utf-16-be').hex().upper()}> "
        f"/Producer (activity-log) /CreationDate ({now}) >>".encode()
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


# ---------------------------------------------------------------------------
# 帳票レイアウト
#   列: 日付 / 時刻（FROM〜TO）/ 行動 / メモ / 1日の感想 / 服薬時刻
#   「1日の感想」「服薬時刻」は日ごとに縦に結合したセル。服薬は行動の行には出さず服薬時刻の列に出す
# ---------------------------------------------------------------------------
MARGIN = 36
TABLE_TOP = 78
FOOTER_Y = PAGE_H - 18
TABLE_BOTTOM = PAGE_H - 34
HEADER_H = 20
PAGE_HEADER_Y = 60                   # ページヘッダ（1行）のベースライン
PAGE_HEADER_FONT = 12
PAD = 5                              # セル内の左右余白
MAX_FONT, MIN_FONT = 9.5, 5.0        # 1週が1ページに収まるよう、この範囲で文字サイズを自動調整
ROW_RATIO = 2.0                      # 行の高さ = 文字サイズ × 2.0
LINE_RATIO = 1.45                    # 折り返し文字の行間 = 文字サイズ × 1.45
# 列: (キー, 見出し, 幅)。感想は残り幅
COLUMNS = [("date", "日付", 74), ("time", "時刻（FROM〜TO）", 122), ("cat", "行動", 46), ("note", "メモ", 150),
           ("comment", "1日の感想", None), ("medicine", "服薬時刻", 108)]

SUN_COLOR, SAT_COLOR = (0.80, 0.15, 0.15), (0.15, 0.35, 0.80)
GRAY_TEXT = (0.6, 0.6, 0.6)


def week_ranges(date_from, date_to):
    """期間を含む月曜〜日曜の週の (月曜, 日曜) のリスト"""
    monday = date_from - datetime.timedelta(days=date_from.weekday())
    weeks = []
    while monday <= date_to:
        weeks.append((monday, monday + datetime.timedelta(days=6)))
        monday += datetime.timedelta(days=7)
    return weeks


def fmt_date(d, with_year=False):
    s = f"{d.year}/{d.month}/{d.day}" if with_year else f"{d.month}/{d.day}"
    return f"{s}（{WEEK_JA[d.weekday()]}）"


def parse_dt(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")


def rel_time_text(t, base_date):
    """base_date を基準にした時刻表示（当日「07:00」、前日「前日23:30」、翌日「翌07:00」、それ以外は日付付き）"""
    days = (t.date() - base_date).days
    hm = t.strftime("%H:%M")
    return {0: hm, -1: f"前日{hm}", 1: f"翌{hm}"}.get(days, f"{t.month}/{t.day} {hm}")


def time_range_text(start, end, base_date):
    """「23:30 〜 翌07:00」「前日23:30 〜 06:45」。終了が未入力なら「23:30 〜」"""
    s = rel_time_text(start, base_date)
    return f"{s} 〜" if end is None else f"{s} 〜 {rel_time_text(end, base_date)}"


SLEEP_DAY_START_HOUR = 18   # 睡眠は就寝時刻がこの時刻以降（かつ日をまたぐ）なら翌日の行動とする


def belongs_date(a, start, end):
    """記録を表示する日。睡眠は就寝時刻が 18:00 以降なら翌日、18:00 より前なら就寝した日。
    ただし 18:00 以降の就寝でも、起床が日をまたがない（24:00 より前）なら就寝した日"""
    if a["category"] == "sleep" and start.hour >= SLEEP_DAY_START_HOUR:
        next_day = start.date() + datetime.timedelta(days=1)
        if end is not None and end < datetime.datetime.combine(next_day, datetime.time()):
            return start.date()
        return next_day
    return start.date()


def fmt_duration(start, end):
    """所要時間「7時間30分」「45分」（画面の表示と同じ形式）"""
    minutes = round((end - start).total_seconds() / 60)
    h, m = divmod(minutes, 60)
    return f"{h}時間{f'{m}分' if m else ''}" if h else f"{m}分"


def fit_text(s, size, width):
    """列幅に収まらない場合は末尾を「…」で省略"""
    if text_width(s, size) <= width:
        return s
    while s and text_width(s + "…", size) > width:
        s = s[:-1]
    return s + "…"


def wrap_text(s, size, width):
    """列幅で折り返した行のリスト（改行も反映）"""
    lines = []
    for para in s.splitlines() or [""]:
        line = ""
        for ch in para:
            if line and text_width(line + ch, size) > width:
                lines.append(line)
                line = ""
            line += ch
        lines.append(line)
    return lines


class DayBlock:
    """1日分の表示内容"""

    def __init__(self, date, activities, medicines, comment):
        self.date = date
        self.activities = activities   # [(start, end, row)]（服薬以外）
        self.medicines = medicines     # [(start, row)]
        self.comment = comment or ""

    def layout(self, font, widths):
        """文字サイズ font での各セルの内容と、この日の高さを計算"""
        row_h, line_h = font * ROW_RATIO, font * LINE_RATIO
        self.font, self.row_h, self.line_h = font, row_h, line_h
        self.comment_lines = wrap_text(self.comment, font, widths["comment"] - PAD * 2) if self.comment else []
        self.medicine_lines = [
            fit_text(f"{start.strftime('%H:%M')} {a['note']}".strip(), font, widths["medicine"] - PAD * 2)
            for start, a in self.medicines
        ]
        text_h = max(len(self.comment_lines), len(self.medicine_lines)) * line_h + (row_h - line_h)
        self.height = max(max(1, len(self.activities)) * row_h, text_h)
        return self.height


def render_week(monday, sunday, activities, comments, printed_at, owner_name=""):
    """1週間分のページ（内容が非常に多い場合のみ複数ページ）を返す"""
    days = [monday + datetime.timedelta(days=i) for i in range(7)]
    acts = {d: [] for d in days}
    meds = {d: [] for d in days}
    for a in activities:
        start = parse_dt(a["start_time"])
        end = parse_dt(a["end_time"]) if a["end_time"] else None
        d = belongs_date(a, start, end)
        if d not in acts:
            continue
        if a["category"] in NO_END_CATEGORIES:
            meds[d].append((start, a))
        else:
            acts[d].append((start, end, a))
    blocks = [
        DayBlock(d, sorted(acts[d], key=lambda x: (x[0], x[2]["id"])),
                 sorted(meds[d], key=lambda x: (x[0], x[1]["id"])), comments.get(d.isoformat()))
        for d in days
    ]

    # 列の x 座標と幅
    table_right = PAGE_W - MARGIN
    fixed = sum(w for _, _, w in COLUMNS if w)
    widths = {k: (w or (table_right - MARGIN - fixed)) for k, _, w in COLUMNS}
    xs, x = {}, MARGIN
    for k, _, _ in COLUMNS:
        xs[k] = x
        x += widths[k]

    # 1ページに収まる最大の文字サイズを探す（最小でも収まらない場合は日単位で改ページ）
    avail = TABLE_BOTTOM - TABLE_TOP - HEADER_H
    font = MAX_FONT
    while font > MIN_FONT and sum(b.layout(font, widths) for b in blocks) > avail:
        font = round(font - 0.25, 2)
    for b in blocks:
        b.layout(font, widths)
    chunks, cur, used = [], [], 0
    for b in blocks:
        if cur and used + b.height > avail:
            chunks.append(cur)
            cur, used = [], 0
        cur.append(b)
        used += b.height
    chunks.append(cur)

    pages = []
    for ci, chunk in enumerate(chunks):
        p = Page()
        # ページヘッダ（1行）：左「行動記録（yyyy/m/d～m/d）」／中央「氏名」／右「出力日時」
        # 年をまたぐ週は終了日にも年を付ける
        end_text = f"{sunday.month}/{sunday.day}" if sunday.year == monday.year else f"{sunday.year}/{sunday.month}/{sunday.day}"
        title = f"行動記録（{monday.year}/{monday.month}/{monday.day}～{end_text}）"
        if len(chunks) > 1:
            title += f"（{ci + 1}/{len(chunks)}）"
        printed = f"出力日時：{printed_at}"
        p.text(MARGIN, PAGE_HEADER_Y, title, size=PAGE_HEADER_FONT)
        p.text(table_right, PAGE_HEADER_Y, printed, size=PAGE_HEADER_FONT, align="right")
        if owner_name:
            name = f"氏名：{owner_name}"
            # 中央の氏名が左右の文字と重ならないよう、長い場合は文字を小さくする
            side = max(text_width(title, PAGE_HEADER_FONT), text_width(printed, PAGE_HEADER_FONT))
            room = 2 * (PAGE_W / 2 - MARGIN - side - 12)
            size = PAGE_HEADER_FONT
            while size > 6 and text_width(name, size) > room:
                size -= 0.5
            p.text(PAGE_W / 2, PAGE_HEADER_Y, name, size=size, align="center")

        # 見出し行
        p.fill_rect(MARGIN, TABLE_TOP, table_right - MARGIN, HEADER_H, 0.88)
        for k, label, _ in COLUMNS:
            p.text(xs[k] + PAD, TABLE_TOP + HEADER_H / 2 + 3.3, label, size=9)

        y = TABLE_TOP + HEADER_H
        for bi, b in enumerate(chunk):
            f, row_h, line_h = b.font, b.row_h, b.line_h
            # 日ごとに背景を交互に
            if (b.date - monday).days % 2 == 1:
                p.fill_rect(MARGIN, y, table_right - MARGIN, b.height, 0.965)
            if bi > 0:
                p.line(MARGIN, y, table_right, y, 0.8, 0.35)        # 日の境目
            first_base = y + row_h / 2 + f * 0.36                  # 1行目のベースライン

            # 日付
            wd = b.date.weekday()
            color = SUN_COLOR if wd == 6 else SAT_COLOR if wd == 5 else (0, 0, 0)
            p.text(xs["date"] + PAD, first_base, fmt_date(b.date), size=f, color=color)

            # 行動（時刻・行動・メモ）
            if b.activities:
                for i, (start, end, a) in enumerate(b.activities):
                    ry = y + i * row_h
                    if i > 0:
                        p.line(xs["time"], ry, xs["comment"], ry, 0.3, 0.75)
                    base = ry + row_h / 2 + f * 0.36
                    p.text(xs["time"] + PAD, base, time_range_text(start, end, b.date), size=f)
                    p.text(xs["cat"] + PAD, base, CATEGORY_JA.get(a["category"], a["category"]), size=f)
                    # 睡眠はメモの代わりに睡眠時間（起床が未入力なら空欄）
                    if a["category"] == "sleep":
                        note = fmt_duration(start, end) if end else ""
                    else:
                        note = a["note"]
                    p.text(xs["note"] + PAD, base, fit_text(note, f, widths["note"] - PAD * 2), size=f)
            else:
                p.text(xs["time"] + PAD, first_base, "記録なし", size=f, color=GRAY_TEXT)

            # 1日の感想・服薬時刻（日ごとに結合したセル。上詰めで折り返し）
            for i, line in enumerate(b.comment_lines):
                p.text(xs["comment"] + PAD, first_base + i * line_h, line, size=f)
            for i, line in enumerate(b.medicine_lines):
                p.text(xs["medicine"] + PAD, first_base + i * line_h, line, size=f)
            y += b.height

        # 外枠・縦線
        p.line(MARGIN, TABLE_TOP, table_right, TABLE_TOP, 0.8, 0.2)
        p.line(MARGIN, TABLE_TOP + HEADER_H, table_right, TABLE_TOP + HEADER_H, 0.8, 0.2)
        p.line(MARGIN, y, table_right, y, 0.8, 0.2)
        for cx in list(xs.values()) + [table_right]:
            edge = cx in (MARGIN, table_right)
            p.line(cx, TABLE_TOP, cx, y, 0.8 if edge else 0.5, 0.2 if edge else 0.35)
        pages.append(p)
    return pages


def build_report(date_from, date_to, fetch_activities, fetch_comments=None, owner_name=""):
    """帳票PDFを作る。
    fetch_activities(start_dt_str, end_dt_str): 期間にかかる行動記録のリスト（前日18:00以降に就寝した睡眠を含む）
    fetch_comments(date_from_str, date_to_str): {日付: 1日の感想} の dict"""
    weeks = week_ranges(date_from, date_to)
    period_text = f"{fmt_date(date_from, True)} 〜 {fmt_date(date_to, True)}"
    printed_at = datetime.datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    pages = []
    for monday, sunday in weeks:
        acts = fetch_activities(f"{(monday - datetime.timedelta(days=1)).isoformat()} {SLEEP_DAY_START_HOUR:02d}:00",
                                f"{(sunday + datetime.timedelta(days=1)).isoformat()} 00:00")
        comments = fetch_comments(monday.isoformat(), sunday.isoformat()) if fetch_comments else {}
        pages.extend(render_week(monday, sunday, acts, comments, printed_at, owner_name))
    # ページ番号
    for i, p in enumerate(pages, start=1):
        p.text(PAGE_W / 2, FOOTER_Y, f"{i} / {len(pages)}", size=8, color=(0.4, 0.4, 0.4), align="center")
    return build_pdf(pages, f"行動記録 {period_text}")
