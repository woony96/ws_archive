#!/usr/bin/env python3
"""자소설닷컴 신입채용 일일 보고서 생성기.

자소설닷컴 공개 API에서 공고를 받아
  1) 최근 24시간 동안 자소설닷컴에 새로 올라온 공고
  2) 오늘~모레 마감되는 공고
를 골라 마크다운 보고서(jobs/reports/YYYY-MM-DD.md)와 요약(jobs/reports/latest.json)을 만든다.

필터: 구분(신입, 인턴, 신입/경력, 신입/인턴) + 기업 규모(대기업, 공공기관, 중견기업)

사용법: python3 jobs/jasoseol_report.py [--date YYYY-MM-DD] [--out-dir DIR]
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone

BASE = "https://jasoseol.com"
DETAIL = BASE + "/recruit/{id}"
KST = timezone(timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (jasoseol-daily-report)", "Accept": "application/json"}

# 자소설닷컴 프론트엔드의 구분 코드
DIVISIONS = {1: "신입", 2: "경력", 3: "인턴", 4: "계약직", 5: "신입/경력", 6: "신입/인턴", 7: "교육"}
WANTED_DIVISIONS = {1, 3, 5, 6}

# 정렬 순서도 겸한다
SIZES = {"big_business": "대기업", "public_institution": "공공기관", "middle_market": "중견기업"}
SIZE_ORDER = list(SIZES)


def get(path, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as r:
        return json.load(r)


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={**HEADERS, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def recently_opened(since):
    """자소설닷컴에 올라온 순서(opened_at 내림차순)로 since 이후 공고를 모은다."""
    out = []
    for page in range(1, 11):
        batch = get("/api/v1/employment_companies", per_page=100, page=page)
        out += batch
        if not batch or min(parse(p.get("opened_at") or p["start_time"]) for p in batch) <= since:
            break
    return out


def closing_soon(today, until):
    """마감 임박 목록 + 채용달력의 날짜별 마감 공고를 합친다(한쪽에서 빠지는 공고가 있어서)."""
    out = get("/api/v1/employment_companies/top-closing", per_page=200)
    day = today
    while day <= until.date():
        start = datetime.combine(day, time(), KST)
        out += post("/employment/calendar_list.json",
                    {"start_time": utc(start), "end_time": utc(start + timedelta(days=1))})["employment"]
        day += timedelta(days=1)
    return out


def parse(ts):
    return datetime.fromisoformat(ts).astimezone(KST)


def size_of(p):
    group = p.get("company_group") or {}
    if (p.get("business_type") or group.get("business_type")) == "public_institution":
        return "public_institution"
    size = p.get("business_size") or group.get("business_size")
    return size if size in SIZES else None


def divisions_of(p):
    """목록 API는 division이 [1, 2] 같은 배열, 채용달력 API는 5 같은 단일 코드다."""
    found = set()
    for e in p.get("employments") or []:
        d = e.get("division")
        codes = set(d) if isinstance(d, list) else {d}
        if {1, 2} <= codes:
            codes = (codes - {1, 2}) | {5}
        found |= codes & set(DIVISIONS)
    return found


def normalize(p):
    fields = [e["field"].strip() for e in p.get("employments") or [] if e.get("field")]
    return {
        "id": p["id"],
        "company": p["name"].strip(),
        "title": p["title"].strip(),
        "size": size_of(p),
        "start": parse(p["start_time"]),
        "end": parse(p["end_time"]),
        "opened": parse(p["opened_at"]) if p.get("opened_at") else None,
        "always": p.get("recruit_type") == 1,  # 상시채용
        "divisions": divisions_of(p),
        "fields": list(dict.fromkeys(fields)),
        "url": DETAIL.format(id=p["id"]),
    }


def merge(posts):
    """같은 공고가 여러 API에서 오면 필드 정보가 있는 쪽을 살린다."""
    by_id = {}
    for p in map(normalize, posts):
        old = by_id.get(p["id"])
        if old:
            p["fields"] = p["fields"] or old["fields"]
            p["opened"] = p["opened"] or old["opened"]
            p["divisions"] |= old["divisions"]
        by_id[p["id"]] = p
    return [p for p in by_id.values() if p["size"] and p["divisions"] & WANTED_DIVISIONS]


def sort_key(p):
    return (SIZE_ORDER.index(p["size"]), p["end"], p["company"])


def dday(end, today):
    n = (end.date() - today).days
    return "D-Day" if n == 0 else f"D-{n}"


def cell(text):
    return text.replace("|", "/").replace("\n", " ")


def fields_text(p, limit=4):
    if not p["fields"]:
        return "-"
    shown = ", ".join(p["fields"][:limit])
    return cell(shown + (f" 외 {len(p['fields']) - limit}" if len(p["fields"]) > limit else ""))


def row(p, today, with_fields):
    labels = "·".join(DIVISIONS[d] for d in sorted(p["divisions"]) if d in DIVISIONS)
    period = "상시" if p["always"] else f"{p['start']:%m/%d} ~ {p['end']:%m/%d %H:%M} ({dday(p['end'], today)})"
    cols = [SIZES[p["size"]], cell(p["company"]), f"[{cell(p['title'])}]({p['url']})", labels]
    if with_fields:
        cols.append(fields_text(p))
    return "| " + " | ".join(cols + [period]) + " |"


def table(posts, today, with_fields=False):
    if not posts:
        return "_해당 공고 없음_\n"
    head = ["규모", "기업", "공고", "구분"] + (["모집 분야"] if with_fields else []) + ["접수기간"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += [row(p, today, with_fields) for p in posts]
    return "\n".join(lines) + "\n"


def build(now):
    today = now.date()
    since = now - timedelta(days=1)
    until = datetime.combine(today + timedelta(days=2), time.max, KST)

    new = merge(recently_opened(since))
    new = sorted((p for p in new if p["opened"] and since < p["opened"] <= now and p["end"] >= now),
                 key=sort_key)
    new_ids = {p["id"] for p in new}
    closing = merge(closing_soon(today, until))
    closing = sorted((p for p in closing
                      if now <= p["end"] <= until and not p["always"] and p["id"] not in new_ids),
                     key=lambda p: (p["end"].date(), SIZE_ORDER.index(p["size"]), p["end"], p["company"]))

    md = [
        f"# 자소설닷컴 신입채용 보고 — {today:%Y-%m-%d} ({'월화수목금토일'[today.weekday()]})",
        "",
        f"- 새 공고: {since:%m/%d %H:%M} ~ {now:%m/%d %H:%M} KST 자소설닷컴 등록분 **{len(new)}건**",
        f"- 마감 임박: {today:%m/%d} ~ {until:%m/%d} 마감 **{len(closing)}건**",
        "- 필터: 신입·인턴·신입/경력·신입/인턴 / 대기업·공공기관·중견기업",
        "",
        f"## 🆕 새로 올라온 공고 ({len(new)})",
        "",
        table(new, today, with_fields=True),
        f"## ⏰ 마감 임박 ({len(closing)})",
        "",
        table(closing, today),
        f"출처: [자소설닷컴]({BASE}/recruit) · 생성 {datetime.now(KST):%Y-%m-%d %H:%M} KST",
        "",
    ]

    def brief(p):
        return {"company": p["company"], "title": p["title"], "size": SIZES[p["size"]],
                "divisions": [DIVISIONS[d] for d in sorted(p["divisions"])], "fields": p["fields"],
                "end": p["end"].isoformat(), "dday": dday(p["end"], today), "url": p["url"]}

    summary = {"date": today.isoformat(), "new_count": len(new), "closing_count": len(closing),
               "new": [brief(p) for p in new], "closing": [brief(p) for p in closing]}
    return "\n".join(md), summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="보고 기준 날짜(YYYY-MM-DD). 지정하면 그날 18:00 기준")
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports"))
    args = ap.parse_args()

    now = datetime.now(KST)
    if args.date:
        now = datetime.combine(datetime.strptime(args.date, "%Y-%m-%d").date(), time(18), KST)

    md, summary = build(now)
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, f"{summary['date']}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(args.out_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(path)
    print(json.dumps({k: summary[k] for k in ("date", "new_count", "closing_count")}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
