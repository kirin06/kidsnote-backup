# 키즈노트 → 시놀로지 NAS 백업 + Home Assistant 대시보드

기존 노션 백업(GitHub Actions)은 그대로 두고, 키즈노트 원본을 **시놀로지 NAS에 쌓고**
**Home Assistant(HA) 대시보드**에서 날짜별로 보는 구성입니다.

```
키즈노트 ──(매일 18:00 · 불러오기 버튼)──▶ NAS 컨테이너 kidsnote-nas (:8765, 집 안 LAN 전용)
                                              │  /volume1/kidsnote 에 저장
                                              ▼
            Home Assistant ── REST 센서 · 사진 12칸(이미지 엔티티) · 네트워크 저장소(미디어)
                                              ▼
              HA 로그인으로만 보는 '키즈노트' 화면 (오늘 편지 · 사진 · 달력 · 공지)
```

## 무엇을 하나

**백업 (NAS)**
- 알림장(본문·사진·동영상·첨부·댓글·식사/수면/배변/날씨), 공지, 앨범, 식단(+사진)을 **처음 글부터 전부** 받습니다.
- 매일 18:00 자동 수집 + HA '불러오기' 버튼으로 즉시 수집.
- 요청 사이 1초 간격. 이미 받은 파일은 건너뛰고, 실패한 파일은 다음 실행 때 다시 받습니다.
- 최근 7일 글은 매번 다시 확인해 수정·댓글을 반영합니다.
- 공지 댓글은 다른 가정 학부모 댓글이 섞이므로 선생님 댓글만 저장합니다.

**화면 (HA)**
- 어린이집에서 온 편지: 고른 날짜의 알림장 전부 (같은 날 여러 건이면 시간 순으로 이어서)
- 오늘의 순간: 그날 사진 12장씩 페이지 (3열 × 4줄, `‹ 1/3 ›` 또는 마지막 칸 `+N`)
- 달력: 아이폰 캘린더 스타일. 알림장 있는 날 점(주황 = 사진 있음, 회색 = 글만),
  고른 날 주황 원, 오늘 주황 굵은 글씨. 제목을 누르면 12개월 선택판.
- 최근 공지 3개, 불러오기 버튼, 수집 상태
- 자정 · 수집 직후 · 다른 날을 10분 넘게 보고 있으면 자동으로 오늘로 복귀
- 미디어 보관함: NAS 폴더를 HA 미디어로 연결해 전체 사진·영상을 날짜별로 열람

**보안**
- 8765 포트는 **공유기 포트포워딩·역방향 프록시에 절대 등록하지 않습니다.** 밖에서는 HA를 통해서만 봅니다.
- 사진은 HA가 NAS에서 받아서 HA 로그인 사용자에게만 보여 줍니다. NAS 주소는 폰에 노출되지 않습니다.
- 키즈노트 아이디/비밀번호는 NAS의 `kidsnote.env` 에만 둡니다. (GitHub·채팅에 올리지 않기)

---

## 저장소 구성

```
tools/kidsnote_fetch/        기존 키즈노트 로그인·수집 코드 (NAS 컨테이너가 같이 씀 — 꼭 있어야 함)
nas/
  collector.py               수집기 (NAS 저장 형식)
  server.py                  LAN 조회 서버 (:8765) + 매일 18:00 스케줄러
  Dockerfile                 빌드 위치는 저장소 최상위 (tools/ 를 같이 넣기 위해)
  docker-compose.yml         시놀로지 Container Manager 프로젝트용
  .env.example               → NAS 에서 kidsnote.env 로 복사해 채움
  .gitignore                 kidsnote.env 가 GitHub 에 올라가지 않게
ha/
  packages/kidsnote.yaml               HA 패키지 (센서·사진 12칸·도우미·스크립트·자동화)
  dashboard_kidsnote_templates.yaml    버튼카드 템플릿 (달력 칸 kn_cal_cell, 12개월 칸 kn_month_cell)
  dashboard_kidsnote_view.yaml         '키즈노트' 뷰
```

> 웹으로 GitHub 에 올릴 때 `.env.example`, `.gitignore` 처럼 점(.)으로 시작하는 파일은 빠지기 쉽습니다.
> 빠졌으면 **Add file → Create new file** 로 `nas/.gitignore` (내용: `kidsnote.env` 한 줄) 를 직접 만드세요.

---

## 1. 시놀로지 준비

### 1-1. 공유폴더 만들기 (백업 저장 위치)
**제어판 → 공유 폴더 → 생성**
- 이름: `kidsnote` (소문자 정확히) · 위치: **볼륨 1** → 경로 `/volume1/kidsnote`
- 관리자 계정에 읽기/쓰기

> 이 폴더가 없으면 컨테이너가 `Bind mount failed: '/volume1/kidsnote' does not exist` 로 켜지지 않습니다.
> 볼륨 2 등에 만들었다면 `nas/docker-compose.yml` 의 `/volume1/kidsnote` 를 그 경로로 바꾸세요.

### 1-2. 코드 내려받기 — 저장소 **전체**
1. GitHub 저장소 → 초록 **Code → Download ZIP**
2. File Station → `docker` 공유폴더에 ZIP 업로드 → 오른쪽 클릭 **압축 풀기 → 여기에**
   (`docker` 폴더가 없으면 패키지 센터에서 **Container Manager** 를 먼저 설치)
3. 생긴 폴더 이름을 `kidsnote-backup` 으로 변경

최종 모양:
```
/volume1/docker/kidsnote-backup/
  tools/kidsnote_fetch/fetch.py …     ← 반드시 있어야 함
  nas/docker-compose.yml …
  ha/ …
```

> `tools` 폴더가 없으면 빌드 중 `COPY failed: ... tools/kidsnote_fetch/: not found` 오류가 납니다.
> `nas`, `ha` 만 따로 올리지 말고 **저장소 ZIP 전체**를 풀어야 합니다.

### 1-3. 비밀번호 파일 `kidsnote.env`
1. 패키지 센터에서 **텍스트 편집기** 설치
2. File Station → `docker/kidsnote-backup/nas/.env.example` 선택 → 위쪽 **작업 → 연결 프로그램 → 텍스트 편집기**
   (파일이 안 보이면 File Station **설정 → 숨김 파일 표시**)
3. 값 채우기 (`=` 뒤에 공백 없이)
   ```
   KIDSNOTE_USERNAME=키즈노트아이디
   KIDSNOTE_PASSWORD=키즈노트비밀번호
   KIDSNOTE_RUN_KEY=영문숫자섞은32자이상아무문자열
   ```
   - `KIDSNOTE_RUN_KEY` 는 HA '불러오기' 버튼 전용 비밀키입니다. HA `secrets.yaml` 에도 똑같이 넣습니다.
   - 아이가 한 명이면 `KIDSNOTE_CHILD_NAME` 은 비워도 됩니다.
   - 키즈노트 2단계 인증을 켠 계정만 `KIDSNOTE_SESSION_COOKIE` 필요 (14일마다 갱신).
4. **파일 → 다른 이름으로 저장** → 같은 `nas` 폴더에 `kidsnote.env` (끝에 `.txt` 가 붙지 않게)

---

## 2. 컨테이너 실행

**Container Manager → 프로젝트 → 생성**
- 프로젝트 이름: `kidsnote`
- 경로: `docker → kidsnote-backup → nas`
- 원본: **기존 docker-compose.yml 사용** (자동 선택됨)
- 웹 포털 설정: 사용 안 함 → **완료** (첫 빌드 1~3분)

처음 켜지면 **바로 전체 백필**이 시작됩니다. 로그 예:
```
키즈노트 서버 시작 :8765 (매일 18:00 수집)
수집 시작 (startup)
키즈노트 로그인 성공 (아이디/비번)
알림장 183건 확인
공지 99건 확인
앨범 0건 확인            ← 어린이집이 앨범 메뉴를 안 쓰면 0건이 정상
식단 500일 확인
수집 완료: {...}
```
- 소요 시간 예: 알림장 183건 약 20분, 식단 500일(사진 포함) 약 30~40분.
- 중간에 꺼져도 다음 실행 때 이어서 받습니다.
- 진행 상황: 컨테이너 → `kidsnote-nas` → **로그**, 또는 `/volume1/kidsnote/logs/collector.log`

**확인** (집 와이파이에 연결된 PC·폰 브라우저)
- `http://NAS내부IP:8765/health` → `ok`
- `http://NAS내부IP:8765/api/summary` → JSON 안에 `"report"`, `"calendar"` 가 보이면 정상

**(선택) 더 잠그기**: 제어판 → 보안 → 방화벽 → 8765 포트는 HA IP 에서만 허용

---

## 3. Home Assistant 설정

### 3-1. 패키지
1. `configuration.yaml` 에 packages 가 없으면 추가
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. File editor 로 `/config/packages/` 폴더를 만들고 `kidsnote.yaml` 생성 → `ha/packages/kidsnote.yaml` 내용 붙여넣기
3. **Ctrl + H** 로 `192.168.0.10` → NAS 내부 IP 로 **모두 바꾸기** (14군데)
4. `/config/secrets.yaml` 맨 아래에 추가 (줄 맨 앞 공백 없이, 콜론 한 번)
   ```yaml
   kidsnote_run_key: "kidsnote.env 의 KIDSNOTE_RUN_KEY 와 똑같은 값"
   ```
5. **개발자 도구 → YAML → 구성 확인 → 다시 시작** (도우미가 새로 생기므로 재시작 필수)

생기는 엔티티

| 엔티티 | 용도 |
|---|---|
| `sensor.kidsnote_summary` | 상태(ok/running/error/never) + 속성 report · today · calendar · notices · run |
| `sensor.kidsnote_photo_count` | 고른 날 사진 수 |
| `sensor.kidsnote_page_photo_count` | 지금 페이지에 보일 칸 수 (+ page, page_count, remaining) |
| `image.kidsnote_photo_1` ~ `12` | 사진 12칸 = 한 페이지 |
| `input_datetime.kidsnote_date` | 달력에서 고른 날짜 |
| `input_number.kidsnote_photo_page` | 사진 페이지 번호 |
| `input_boolean.kidsnote_month_picker` / `input_number.kidsnote_picker_year` | 12개월 선택판 열림 / 연도 |
| `script.kidsnote_load` | 불러오기 버튼 (수집 끝날 때까지 10초마다 새로고침) |
| `script.kidsnote_photo_next` / `_prev` | 사진 페이지 넘기기 |
| `script.kidsnote_cal_prev` / `_next` / `_today` | 달력 이전 / 다음 / 오늘 (선택판이 열려 있으면 ‹ › 는 연도) |
| `script.kidsnote_picker_toggle` / `kidsnote_pick_month` | 선택판 열기·닫기 / 월 고르기 |
| 자동화 4개 | 18:05·18:20 새로고침, 새 글이면 사진 1페이지로, 날짜 바뀌면 새로고침, 오늘로 복귀 |

### 3-2. 대시보드 — 템플릿 + 뷰
이 화면은 기존 대시보드의 버튼카드 템플릿 `hero`, `info_tile`, `tile_gap`, `spacer_bottom` 을 그대로 씁니다.
필요한 커스텀 카드: **button-card, vertical-stack-in-card, card-mod**

대시보드 → 편집 → ⋮ → **원본 구성 편집기**

**① 템플릿** — 파일 맨 위
```yaml
title: 내 대시보드
kiosk_mode:
  hide_header: true
button_card_templates:
  kn_cal_cell:          ← 여기에 dashboard_kidsnote_templates.yaml 내용 (주석 줄 제외)
    ...
  kn_month_cell:
    ...
  tile:                 ← 원래 있던 템플릿 (같은 들여쓰기 = 공백 2칸)
```
`button_card_templates:` 줄 끝에서 엔터 → 새 줄 맨 앞 칸에 붙여넣기.
업데이트할 때는 기존 `kn_cal_cell:` ~ `tile:` 바로 위 줄까지 지우고 새로 붙입니다.

**② 뷰** — `views:` 목록 안, 홈 뷰 다음
```yaml
  - title: 홈
    ...
          - type: custom:button-card
            template: spacer_bottom
  - title: 키즈노트        ← dashboard_kidsnote_view.yaml 내용 (주석 줄 제외)
    path: kidsnote
    ...
  - title: 조명            ← 원래 있던 뷰
```
- `- title: 키즈노트` 가 다른 뷰의 `- title:` 과 같은 열(공백 2칸)이어야 합니다.
- 업데이트할 때는 `- title: 키즈노트` 부터 `- title: 조명` 바로 위 줄까지 지우고 새로 붙입니다.
- 뷰 순서 = 좌우 스와이프 순서입니다.

### 3-3. 홈 화면 타일 (선택) — 키즈노트 · 보관함
홈 뷰의 가족(사람) 타일 그리드(`columns: 2`) 안, 마지막 타일 바로 아래에 이어서 붙이면
2열로 `키즈노트 | 보관함` 이 생깁니다. (들여쓰기 = 기존 `- type: custom:button-card` 와 같은 열)
```yaml
                  - type: custom:button-card
                    template: info_tile
                    entity: sensor.kidsnote_summary
                    name: 키즈노트
                    icon: mdi:teddy-bear
                    variables:
                      on_states:
                        - running
                    state_display: |
                      [[[
                        if (!entity || ['unknown','unavailable'].includes(entity.state)) return 'NAS 연결 없음';
                        if (entity.state === 'running') return '불러오는 중';
                        const t = entity.attributes.today;
                        if (!t) return '알림장 없음';
                        return t.has_report ? `오늘 알림장${t.photo_count ? ' · 사진 ' + t.photo_count + '장' : ''}` : '최근 ' + t.latest_label;
                      ]]]
                    tap_action:
                      action: navigate
                      navigation_path: kidsnote
                  - type: custom:button-card
                    template: info_tile
                    entity: sensor.kidsnote_summary
                    name: 보관함
                    icon: mdi:folder-image
                    show_state: true
                    state_display: '[[[ return ''사진 · 영상 전체''; ]]]'
                    variables:
                      on_states: []
                    tap_action:
                      action: navigate
                      navigation_path: /media-browser/browser
```
> 타일을 붙이거나 블록을 옮길 때 그 아래 `card_mod:` 블록(패널 여백)과 `spacer_bottom` 이
> 같이 지워지지 않았는지 확인하세요. 지워지면 타일이 패널 끝에 붙고 아래 여백이 사라집니다.

### 3-4. 하단 탭바 (ios-navbar.js 를 쓰는 경우)
하단 탭바는 뷰 목록에서 자동으로 만들어지지 않습니다. `/config/www/ios-navbar.js` 의 `VIEWS` 목록에 한 줄 추가:
```js
  const VIEWS = [
    ["home", "홈", "mdi:home"],
    ["kidsnote", "키즈노트", "mdi:teddy-bear"],   // ← 추가 (path 는 뷰의 path: kidsnote 와 같아야 함)
    ["lights", "조명", "mdi:lightbulb-group"],
    ...
```
JS 를 고친 뒤 폰에 바로 반영하려면 `configuration.yaml` 의 주소에 버전을 붙이고 HA 재시작:
```yaml
frontend:
  extra_module_url:
    - /local/ios-navbar.js?v=2     # 고칠 때마다 숫자 올리기
```
(HA 앱은 Companion App → 디버깅 → 프런트엔드 캐시 초기화, Safari 홈 화면 웹앱은 버전 숫자로 해결)

### 3-5. 미디어 보관함 (NAS 폴더를 HA 미디어로)
**시놀로지**
1. 제어판 → 사용자 및 그룹 → 생성 → `ha_media`
   - 공유 폴더 권한: `kidsnote` **읽기 전용**, 나머지 액세스 없음
   - 애플리케이션 권한: **SMB 허용**
2. 제어판 → 파일 서비스 → SMB → **고급 설정**
   - 최대 SMB 프로토콜 **SMB3**, 최소 **SMB2**

**HA** → 설정 → 시스템 → 저장소 → **네트워크 저장소 추가**
- 이름 `kidsnote` · 용도 **미디어** · 서버 NAS 내부 IP · 프로토콜 **Samba/Windows (CIFS)**
- 원격 공유 `kidsnote` (경로 말고 공유 이름만) · 사용자 `ha_media` / 비밀번호

→ 미디어 → 내 미디어 → `kidsnote/reports/2026/20261027_…/` 에서 날짜별 사진·동영상

> `Mount kidsnote is not reachable` 이면 설정 → 시스템 → 로그 → **Host** 로그에서 `mount error(숫자)` 확인
> - `error(95)` / `Dialect not supported` → 시놀로지 SMB 최대 버전을 SMB3 으로
> - `error(13)` → 비밀번호 · 폴더 권한 · SMB 앱 권한
> - `error(2)` → 원격 공유 이름 오타

---

## NAS 저장 구조 (`/volume1/kidsnote`)

```
reports/2026/20261027_163012_<id>/   report.json(상세·댓글)  note.txt  image_001.jpg …  video_001.mp4  file_001_….pdf
notices/2026/20261020_<id>/          notice.json  image_001.jpg …
albums/2026/20261015_<id>/           album.json   image_001.jpg …
menus/2026-10.json                   menus/img/2026-10/20261027_lunch.jpg
state.json                           logs/collector.log
```
폴더 이름 앞 8자리가 날짜라서 날짜순으로 정렬됩니다.

## 조회 서버 API (LAN 전용)

| 주소 | 내용 |
|---|---|
| `GET /health` | `ok` |
| `GET /api/summary?date=YYYY-MM-DD` | 그날 알림장 전부(오늘인데 없으면 최근 날), 그 달 달력, 오늘 요약, 공지 3개, 수집 상태. date 없으면 오늘 |
| `GET /photo/<n>?page=<p>&date=YYYY-MM-DD` | 그날 사진 중 p페이지 n번째 (없으면 투명 1px) |
| `POST /api/run` (헤더 `X-Run-Key`) | 지금 수집 시작 (202) / 이미 수집 중 (409) / 키 불일치 (403) |

## 설정값 (`kidsnote.env`)

| 키 | 기본 | 설명 |
|---|---|---|
| `RUN_AT` | 18:00 | 매일 자동 수집 시각 (한국시간) |
| `REQUEST_DELAY` | 1.0 | 키즈노트 요청 간격(초). 줄이지 마세요 |
| `REFRESH_DAYS` | 7 | 최근 며칠 글을 매번 다시 확인 |
| `RUN_ON_START` | auto | 켤 때 백필 (auto = 성공 기록 없을 때만) |
| `PHOTOS_PER_PAGE` | 12 | 사진 한 페이지 장수. HA 사진 칸(12개)과 같게 둘 것 |
| `BACKUP_NOTICES` / `BACKUP_ALBUMS` / `BACKUP_MENUS` | true | 항목별 백업 여부 |
| `KIDSNOTE_SESSION_COOKIE` | | 2단계 인증 계정용 (14일마다 갱신) |

---

## 코드 업데이트

### NAS (server.py / collector.py 가 바뀐 경우)
1. File Station 에서 `docker/kidsnote-backup/nas/` 에 새 파일 업로드 → **덮어쓰기**
2. Container Manager
   1. **컨테이너** → `kidsnote-nas` → 작업 → **중지** → **삭제**
   2. **이미지** → `kidsnote-nas` → **삭제**
   3. **프로젝트** → `kidsnote` 선택 → 작업 → **빌드**
3. 확인: `http://NAS내부IP:8765/api/summary` 에 새 항목이 보이는지

> '빌드' 만 누르거나 '재설정' 하면 예전 이미지로 다시 켜지는 경우가 있습니다. 위 순서대로 지우고 빌드하세요.
> 백업 데이터(`/volume1/kidsnote`)와 `kidsnote.env` 는 컨테이너 밖에 있어서 지워지지 않습니다.

### HA
- 패키지: `kidsnote.yaml` 전체 교체 → IP 바꾸기(Ctrl+H) → 구성 확인 → 재시작
- 대시보드: 템플릿 · 뷰 블록 교체 (3-2 참고)

---

## 문제 해결

| 증상 | 확인 |
|---|---|
| 빌드 중 `COPY failed: tools/kidsnote_fetch` | 저장소 ZIP 전체를 풀었는지 (`kidsnote-backup/tools/` 존재) |
| `Bind mount failed: '/volume1/kidsnote' does not exist` | 공유폴더 `kidsnote` 생성 / 볼륨 경로 |
| 수정한 코드가 반영 안 됨 | 컨테이너 삭제 → 이미지 삭제 → 프로젝트 빌드 |
| 화면 상태 "NAS 연결 없음" | HA 에서 NAS IP:8765 접근 가능? 패키지 IP 오타? NAS 방화벽? |
| "수집 오류 · 로그인 실패" | `kidsnote.env` 아이디/비번. 2단계 인증이면 `KIDSNOTE_SESSION_COOKIE` |
| 불러오기 버튼 반응 없음 | `secrets.yaml` 의 키 ≠ `KIDSNOTE_RUN_KEY` (서버 403) |
| `secrets.yaml` 오류 `mapping values are not allowed` | 그 줄에 콜론 두 번 / 앞 공백 / 따옴표 누락 |
| 편지가 "알림장 없음", 달력 월 이름이 안 나옴 | NAS 서버가 예전 버전 → 위 '코드 업데이트' |
| 달력 날짜를 눌러도 반응 없음 | `input_datetime.kidsnote_date` 없음 → 패키지 교체 후 재시작 |
| 날짜를 바꿔도 사진이 그대로 | 최신 뷰 파일인지 (사진 주소에 갱신 시각을 붙이는 버전) |
| 12개월 선택판에서 연도가 안 바뀜 | `input_number.kidsnote_picker_year` 존재 여부 (패키지 최신인지) |
| 사진 칸이 비어 있음 | `http://NAS내부IP:8765/photo/1?page=1` 이 사진을 주는지, HA 로그 template image 오류 |
| 일부 사진 누락 | 다음 18:00 실행 때 자동 재시도. 급하면 불러오기 버튼 |
| 하단 탭바에 키즈노트가 없음 | `ios-navbar.js` 의 `VIEWS` 에 추가 + 캐시 (3-4) |
