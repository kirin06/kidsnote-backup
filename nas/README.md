# 키즈노트 → 시놀로지 NAS 백업 + HA 대시보드 (1단계)

기존 노션 백업(GitHub Actions)은 그대로 두고, 따로 **NAS에 원본을 쌓고** HA 대시보드에 매일 띄웁니다.

```
키즈노트 ──(매일 18:00 / 불러오기 버튼)──> NAS 컨테이너 kidsnote-nas (:8765, 집 안에서만)
                                              │  /volume1/kidsnote 에 저장
                                              ▼
                    Home Assistant ── REST 센서 / 사진 12칸(페이지) / 미디어 보관함
                                              ▼
                         HA 로그인으로만 보는 '키즈노트' 화면
```

- 백업 범위: 알림장(본문·사진·동영상·첨부·댓글·식사/수면/배변/날씨), 공지, 앨범, 식단(+사진). **처음 글부터 전부.**
- 요청 사이 1초 간격, 이미 받은 파일은 건너뛰고, 실패한 파일은 다음 실행 때 다시 받습니다.
- 최근 7일 글은 매번 다시 확인해서 수정·댓글을 반영합니다.
- 공지 댓글은 다른 가정 학부모 댓글이 섞이므로 선생님 댓글만 저장합니다.
- 8765 포트는 **공유기 포트포워딩·역방향 프록시에 절대 등록하지 않습니다.** 밖에서는 HA를 통해서만 봅니다.

---

## 0. GitHub에 올릴 파일

저장소 최상위에 아래 두 폴더를 그대로 올립니다. (기존 파일은 건드리지 않음)

```
nas/   collector.py  server.py  Dockerfile  docker-compose.yml  .env.example  .gitignore  README.md
ha/    packages/kidsnote.yaml   dashboard_kidsnote_view.yaml
```

> `.env.example`, `.gitignore` 는 점(.)으로 시작해서 웹 업로드 시 안 보일 수 있습니다. 업로드 목록에 있는지 꼭 확인하세요.

---

## 1. 시놀로지 준비

1. **공유폴더 만들기**: 제어판 → 공유 폴더 → 생성 → 이름 `kidsnote` (경로 `/volume1/kidsnote`)
2. **코드 내려받기**: GitHub 저장소 → Code → Download ZIP
   → File Station 에서 `/volume1/docker/` 에 업로드 후 압축 풀기
   → 폴더 이름을 `kidsnote-backup` 으로 맞추기
   (최종 경로: `/volume1/docker/kidsnote-backup/nas/docker-compose.yml`)
3. **비밀번호 파일 만들기**: `/volume1/docker/kidsnote-backup/nas/.env.example` 을 같은 폴더에
   `kidsnote.env` 로 복사 → 텍스트 편집기로 열어서 채우기
   - `KIDSNOTE_USERNAME`, `KIDSNOTE_PASSWORD`
   - `KIDSNOTE_CHILD_NAME` (아이가 한 명이면 비워도 됨)
   - `KIDSNOTE_RUN_KEY` : 아무 긴 문자열 (예: 비밀번호 생성기로 40자). HA에도 똑같이 넣습니다.
   - 이 파일은 NAS 에만 둡니다. GitHub·채팅에 붙여넣지 마세요.

## 2. 컨테이너 실행

Container Manager → **프로젝트 → 생성**
- 프로젝트 이름: `kidsnote`
- 경로: `/volume1/docker/kidsnote-backup/nas`
- 원본: "기존 docker-compose.yml 사용"
- 웹 포털 설정: 사용 안 함 → 완료 (이미지 빌드에 1~2분)

처음 켜지면 **바로 전체 백필**이 시작됩니다.
- 진행 상황: 컨테이너 → kidsnote-nas → 로그, 또는 `/volume1/kidsnote/logs/collector.log`
- 시간: 1초 간격이라 알림장 1,000건 · 사진 5,000장이면 대략 2~3시간. 중간에 꺼져도 다음 실행 때 이어서 받습니다.

**확인** (집 와이파이에 연결된 폰/PC 브라우저)
- `http://NAS_IP:8765/health` → `ok`
- `http://NAS_IP:8765/api/summary` → 오늘(또는 최근) 알림장 JSON

**(선택) 더 잠그기**: 제어판 → 보안 → 방화벽 → 8765 포트는 HA IP 에서만 허용.

## 3. Home Assistant

### 3-1. 패키지
1. `ha/packages/kidsnote.yaml` → HA `/config/packages/kidsnote.yaml`
   - `configuration.yaml` 에 아래가 없으면 추가
     ```yaml
     homeassistant:
       packages: !include_dir_named packages
     ```
2. 파일 안의 `192.168.0.10` 을 NAS IP로 전부 바꾸기 (14군데: summary·run·사진 12칸)
3. `secrets.yaml` 에 추가
   ```yaml
   kidsnote_run_key: "kidsnote.env 의 KIDSNOTE_RUN_KEY 와 같은 값"
   ```
4. 개발자 도구 → YAML → 설정 확인 → HA 재시작

생기는 엔티티: `sensor.kidsnote_summary`, `sensor.kidsnote_photo_count`, `sensor.kidsnote_page_photo_count`,
`input_number.kidsnote_photo_page`, `image.kidsnote_photo_1~12`,
`script.kidsnote_load`, `script.kidsnote_photo_next` / `_prev`,
자동화 2개(18:05·18:20 새로고침, 새 알림장이면 사진 1페이지로)

**사진 페이지**: '오늘의 순간'은 한 페이지 12장(3열 × 4줄, 아이폰 사진 앱 기본 크기).
12장이 넘으면 제목 옆 `‹ 1/3 ›` 로 넘기거나, 마지막 칸의 `+N` 을 눌러 다음 페이지로 갑니다.

> 사진은 HA가 NAS에서 받아서 HA 로그인 사용자에게만 보여줍니다(`image` 엔티티). NAS 주소는 폰에 노출되지 않습니다.

### 3-2. 대시보드 화면
대시보드 → 편집 → 원본 구성 편집기 → `views:` 목록 안에 `ha/dashboard_kidsnote_view.yaml` 내용을
다른 뷰와 같은 들여쓰기로 붙여넣기 → 저장.
주소는 `/<대시보드>/kidsnote`, 왼쪽 위 `‹ 홈` 은 같은 대시보드의 `home` 뷰로 갑니다.

홈 화면에 들어가는 타일을 원하면 홈 뷰 그리드에 추가:
```yaml
- type: custom:button-card
  template: info_tile
  entity: sensor.kidsnote_summary
  name: 키즈노트
  icon: mdi:teddy-bear
  variables:
    on_states: [running]
  state_display: >
    [[[ const r = entity.attributes.report;
        return r ? (r.is_fallback ? '최근 ' + r.date_label : '오늘 알림장 도착') : '알림장 없음'; ]]]
  tap_action:
    action: navigate
    navigation_path: kidsnote
```

### 3-3. 전체 보관함 (HA 미디어)
**시놀로지**: 제어판 → 사용자 → `ha_media` 사용자 생성 → `kidsnote` 폴더 **읽기 전용** 권한만.
제어판 → 파일 서비스 → SMB 켜짐 확인.

**HA**: 설정 → 시스템 → 저장소 → **네트워크 저장소 추가**
- 이름 `kidsnote` · 용도 **미디어** · 서버 NAS IP · 프로토콜 **Samba/Windows (CIFS)**
- 원격 공유 `kidsnote` · 사용자 `ha_media` / 비밀번호

→ 미디어 탭 → 내 미디어 → kidsnote → `reports/2026/…` 에서 날짜별 사진·동영상을 봅니다.
(대시보드의 '보관함' 타일이 이 화면으로 연결됩니다.)

---

## 저장 구조 (`/volume1/kidsnote`)

```
reports/2026/20261027_163012_<id>/  report.json  note.txt  image_001.jpg …  video_001.mp4
notices/2026/20261020_<id>/         notice.json  image_001.jpg …
albums/2026/20261015_<id>/          album.json   image_001.jpg …
menus/2026-10.json                  menus/img/2026-10/20261027_lunch.jpg
state.json                          logs/collector.log
```

## 설정값 (`kidsnote.env`)

| 키 | 기본 | 설명 |
|---|---|---|
| RUN_AT | 18:00 | 매일 자동 수집 시각 |
| REQUEST_DELAY | 1.0 | 요청 간격(초) |
| REFRESH_DAYS | 7 | 최근 며칠 글을 매번 다시 확인 |
| RUN_ON_START | auto | 켤 때 백필 (auto=성공 기록 없을 때만) |
| PHOTOS_PER_PAGE | 12 | 대시보드 사진 한 페이지 장수 (HA 사진 칸 수와 같게 둘 것) |
| BACKUP_NOTICES / ALBUMS / MENUS | true | 항목별 백업 여부 |
| KIDSNOTE_SESSION_COOKIE | | 2단계 인증 계정용 (14일마다 갱신 필요) |

## 코드 업데이트

GitHub 에서 새 ZIP → `/volume1/docker/kidsnote-backup` 덮어쓰기 (`kidsnote.env` 는 유지)
→ Container Manager → 프로젝트 kidsnote → **정지 → 빌드 → 시작**

## 문제 해결

| 증상 | 확인 |
|---|---|
| 대시보드 상태가 "NAS 서버 연결 없음" | HA에서 NAS_IP:8765 접근 가능? 방화벽? IP 오타? |
| "수집 오류 · 로그인 실패" | kidsnote.env 아이디/비번. 2단계 인증이면 SESSION_COOKIE |
| 불러오기 버튼이 반응 없음 | secrets.yaml 키 ≠ KIDSNOTE_RUN_KEY → 서버가 403 |
| 사진 칸이 비어 있음 | `http://NAS_IP:8765/photo/1?page=1` 이 사진을 주는지, HA 로그에 template image 오류 |
| 일부 사진 누락 | 다음 18:00 실행 때 자동 재시도. 급하면 불러오기 버튼 |
