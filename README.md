# 테헤란로 501 출근권 · KB 선도아파트 맵 (v0.1)

서울·경기도에서 매매할 아파트를 찾기 위해 [KB부동산 월간 선도아파트](https://data.kbland.kr/kbstats/lead50)(`KB선도아파트50` + `경기 시세총액 TOP` + **핵심 출근권 18개 시/구 TOP 50**)와 **테헤란로 501 대중교통 통근시간**을 결합해 필터링할 수 있는 초경량 웹 맵입니다.

별도 Node.js 빌드나 백엔드 DB 없이 macOS 기본 `Python 3.9` 표준 라이브러리만으로 동작합니다.

---

## 1. `.env` 파일 설정 (무료 카카오 API 키)

```bash
cd /Users/hkeum/.gemini/antigravity/scratch/apt-commute-map
cp .env.example .env
```

`.env`에 Kakao Developers에서 발급한 키 2개를 입력합니다.

```env
VITE_KAKAO_JS_KEY=여기에_카카오_JavaScript_키
KAKAO_REST_API_KEY=여기에_카카오_REST_API_키
```

### Kakao Developers 체크리스트 (모두 무료 쿼터 내 0원)
1. [Kakao Developers](https://developers.kakao.com)에서 앱 생성
2. **[제품 설정] > [카카오맵] > 사용 설정 ON**
3. **[앱 설정] > [앱] > [플랫폼 키] > [JavaScript 키] > JavaScript SDK 도메인**에 아래 도메인 등록 후 저장:
   - 로컬용: `http://localhost:5173` (및 필요 시 `http://127.0.0.1:5173`)
   - 모바일 / GitHub Pages용: `https://rose-brown.github.io`

---

## 모바일 · GitHub Pages 접속

정적 호스팅 경로는 `docs/` 폴더(`docs/data/apartments.json`, `docs/config.json`)입니다.
- **라이브 주소**: https://rose-brown.github.io/lead50map/
- 모바일에서는 상단 `지도 / 목록 / 필터` 탭 바를 통해 한 화면씩 넓게 탐색할 수 있습니다.
- 새 시/구를 추가하거나 데이터를 갱신한 뒤에는 `docs/` 변경 사항을 `git push`하면 GitHub Pages에도 자동 반영됩니다.

---

## 2. 카카오 좌표 & 테헤란로 501 대중교통 시간 1회 실측 (영구 캐시)

```bash
python3 scripts/enrich_kakao.py
```

- **영구 1회 캐시 (`data/kakao_transit_cache.json`)**:
  - 한 번 조회한 단지와 좌표 쌍의 대중교통 소요시간은 `data/kakao_transit_cache.json`에 영구 저장됩니다.
  - 다시 실행하거나 KB 월간 데이터를 갱신해도 이미 측정된 단지는 **재호출 0건**으로 캐시에서 복원됩니다(`--force-refetch-transit` 옵션을 명시하지 않는 한 절대 재호출하지 않음).
  - 무료 대중교통 일일 쿼터(일 1,000건)를 넘기지 않도록 기본적으로 반경 `30km` 이내, 하루 누적 `950건` 상한으로 동작합니다.

---

## 3. 원하는 시/구 더 추가하기

현재 기본 프리셋(`core18`)에는 서울 10개 구(강남·서초·송파·강동·성동·광진·동작·용산·영등포·마포)와 경기 8개 시/구(과천·분당·수정·중원·하남·안양 동안·광명·수지)가 포함되어 있습니다.

다른 시/구를 50개씩 추가하려면 아래 한 줄만 실행하세요:

```bash
python3 scripts/fetch_kb_regions.py --regions "서울 양천구,경기 의왕시,경기 구리시" && python3 scripts/enrich_kakao.py
```

---

## 4. 로컬 웹 서버 실행

```bash
python3 server.py
```

브라우저에서 `http://localhost:5173` 을 열면 바로 탐색할 수 있습니다.

---

## 5. 월간 KB 데이터 갱신 방법

```bash
python3 scripts/fetch_kb_regions.py --preset core18
```

기존 [kakao_transit_cache.json](data/kakao_transit_cache.json)의 실측 좌표와 통근시간은 그대로 유지되면서 최신 KB 시세만 업데이트됩니다.

---

## v0.1 기능 범위 및 향후 v0.2 계획
- **v0.1**: `3.3㎡당 KB시세 × 기준 평형` 추정 매매가 + 기본 예산대(`4억 이하`, `4~6억`, `6~8억`, `8~12억`, `12억 이상`), 시/구 다중 필터, 세대수, 준공년월, 대중교통 통근시간/환승 필터, 줌 레벨별 카카오맵 클러스터링
- **v0.2 예정**: 단지별 실제 전용면적·평형별 KB시세/실거래가 연동
