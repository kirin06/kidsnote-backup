"""키즈노트 NAS 조회 서버 (LAN 전용, 포트 8765).

Home Assistant 만 이 서버를 읽는다. 공유기 포트포워딩/역방향 프록시에 올리지 않는다.

  GET  /health              살아있는지 확인
  GET  /api/summary?date=YYYY-MM-DD
                            대시보드용 요약: 그날 알림장 전부(오늘인데 없으면 최근 날),
                            그 달 달력(알림장 있는 날), 공지 3개, 상태. date 없으면 오늘
  GET  /photo/<n>?page=<p>&date=YYYY-MM-DD
                            그날 사진 전체 중 p페이지의 n번째 (없으면 투명 1px)
                            한 페이지 = PHOTOS_PER_PAGE 장 (기본 12)
  POST /api/run             지금 수집 (헤더 X-Run-Key 필요)

매일 RUN_AT(기본 18:00, 한국시간)에 자동 수집.
처음 켰을 때 성공 기록이 없으면 바로 전체 백필을 시작한다.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import collector as col

KST = col.KST
DATA = Path(os.environ.get("DATA_DIR", "/data"))
PORT = int(os.environ.get("PORT", "8765"))
RUN_KEY = (os.environ.get("KIDSNOTE_RUN_KEY") or "").strip()
RUN_AT = (os.environ.get("RUN_AT") or "18:00").strip()
RUN_ON_START = (os.environ.get("RUN_ON_START") or "auto").strip().lower()
PER_PAGE = max(1, int(os.environ.get("PHOTOS_PER_PAGE") or 12))

try:  # 식사/수면/날씨 코드 → 한글 (기존 노션 미러와 같은 표)
    from notion_mirror import SLEEP_HOUR_KO, STATUS_KO, WEATHER_KO, WEATHER_HIDDEN
except Exception:  # noqa: BLE001
    SLEEP_HOUR_KO, STATUS_KO, WEATHER_KO, WEATHER_HIDDEN = {}, {}, {}, frozenset()

# 1x1 투명 PNG (사진 칸이 비었을 때)
BLANK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000b49444154789c6360000200000500017a5eab3f00000000"
    "49454e44ae426082")
WEEK = "월화수목금토일"
IMG_RE = re.compile(r"^image_\d{3}\.(jpe?g|png|gif|webp|heic)$", re.I)
VID_RE = re.compile(r"^video_\d{3}\.", re.I)


# ---------------------------------------------------------------------------
# 데이터 읽기
# ---------------------------------------------------------------------------
def _folders_newest_first(root: Path):
    if not root.is_dir():
        return
    for year in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        for d in sorted((p for p in year.iterdir() if p.is_dir()), reverse=True):
            yield d


def _date_of(meta: dict[str, Any], folder: Path) -> date:
    raw = ((meta.get("_backup") or {}).get("date") or meta.get("date_written") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return datetime.strptime(folder.name[:8], "%Y%m%d").date()


def _label(d: date) -> str:
    return f"{d.month}월 {d.day}일 ({WEEK[d.weekday()]})"


def _author(meta: dict[str, Any]) -> str:
    return meta.get("author_name") or (meta.get("author") or {}).get("name") or ""


def _ko(v: Any) -> str | None:
    if isinstance(v, bool):
        v = "true" if v else "false"
    if not v:
        return None
    return STATUS_KO.get(v, v)


def _life(meta: dict[str, Any]) -> list[str]:
    out: list[str] = []
    w = meta.get("weather")
    if w and w not in WEATHER_HIDDEN:
        out.append(WEATHER_KO.get(w, w))
    if (m := _ko(meta.get("meal_status"))):
        out.append(f"🍽️ 식사 {m}")
    if (s := meta.get("sleep_hour")):
        out.append(f"💤 낮잠 {SLEEP_HOUR_KO.get(s, s)}")
    if (b := _ko(meta.get("bowel_status"))):
        out.append(f"🚽 배변 {b}")
    if (t := _ko(meta.get("temperature_status"))):
        out.append(f"🌡️ 체온 {t}")
    return [x for x in out if isinstance(x, str)][:5]


def _media(folder: Path) -> tuple[list[Path], int]:
    files = sorted(p for p in folder.iterdir() if p.is_file())
    return [p for p in files if IMG_RE.match(p.name)], sum(1 for p in files if VID_RE.match(p.name))


def _is_parent(meta: dict[str, Any]) -> bool:
    return ((meta.get("author") or {}).get("type") or "").lower() == "parent"


def _teacher_reports_on(d: date) -> list[tuple[Path, dict[str, Any]]]:
    """그날 선생님이 쓴 알림장 전부, 시간 순 (폴더 이름 앞 8자리 = 날짜)."""
    ydir = DATA / "reports" / f"{d:%Y}"
    if not ydir.is_dir():
        return []
    out = []
    for folder in sorted(ydir.glob(f"{d:%Y%m%d}_*")):
        meta = col.read_json(folder / "report.json")
        if meta and not _is_parent(meta):
            out.append((folder, meta))
    return out


def _latest_teacher_date(before: date) -> date | None:
    """before 이전(포함)에서 선생님 알림장이 있는 가장 최근 날짜."""
    for i, folder in enumerate(_folders_newest_first(DATA / "reports")):
        if i > 120:
            break
        meta = col.read_json(folder / "report.json")
        if not meta or _is_parent(meta):
            continue
        d = _date_of(meta, folder)
        if d <= before:
            return d
    return None


def resolve_day(selected: date, today: date) -> tuple[date | None, list[tuple[Path, dict[str, Any]]]]:
    """보여줄 날짜와 그날 알림장 목록.
    오늘을 골랐는데 오늘 알림장이 없으면 가장 최근 날로 대체한다."""
    items = _teacher_reports_on(selected)
    if items or selected != today:
        return (selected if items else None), items
    d = _latest_teacher_date(today)
    return (d, _teacher_reports_on(d)) if d else (None, [])


def day_photos(selected: date, today: date) -> list[Path]:
    """그날 알림장 사진 전부 (여러 건이면 시간 순으로 이어붙임)."""
    _, items = resolve_day(selected, today)
    out: list[Path] = []
    for folder, _ in items:
        out.extend(_media(folder)[0])
    return out


def _comments(meta: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for c in (meta.get("comments") or [])[-5:]:
        body = (c.get("content") or "").strip() or ("[이모티콘]" if c.get("emoticon_content") else "")
        out.append({"author": c.get("author_name") or (c.get("author") or {}).get("name") or "",
                    "type": (c.get("author") or {}).get("type") or "",
                    "text": body[:300]})
    return out


def _time_label(meta: dict[str, Any]) -> str:
    dt = col._parse_dt(meta.get("created"))
    return f"{dt:%H:%M}" if dt else ""


def build_report(selected: date, today: date) -> dict[str, Any]:
    d, items = resolve_day(selected, today)
    entries, photos, videos, ids = [], 0, 0, []
    for folder, meta in items:
        imgs, n_vid = _media(folder)
        photos += len(imgs)
        videos += n_vid
        ids.append(str(meta.get("id")))
        entries.append({
            "id": meta.get("id"),
            "time": _time_label(meta),
            "author": _author(meta),
            "text": (meta.get("content") or meta.get("body") or "").strip()[:4000],
            "life": _life(meta),
            "photo_count": len(imgs),
            "video_count": n_vid,
            "comments": _comments(meta),
        })
    is_today = selected == today
    fallback = bool(d) and d != selected
    if not d:
        line = "아직 받은 알림장이 없어요." if is_today else "이날은 알림장이 없어요."
    elif not fallback:
        line = ""
    elif d == today - timedelta(days=1):
        line = "오늘 자료가 없어 어제 알림장을 보여드려요"
    else:
        line = f"오늘 자료가 없어 {d.month}월 {d.day}일 알림장을 보여드려요"
    shown = d or selected
    return {
        "selected": selected.isoformat(),
        "is_today": is_today,
        "date": shown.isoformat(),
        "date_label": _label(shown),
        "has_report": bool(items),
        "is_fallback": fallback,
        "fallback_line": line,
        "items": entries,
        "count": len(entries),
        "photo_count": photos,
        "video_count": videos,
        # HA 이미지 엔티티 캐시 갱신용 (날짜·글·사진 수가 바뀌면 값이 바뀜)
        "version": f"{selected:%Y%m%d}-{'_'.join(ids) or '0'}-{photos}",
        # 대시보드 사진 페이지 넘기기용
        "per_page": PER_PAGE,
        "page_count": max(1, -(-photos // PER_PAGE)),
    }


def build_calendar(month_of: date, today: date) -> dict[str, Any]:
    """그 달에 알림장이 있는 날 {날짜: {count, photos}}"""
    days: dict[str, dict[str, int]] = {}
    ydir = DATA / "reports" / f"{month_of:%Y}"
    if ydir.is_dir():
        for folder in sorted(ydir.glob(f"{month_of:%Y%m}*_*")):
            meta = col.read_json(folder / "report.json")
            if not meta or _is_parent(meta):
                continue
            d = _date_of(meta, folder)
            k = d.isoformat()
            e = days.setdefault(k, {"count": 0, "photos": 0})
            e["count"] += 1
            e["photos"] += len(_media(folder)[0])
    return {"month": f"{month_of:%Y-%m}", "today": today.isoformat(), "days": days}


def _parse_date(raw: str | None, today: date) -> date:
    try:
        return datetime.strptime((raw or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return today


def build_summary(selected_raw: str | None = None) -> dict[str, Any]:
    now = datetime.now(KST)
    today = now.date()
    st = col.read_json(DATA / "state.json") or {}
    running = col.is_running()
    selected = _parse_date(selected_raw, today)

    report = build_report(selected, today)
    today_rep = report if selected == today else build_report(today, today)
    today_info = {"has_report": today_rep["has_report"] and not today_rep["is_fallback"],
                  "photo_count": today_rep["photo_count"] if not today_rep["is_fallback"] else 0,
                  "latest_label": today_rep["date_label"]}

    notices = []
    for folder in _folders_newest_first(DATA / "notices"):
        meta = col.read_json(folder / "notice.json")
        if not meta:
            continue
        body = re.sub(r"\s+", " ", (meta.get("content") or "")).strip()
        d = _date_of(meta, folder)
        notices.append({"date": d.isoformat(), "date_label": f"{d.month}/{d.day}",
                        "title": (meta.get("title") or body[:30] or "공지").strip(),
                        "author": _author(meta), "snippet": body[:80],
                        "is_new": (today - d).days <= 2})
        if len(notices) >= 3:
            break

    status = "running" if running else (st.get("status") or "never")
    return {
        "status": status,
        "generated_at": now.isoformat(timespec="seconds"),
        "report": report,
        "today": today_info,
        "calendar": build_calendar(selected, today),
        "notices": notices,
        "run": {
            "progress": st.get("progress", "") if running else "",
            "last_success": st.get("last_success", ""),
            "last_error": st.get("last_error", "") if status == "error" else "",
            "next_run": next_run_time().isoformat(timespec="minutes"),
            "stats": st.get("last_stats", {}),
        },
    }


# ---------------------------------------------------------------------------
# 스케줄러
# ---------------------------------------------------------------------------
def _run_at() -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", RUN_AT)
    return (int(m.group(1)), int(m.group(2))) if m else (18, 0)


def next_run_time() -> datetime:
    h, m = _run_at()
    now = datetime.now(KST)
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return nxt if nxt > now else nxt + timedelta(days=1)


def _start_run(trigger: str) -> bool:
    if col.is_running():
        return False
    threading.Thread(target=col.run_once, args=(trigger,), daemon=True).start()
    return True


def scheduler() -> None:
    st = col.read_json(DATA / "state.json") or {}
    if RUN_ON_START == "true" or (RUN_ON_START == "auto" and not st.get("last_success")):
        _start_run("startup")
    while True:
        target = next_run_time()
        while datetime.now(KST) < target:
            time.sleep(min(30, max(1, (target - datetime.now(KST)).total_seconds())))
        if not _start_run("schedule"):
            col.LOG.info("예약 시각이지만 이미 수집 중이라 건너뜀")
        time.sleep(61)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, bytes]] = {}
_cache_lock = threading.Lock()


def summary_bytes(selected: str | None) -> bytes:
    key = (selected or "")[:10]
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] <= 10 and not col.is_running():
            return hit[1]
        body = json.dumps(build_summary(selected), ensure_ascii=False).encode("utf-8")
        if len(_cache) > 50:
            _cache.clear()
        _cache[key] = (time.monotonic(), body)
        return body


class Handler(BaseHTTPRequestHandler):
    server_version = "kidsnote-nas/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 접근 로그는 조용히
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        try:
            if path == "/health":
                self._send(200, b"ok", "text/plain")
            elif path == "/api/summary":
                self._send(200, summary_bytes((qs.get("date") or [""])[0]))
            elif (m := re.fullmatch(r"/photo/(\d{1,2})", path)):
                page = (qs.get("page") or ["1"])[0]
                page = int(page) if page.isdigit() and int(page) > 0 else 1
                self._photo((page - 1) * PER_PAGE + int(m.group(1)), (qs.get("date") or [""])[0])
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            col.LOG.exception("요청 처리 실패 %s", path)
            self._json(500, {"error": str(e)[:200]})

    do_HEAD = do_GET

    def _photo(self, n: int, selected_raw: str = "") -> None:
        """n = 그날 사진 전체에서 몇 번째인지 (페이지 반영 후, 1부터)"""
        today = datetime.now(KST).date()
        imgs = day_photos(_parse_date(selected_raw, today), today)
        if 1 <= n <= len(imgs):
            p = imgs[n - 1]
            ctype = mimetypes.guess_type(p.name)[0] or "image/jpeg"
            self._send(200, p.read_bytes(), ctype)
            return
        self._send(200, BLANK_PNG, "image/png")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/api/run":
            self._json(404, {"error": "not found"})
            return
        given = self.headers.get("X-Run-Key", "")
        if not RUN_KEY or not hmac.compare_digest(given.encode(), RUN_KEY.encode()):
            self._json(403, {"error": "bad run key"})
            return
        if _start_run("manual"):
            self._json(202, {"started": True})
        else:
            self._json(409, {"started": False, "reason": "already running"})


def main() -> None:
    col.setup_logging(DATA)
    if not RUN_KEY:
        col.LOG.warning("KIDSNOTE_RUN_KEY 가 비어 있어 '불러오기' 버튼(POST /api/run)이 막혀 있습니다.")
    threading.Thread(target=scheduler, daemon=True).start()
    col.LOG.info("키즈노트 서버 시작 :%d (매일 %s 수집)", PORT, RUN_AT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
