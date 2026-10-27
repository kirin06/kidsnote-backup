"""키즈노트 → 시놀로지 NAS 백업 수집기.

tools/kidsnote_fetch 의 검증된 API 함수(fetch.py, kidsnote_auth.py)를 그대로
재사용하고, 저장 형식만 NAS 용으로 새로 만든다.

저장 구조 (DATA_DIR, 컨테이너 안에서는 /data):

    reports/2026/20261027_163012_123456/   알림장 1건
        report.json      목록+상세(식사/수면/배변/날씨) + 댓글 원본
        note.txt         본문 텍스트
        image_001.jpg …  사진
        video_001.mp4    동영상
        file_001_….pdf   첨부파일
    notices/2026/20261020_654321/          공지 1건 (notice.json + 첨부)
    albums/2026/20261015_777777/           앨범 1건 (album.json + 첨부)
    menus/2026-10.json                     월별 식단 원본
    menus/img/2026-10/20261027_lunch.jpg   식단 사진
    state.json                             마지막 실행 상태
    logs/collector.log

규칙
- 이미 받은 파일은 다시 받지 않는다(이어받기). 실패한 파일은 다음 실행 때 재시도.
- 모든 키즈노트 요청 사이에 REQUEST_DELAY 초(기본 1초) 간격을 둔다.
- 최근 REFRESH_DAYS 일(기본 7일) 안의 글은 매번 다시 확인해 수정/댓글을 반영.
- 공지 댓글은 다른 가정 학부모 댓글이 섞이므로 선생님/원 댓글만 저장한다.

단독 실행:  python collector.py  (서버 없이 1회 수집)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

# tools/kidsnote_fetch 를 import 경로에 추가 (Docker 에서는 PYTHONPATH 로도 지정됨)
_REPO_FETCH = Path(__file__).resolve().parent.parent / "tools" / "kidsnote_fetch"
if _REPO_FETCH.is_dir() and str(_REPO_FETCH) not in sys.path:
    sys.path.insert(0, str(_REPO_FETCH))

import fetch as kf  # noqa: E402  (tools/kidsnote_fetch/fetch.py)
from kidsnote_auth import AuthError, resolve_session  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
LOG = logging.getLogger("kidsnote_nas")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


class Config:
    def __init__(self) -> None:
        self.data_dir = Path(_env("DATA_DIR", "/data"))
        self.username = _env("KIDSNOTE_USERNAME")
        self.password = os.environ.get("KIDSNOTE_PASSWORD", "")
        self.cookie = _env("KIDSNOTE_SESSION_COOKIE")
        self.child_name = _env("KIDSNOTE_CHILD_NAME")
        cid = _env("KIDSNOTE_CHILD_ID")
        self.child_id = int(cid) if cid.isdigit() else None
        self.delay = float(_env("REQUEST_DELAY", "1.0") or 1.0)
        self.refresh_days = int(_env("REFRESH_DAYS", "7") or 7)
        self.with_notices = _env("BACKUP_NOTICES", "true").lower() != "false"
        self.with_albums = _env("BACKUP_ALBUMS", "true").lower() != "false"
        self.with_menus = _env("BACKUP_MENUS", "true").lower() != "false"


# ---------------------------------------------------------------------------
# 요청 간격 + 재시도가 들어간 세션
# ---------------------------------------------------------------------------
class ThrottledSession(requests.Session):
    """모든 요청 사이에 최소 `delay` 초 간격. 429/5xx/네트워크 오류는 3회까지 재시도."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = max(0.0, delay)
        self._last = 0.0
        self._lock = threading.Lock()
        self.headers.update({
            "User-Agent": kf.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko",
            "Referer": kf.KIDSNOTE_BASE,
        })

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        last_exc: Exception | None = None
        for attempt in range(3):
            with self._lock:
                wait = self._last + self.delay - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                resp = super().request(method, url, *args, **kwargs)
            except requests.RequestException as e:
                last_exc = e
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After", "")
                pause = int(retry_after) if retry_after.isdigit() else 10 * (attempt + 1)
                LOG.warning("HTTP %s (%s) → %ss 후 재시도", resp.status_code,
                            kf._safe_url(url), min(pause, 120))
                time.sleep(min(pause, 120))
                last_resp = resp
                if attempt < 2:
                    continue
                return last_resp
            return resp
        assert last_exc is not None
        raise last_exc


# ---------------------------------------------------------------------------
# 상태 파일
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(KST)


def _write_json(path: Path, data: Any) -> None:
    """원자적 저장(임시파일 → rename)이라 중간에 끊겨도 파일이 깨지지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


class State:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "state.json"
        self.data: dict[str, Any] = read_json(self.path) or {}
        self._lock = threading.Lock()

    def update(self, **kw: Any) -> None:
        with self._lock:
            self.data.update(kw)
            _write_json(self.path, self.data)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------
def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def item_date(item: dict[str, Any]) -> datetime:
    """글의 '날짜'. date_written(작성일) 우선, 시각은 created 에서."""
    created = _parse_dt(item.get("created")) or _parse_dt(item.get("modified"))
    written = (item.get("date_written") or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", written):
        base = datetime.strptime(written, "%Y-%m-%d").replace(tzinfo=KST)
        if created:
            return base.replace(hour=created.hour, minute=created.minute, second=created.second)
        return base
    return created or _now()


def _index_existing(root: Path) -> dict[int, Path]:
    """root/YYYY/<이름>_<id> 폴더를 id → 경로로 색인 (폴더명이 바뀌어도 같은 글을 찾기 위해)."""
    out: dict[int, Path] = {}
    if not root.is_dir():
        return out
    for year in root.iterdir():
        if not year.is_dir():
            continue
        for d in year.iterdir():
            tail = d.name.rsplit("_", 1)[-1]
            if d.is_dir() and tail.isdigit():
                out[int(tail)] = d
    return out


def _video_objects(item: dict[str, Any]) -> list[dict[str, Any]]:
    for k in kf.VIDEO_OBJECT_KEYS:
        v = item.get(k)
        if isinstance(v, dict):
            return [v]
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _save_media(sess: requests.Session, item: dict[str, Any], folder: Path) -> tuple[int, int]:
    """사진/동영상/첨부를 받는다. (새로 받은 수, 실패 수) 반환."""
    new = failed = 0

    def _one(obj: Any, stem: str, suffix: str, keep_name: bool = False) -> None:
        nonlocal new, failed
        url = obj if isinstance(obj, str) else (
            kf._first_existing_key(obj, kf.IMAGE_URL_KEYS) if isinstance(obj, dict) else None)
        if not url:
            return
        if any(folder.glob(f"{stem}.*")) or any(folder.glob(f"{stem}_*")):
            return  # 이미 있음
        if kf._download_attachment(sess, obj, folder, stem,
                                   default_suffix=suffix, keep_original_name=keep_name):
            new += 1
        else:
            failed += 1
            # 받다 끊긴 반쪽 파일은 지워야 다음 실행 때 다시 받는다
            for p in list(folder.glob(f"{stem}.*")) + list(folder.glob(f"{stem}_*")):
                p.unlink(missing_ok=True)

    for i, img in enumerate(kf._first_existing_key(item, kf.ATTACH_LIST_KEYS) or [], 1):
        _one(img, f"image_{i:03d}", ".jpg")
    for i, vid in enumerate(_video_objects(item), 1):
        _one(vid, f"video_{i:03d}", ".mp4")
    files = kf._first_existing_key(item, kf.FILE_LIST_KEYS) or []
    if isinstance(files, list):
        for i, f in enumerate(files, 1):
            _one(f, f"file_{i:03d}", ".bin", keep_name=True)
    return new, failed


def _teacher_only(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in comments
            if ((c.get("author") or {}).get("type") or "").lower() != "parent"]


# ---------------------------------------------------------------------------
# 수집기
# ---------------------------------------------------------------------------
class Collector:
    def __init__(self, cfg: Config, state: State,
                 progress: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.state = state
        self.progress = progress or (lambda msg: None)
        self.sess = ThrottledSession(cfg.delay)
        self.stats = {"new_files": 0, "failed_files": 0, "reports": 0,
                      "notices": 0, "albums": 0, "menu_days": 0}
        self.today = _now().date()

    # ---- 로그인 / 아이 선택 -------------------------------------------------
    def login(self) -> dict[str, Any]:
        s = resolve_session(self.cfg.username, self.cfg.password, self.cfg.cookie, kf.USER_AGENT)
        LOG.info("키즈노트 로그인 성공 (%s)", "아이디/비번" if s.source == "login" else "쿠키")
        self.sess.cookies.set("sessionid", s.sessionid, domain="www.kidsnote.com", path="/")
        children = kf._list_children(self.sess)
        try:
            child = kf._pick_child(children, self.cfg.child_id, self.cfg.child_name, None)
        except SystemExit as e:  # fetch.py 는 못 찾으면 sys.exit — 서버가 죽지 않게 변환
            raise RuntimeError(f"아이 선택 실패: {e}") from None
        return child

    @staticmethod
    def center_id(child: dict[str, Any]) -> int | None:
        enr = child.get("enrollment")
        if isinstance(enr, list) and enr:
            enr = enr[0]
        if isinstance(enr, dict):
            return enr.get("center_id") or enr.get("center")
        return None

    def _is_recent(self, dt: datetime) -> bool:
        return (self.today - dt.date()).days <= self.cfg.refresh_days

    # ---- 알림장 -------------------------------------------------------------
    def backup_reports(self, child_id: int) -> None:
        root = self.cfg.data_dir / "reports"
        existing = _index_existing(root)
        reports = kf._list_reports(self.sess, child_id)  # 최신순 전체
        total = len(reports)
        LOG.info("알림장 %d건 확인", total)
        for n, rep in enumerate(reports, 1):
            rid = rep.get("id")
            if not isinstance(rid, int):
                continue
            dt = item_date(rep)
            folder = existing.get(rid) or root / f"{dt:%Y}" / f"{dt:%Y%m%d_%H%M%S}_{rid}"
            meta = read_json(folder / "report.json") or {}
            done = (meta.get("_backup") or {}).get("complete") is True
            if done and not self._is_recent(dt):
                continue
            self.progress(f"알림장 {n}/{total}")
            folder.mkdir(parents=True, exist_ok=True)
            detail = kf._fetch_report_detail(self.sess, rid) or {}
            merged = {**rep, **detail}
            comments = kf._list_comments(self.sess, "reports", rid) \
                if (merged.get("num_comments") or not done) else meta.get("comments", [])
            text = kf._first_existing_key(merged, kf.TEXT_KEYS) or ""
            note = folder / "note.txt"
            if not note.exists() or note.read_text(encoding="utf-8") != text:
                note.write_text(text, encoding="utf-8")
            new, failed = _save_media(self.sess, merged, folder)
            self.stats["new_files"] += new
            self.stats["failed_files"] += failed
            merged["comments"] = comments
            merged["_backup"] = {"complete": failed == 0, "saved_at": _now().isoformat(),
                                 "date": f"{dt:%Y-%m-%d}"}
            _write_json(folder / "report.json", merged)
            self.stats["reports"] += 1

    # ---- 공지 / 앨범 (같은 형식) -------------------------------------------
    def _backup_posts(self, kind: str, items: list[dict[str, Any]]) -> None:
        """kind = notices | albums"""
        root = self.cfg.data_dir / kind
        fname = "notice.json" if kind == "notices" else "album.json"
        label = "공지" if kind == "notices" else "앨범"
        existing = _index_existing(root)
        total = len(items)
        LOG.info("%s %d건 확인", label, total)
        for n, it in enumerate(items, 1):
            iid = it.get("id")
            if not isinstance(iid, int):
                continue
            dt = item_date(it)
            folder = existing.get(iid) or root / f"{dt:%Y}" / f"{dt:%Y%m%d}_{iid}"
            meta = read_json(folder / fname) or {}
            done = (meta.get("_backup") or {}).get("complete") is True
            if done and not self._is_recent(dt):
                continue
            self.progress(f"{label} {n}/{total}")
            folder.mkdir(parents=True, exist_ok=True)
            comments: list[dict[str, Any]] = meta.get("comments", [])
            if it.get("num_comments"):
                comments = kf._list_comments(self.sess, kind, iid)
                if kind == "notices":
                    comments = _teacher_only(comments)  # 다른 가정 댓글 제외
            new, failed = _save_media(self.sess, it, folder)
            self.stats["new_files"] += new
            self.stats["failed_files"] += failed
            out = {**it, "comments": comments,
                   "_backup": {"complete": failed == 0, "saved_at": _now().isoformat(),
                               "date": f"{dt:%Y-%m-%d}"}}
            _write_json(folder / fname, out)
            self.stats[kind] += 1

    # ---- 식단 ---------------------------------------------------------------
    def backup_menus(self, center_id: int) -> None:
        self.progress("식단")
        menus = kf._list_menus(self.sess, center_id)
        LOG.info("식단 %d일 확인", len(menus))
        by_month: dict[str, list[dict[str, Any]]] = {}
        for m in menus:
            d = str(m.get("date_menu") or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                by_month.setdefault(d[:7], []).append(m)
        root = self.cfg.data_dir / "menus"
        for month, items in sorted(by_month.items()):
            items.sort(key=lambda x: str(x.get("date_menu")))
            _write_json(root / f"{month}.json", items)
            img_dir = root / "img" / month
            for m in items:
                d = str(m["date_menu"])[:10].replace("-", "")
                for field in ("morning", "morning_snack", "lunch", "afternoon_snack", "dinner"):
                    img = m.get(f"{field}_img")
                    if not isinstance(img, dict):
                        continue
                    img_dir.mkdir(parents=True, exist_ok=True)
                    stem = f"{d}_{field}"
                    if any(img_dir.glob(f"{stem}.*")):
                        continue
                    if kf._download_attachment(self.sess, img, img_dir, stem, default_suffix=".jpg"):
                        self.stats["new_files"] += 1
                    else:
                        self.stats["failed_files"] += 1
                        for p in img_dir.glob(f"{stem}.*"):
                            p.unlink(missing_ok=True)
        self.stats["menu_days"] = len(menus)

    # ---- 전체 실행 ----------------------------------------------------------
    def run(self) -> dict[str, Any]:
        child = self.login()
        child_id = int(child["id"])
        center = self.center_id(child)
        self.backup_reports(child_id)
        if center and self.cfg.with_notices:
            self._safe("공지", lambda: self._backup_posts(
                "notices", kf._list_notices(self.sess, center)))
        if self.cfg.with_albums:
            self._safe("앨범", lambda: self._backup_posts(
                "albums", kf._list_albums(self.sess, child_id)))
        if center and self.cfg.with_menus:
            self._safe("식단", lambda: self.backup_menus(center))
        return self.stats

    def _safe(self, label: str, fn: Callable[[], None]) -> None:
        """공지/앨범/식단 중 하나가 실패해도 나머지는 계속."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            LOG.exception("%s 백업 실패: %s", label, e)
            self.stats.setdefault("errors", []).append(f"{label}: {e}")


# ---------------------------------------------------------------------------
# 서버/CLI 공용 진입점
# ---------------------------------------------------------------------------
_RUN_LOCK = threading.Lock()


def setup_logging(data_dir: Path) -> None:
    if LOG.handlers:
        return
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)
    try:
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            data_dir / "logs" / "collector.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass
    # fetch.py 경고(다운로드 실패 등)도 같은 곳으로
    logging.getLogger("kidsnote_fetch").handlers = LOG.handlers
    logging.getLogger("kidsnote_fetch").setLevel(logging.INFO)


def is_running() -> bool:
    return _RUN_LOCK.locked()


def run_once(trigger: str = "manual") -> bool:
    """1회 수집. 이미 실행 중이면 False."""
    if not _RUN_LOCK.acquire(blocking=False):
        return False
    cfg = Config()
    setup_logging(cfg.data_dir)
    state = State(cfg.data_dir)
    started = _now()
    state.update(status="running", trigger=trigger, last_run_started=started.isoformat(),
                 progress="로그인 중")
    LOG.info("수집 시작 (%s)", trigger)
    try:
        col = Collector(cfg, state, progress=lambda m: state.update(progress=m))
        stats = col.run()
        finished = _now()
        LOG.info("수집 완료: %s (%.0f초)", stats, (finished - started).total_seconds())
        state.update(status="ok", progress="", last_run_finished=finished.isoformat(),
                     last_success=finished.isoformat(), last_error="", last_stats=stats)
    except AuthError as e:
        LOG.error("로그인 실패: %s", e)
        state.update(status="error", progress="", last_run_finished=_now().isoformat(),
                     last_error=f"로그인 실패: {e}")
    except Exception as e:  # noqa: BLE001
        LOG.exception("수집 실패")
        state.update(status="error", progress="", last_run_finished=_now().isoformat(),
                     last_error=f"{type(e).__name__}: {e}"[:300])
    finally:
        _RUN_LOCK.release()
    return True


if __name__ == "__main__":
    run_once("cli")
    st = read_json(Config().data_dir / "state.json") or {}
    sys.exit(0 if st.get("status") == "ok" else 1)
