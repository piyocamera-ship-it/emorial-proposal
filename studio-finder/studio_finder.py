#!/usr/bin/env python3
"""東京都内の撮影スタジオ空き状況チェッカー（自分用）

studios.csv に登録したスタジオを、エリア・料金・人数で絞り込み、
公開カレンダー（iCal）があるものは指定日時の空き状況を自動判定する。
予約サイトのページを自動で読み取る処理（スクレイピング）は行わない。

使い方:
    python3 studio_finder.py --date 2026-10-20 --start 10:00 --end 14:00
    python3 studio_finder.py --date 2026-10-20 --start 10:00 --end 14:00 \
        --area 渋谷区 --max-price 6000 --people 5 --html result.html
"""

import argparse
import csv
import html
import sys
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_CSV = Path(__file__).with_name("studios.csv")

STATUS_FREE = "空き"
STATUS_BUSY = "埋まり"
STATUS_UNKNOWN = "要確認"
STATUS_ERROR = "取得失敗"


@dataclass
class Studio:
    name: str
    area: str
    station: str
    price_per_hour: int
    min_hours: float
    capacity: int
    booking_url: str
    ical_url: str
    memo: str


@dataclass
class Result:
    studio: Studio
    status: str
    cost: int
    detail: str = ""


def load_studios(path):
    studios = []
    base = Path(path).resolve().parent
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("name", "").strip():
                continue
            ical = row.get("ical_url", "").strip()
            # file:// の相対パスはCSVと同じフォルダを起点にする
            if ical.startswith("file://") and not ical[7:].startswith("/"):
                ical = "file://" + str(base / ical[7:])
            studios.append(
                Studio(
                    name=row["name"].strip(),
                    area=row.get("area", "").strip(),
                    station=row.get("station", "").strip(),
                    price_per_hour=int(row.get("price_per_hour") or 0),
                    min_hours=float(row.get("min_hours") or 0),
                    capacity=int(row.get("capacity") or 0),
                    booking_url=row.get("booking_url", "").strip(),
                    ical_url=ical,
                    memo=row.get("memo", "").strip(),
                )
            )
    return studios


# ---- iCal（.ics）の最小限の読み取り ----

def _unfold(text):
    """iCalの折り返し行（先頭が空白/タブ）を1行に戻す"""
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _parse_dt(prop, value):
    """DTSTART/DTEND の値を JST の datetime に変換する"""
    params = dict(p.split("=", 1) for p in prop.split(";")[1:] if "=" in p)
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(tzinfo=JST), True
    if value.endswith("Z"):
        d = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return d.astimezone(JST), False
    d = datetime.strptime(value, "%Y%m%dT%H%M%S")
    tz = ZoneInfo(params["TZID"]) if "TZID" in params else JST
    return d.replace(tzinfo=tz).astimezone(JST), False


def parse_ical_events(text):
    """(開始, 終了) の一覧を返す。TRANSPARENT（予定なし扱い）は除外する"""
    events = []
    current = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current and "start" in current:
                start, all_day = current["start"]
                end = current.get("end", (None, all_day))[0]
                if end is None:
                    end = start + (timedelta(days=1) if all_day else timedelta(0))
                if current.get("transp") != "TRANSPARENT":
                    events.append((start, end))
            current = None
        elif current is not None and ":" in line:
            prop, value = line.split(":", 1)
            name = prop.split(";")[0].upper()
            if name == "DTSTART":
                current["start"] = _parse_dt(prop, value.strip())
            elif name == "DTEND":
                current["end"] = _parse_dt(prop, value.strip())
            elif name == "TRANSP":
                current["transp"] = value.strip().upper()
    return events


def find_conflicts(events, start, end):
    return [(s, e) for s, e in events if s < end and e > start]


def fetch_ical(url, timeout=15):
    if url.startswith("file://"):
        return Path(url[7:]).read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": "studio-finder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="replace")


# ---- 判定 ----

def estimate_cost(studio, hours):
    billed = max(hours, studio.min_hours)
    return int(round(studio.price_per_hour * billed))


def check_studio(studio, start, end, fetch=fetch_ical):
    hours = (end - start).total_seconds() / 3600
    cost = estimate_cost(studio, hours)
    if not studio.ical_url:
        return Result(studio, STATUS_UNKNOWN, cost, "公開カレンダーなし。予約ページで確認")
    try:
        events = parse_ical_events(fetch(studio.ical_url))
    except Exception as e:  # ネットワークや形式の不具合は1件ずつ扱う
        return Result(studio, STATUS_ERROR, cost, f"カレンダー取得エラー: {e}")
    conflicts = find_conflicts(events, start, end)
    if conflicts:
        times = "、".join(f"{s:%H:%M}〜{e:%H:%M}" for s, e in conflicts[:3])
        return Result(studio, STATUS_BUSY, cost, f"予約あり: {times}")
    return Result(studio, STATUS_FREE, cost, "カレンダー上は空き（最終確認は予約ページで）")


def filter_studios(studios, area=None, station=None, max_price=None, people=None):
    out = []
    for s in studios:
        if area and area not in s.area:
            continue
        if station and station not in s.station:
            continue
        if max_price and s.price_per_hour > max_price:
            continue
        if people and s.capacity and s.capacity < people:
            continue
        out.append(s)
    return out


ORDER = {STATUS_FREE: 0, STATUS_UNKNOWN: 1, STATUS_ERROR: 2, STATUS_BUSY: 3}


def sort_results(results):
    return sorted(results, key=lambda r: (ORDER[r.status], r.cost))


# ---- 出力 ----

def print_table(results, start, end):
    wd = "月火水木金土日"[start.weekday()]
    print(f"\n{start:%Y-%m-%d}（{wd}） {start:%H:%M}〜{end:%H:%M}  該当 {len(results)} 件\n")
    for r in results:
        s = r.studio
        print(f"[{r.status}] {s.name}（{s.area} / {s.station}）")
        print(f"    ¥{s.price_per_hour:,}/時間  概算 ¥{r.cost:,}  定員 {s.capacity or '-'}名")
        print(f"    {r.detail}")
        if s.booking_url:
            print(f"    {s.booking_url}")
        if s.memo:
            print(f"    メモ: {s.memo}")
        print()


def render_html(results, start, end):
    rows = []
    for r in results:
        s = r.studio
        link = (f'<a href="{html.escape(s.booking_url)}" target="_blank" rel="noopener">予約ページ</a>'
                if s.booking_url else "")
        rows.append(
            f'<tr class="st-{ORDER[r.status]}"><td>{html.escape(r.status)}</td>'
            f"<td>{html.escape(s.name)}</td><td>{html.escape(s.area)}</td>"
            f"<td>{html.escape(s.station)}</td><td>¥{s.price_per_hour:,}</td>"
            f"<td>¥{r.cost:,}</td><td>{s.capacity or '-'}</td>"
            f"<td>{html.escape(r.detail)}</td><td>{link}</td>"
            f"<td>{html.escape(s.memo)}</td></tr>"
        )
    title = f"{start:%Y-%m-%d %H:%M}〜{end:%H:%M} のスタジオ空き状況"
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:-apple-system,"Hiragino Sans",sans-serif;margin:16px;background:#fff;color:#222}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f4f4f4}}
.st-0 td:first-child{{color:#1a7f37;font-weight:bold}}
.st-3{{color:#999}}
.st-2 td:first-child{{color:#c62828}}
</style></head><body>
<h1 style="font-size:18px">{html.escape(title)}</h1>
<div style="overflow-x:auto"><table>
<tr><th>状況</th><th>スタジオ</th><th>エリア</th><th>最寄り駅</th><th>時間単価</th>
<th>概算</th><th>定員</th><th>詳細</th><th>リンク</th><th>メモ</th></tr>
{''.join(rows)}
</table></div></body></html>"""


def parse_args(argv):
    p = argparse.ArgumentParser(description="東京都内の撮影スタジオ空き状況チェッカー")
    p.add_argument("--date", required=True, help="利用日 例: 2026-10-20")
    p.add_argument("--start", required=True, help="開始時刻 例: 10:00")
    p.add_argument("--end", required=True, help="終了時刻 例: 14:00")
    p.add_argument("--area", help="区市町村で絞り込み 例: 渋谷区")
    p.add_argument("--station", help="最寄り駅で絞り込み 例: 恵比寿")
    p.add_argument("--max-price", type=int, help="1時間あたりの上限料金（円）")
    p.add_argument("--people", type=int, help="利用人数")
    p.add_argument("--csv", default=str(DEFAULT_CSV), help="スタジオ一覧CSVのパス")
    p.add_argument("--html", help="結果をHTMLファイルにも保存する")
    p.add_argument("--open", action="store_true", help="保存したHTMLをブラウザで開く")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    d = date.fromisoformat(args.date)
    start = datetime.combine(d, datetime.strptime(args.start, "%H:%M").time(), JST)
    end = datetime.combine(d, datetime.strptime(args.end, "%H:%M").time(), JST)
    if end <= start:
        sys.exit("終了時刻は開始時刻より後にしてください")

    studios = filter_studios(load_studios(args.csv), args.area, args.station,
                             args.max_price, args.people)
    results = sort_results([check_studio(s, start, end) for s in studios])
    print_table(results, start, end)

    if args.html:
        out = Path(args.html)
        out.write_text(render_html(results, start, end), encoding="utf-8")
        print(f"HTMLを保存しました: {out.resolve()}")
        if args.open:
            webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
