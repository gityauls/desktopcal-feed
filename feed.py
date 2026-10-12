"""
feed.py - 배포판 중계 파일 (2026-10-10 사용자 '배포 C 안 — 중계로 진행')

GitHub 자동 작업(relay/relay.py)이 내 키로 전국 공통 자료를 받아 공개 주소(GitHub Pages)에 파일로 올리고,
키가 없는 앱은 같은 요청을 그 파일로 받는다 — 사용자가 몇 명이든 내 키로 부르는 수는 그대로다.
  * 어떤 요청이 어느 파일인지(path_for)는 이 모듈 하나로 정한다. 중계 저장소에도 이 파일을 그대로 복사해 쓴다
    (그래서 표준 라이브러리만, 패키지 안 다른 모듈을 부르지 않는다).
  * 파일 = {'fetched': 받은 시각(한국), 'status': 200, 'text': 원래 응답} — 앱은 원래 응답을 그대로 해석한다.
  * 중계가 멈춰 오래된 파일은 쓰지 않는다 (Route.max_age) — 특보가 며칠 전 것으로 남지 않게.
  * 중계하지 않는 것: 아파트 실거래(시군구 250곳), 재난문자(부르는 PC 의 IP 등록), 주가·공시(개인 키),
    기상청 동네예보(1차는 키 없는 Open-Meteo 그대로).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import quote

BASE_URL = 'https://gityauls.github.io/desktopcal-feed/'
KEY = 'relay'                   # 중계로 받을 때 공급자에게 건네는 자리표시 키 (어디로도 보내지 않는다)
KEY_PARAMS = ('serviceKey', 'key', 'authkey')

_DG = 'https://apis.data.go.kr'
WARNING = _DG + '/1360000/WthrWrnInfoService/getPwnStatus'
MID_LAND = _DG + '/1360000/MidFcstInfoService/getMidLandFcst'
MID_TA = _DG + '/1360000/MidFcstInfoService/getMidTa'
FIRE = _DG + '/1400377/forestPointV2/forestPointListSigunguSearchV2'
DUST_ALARM = _DG + '/B552584/UlfptcaAlarmInqireSvc/getUlfptcaAlarmInfo'
DUST_CAST = _DG + '/B552584/ArpltnInforInqireSvc/getMinuDustFrcstDspth'
AIR_NOW = _DG + '/B552584/ArpltnInforInqireSvc/getMsrstnAcctoRltmMesureDnsty'   # 앱: 측정소 하나씩
AIR_SIDO = _DG + '/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty'       # 중계: '전국' 한 번에 → 측정소별 파일
STATIONS = _DG + '/B552584/MsrstnInfoInqireSvc/getMsrstnList'
FESTIVAL = _DG + '/B551011/KorService2/searchFestival2'
HOLIDAYS = _DG + '/B090041/openapi/service/SpcdeInfoService/getRestDeInfo'
KOBIS = 'https://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json'
EXIM = 'https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON'

NO_DATA = json.dumps({'response': {'header': {'resultCode': '03', 'resultMsg': 'NO_DATA'}}})


@dataclass(frozen=True)
class Route:
    prefix: str
    keys: tuple[str, ...]           # 파일 이름에 들어가는 매개변수 (나머지는 늘 같은 값)
    max_age: timedelta              # 이보다 오래된 파일은 쓰지 않는다 (중계가 멈췄다)
    missing: str | None = None      # 파일이 아직 없을 때 '자료 없음' 응답 (앱이 앞 발표·전날로 넘어가게). None = 404
    back: str = ''                  # 파일이 없으면 이 날짜 매개변수를 하루 앞으로 한 번 더 (자정 직후 — 축제)


ROUTES = {
    WARNING: Route('kma/warning', (), timedelta(hours=2)),
    MID_LAND: Route('kma/mid-land', ('regId', 'tmFc'), timedelta(days=2), missing=NO_DATA),
    MID_TA: Route('kma/mid-ta', ('regId', 'tmFc'), timedelta(days=2), missing=NO_DATA),
    FIRE: Route('forest/fire', (), timedelta(hours=12)),
    DUST_ALARM: Route('air/alarm', ('year',), timedelta(hours=3)),
    DUST_CAST: Route('air/forecast', ('searchDate',), timedelta(hours=8), missing=NO_DATA),
    AIR_NOW: Route('air/now/', ('stationName',), timedelta(hours=4)),
    STATIONS: Route('air/stations', (), timedelta(days=40)),
    FESTIVAL: Route('tour/festival', ('lDongRegnCd', 'eventStartDate'), timedelta(days=2), back='eventStartDate'),
    HOLIDAYS: Route('kasi/holidays', ('solYear',), timedelta(days=60)),
    KOBIS: Route('kobis/daily', ('targetDt',), timedelta(days=30),
                 missing=json.dumps({'boxOfficeResult': {'dailyBoxOfficeList': []}})),
    EXIM: Route('exim/ap01', ('searchdate',), timedelta(days=30), missing='[]'),
}

# 중계가 미리 받아 둘 목록 — 앱(providers/midterm.py · regions_kr.json)과 같아야 한다 (tests/test_marquee_feed.py 가 맞춰 본다)
SIDO_CODES = ('11', '12', '26', '27', '28', '30', '31', '36', '41', '43', '44', '47', '48', '50', '51', '52')
MID_LAND_IDS = ('11B00000', '11C10000', '11C20000', '11D10000', '11D20000', '11F10000', '11F20000', '11G00000',
                '11H10000', '11H20000')
MID_TA_IDS = ('11A00101', '11B10101', '11B20101', '11B20102', '11B20201', '11B20203', '11B20301', '11B20302', '11B20305',
              '11B20401', '11B20403', '11B20502', '11B20503', '11B20601', '11B20602', '11B20604', '11B20605', '11B20606',
              '11B20612', '11B20701', '11B20703', '11C10101', '11C10201', '11C10301', '11C20101', '11C20104', '11C20201',
              '11C20301', '11C20401', '11C20402', '11C20404', '11C20602', '11D10101', '11D10301', '11D10302', '11D10401',
              '11D10501', '11D20301', '11D20401', '11D20501', '11D20601', '11D20602', '11E00101', '11F10201', '11F10202',
              '11F10203', '11F10401', '11F20301', '11F20401', '11F20405', '11F20501', '11F20503', '11G00201', '11G00401',
              '11H10101', '11H10201', '11H10202', '11H10302', '11H10401', '11H10501', '11H10602', '11H10701', '11H20101',
              '11H20102', '11H20201', '11H20301', '11H20304', '11H20401', '11H20403', '11H20502', '11H20701', '21F10501',
              '21F20801')
MID_RELEASES = (6, 18)                        # 중기예보 발표 06·18시, 10분쯤 뒤에 올라온다 (providers/midterm.issued 와 같게)
MID_READY = timedelta(minutes=10)


def mid_issued(now: datetime) -> datetime:
    """지금 볼 수 있는 가장 늦은 중기예보 발표 — providers/midterm.issued 와 같은 규칙."""
    for d in (now.date(), now.date() - timedelta(days=1)):
        for h in reversed(MID_RELEASES):
            at = datetime.combine(d, datetime.min.time()).replace(hour=h)
            if at + MID_READY <= now:
                return at
    return datetime.combine(now.date() - timedelta(days=1), datetime.min.time()).replace(hour=MID_RELEASES[-1])


def _safe(v) -> str:
    """파일 이름에 넣을 값 — 경로 문자·공백은 '_' (측정소 이름은 한글 그대로)."""
    return re.sub(r'[\s/\\:*?"<>|]+', '_', str(v if v is not None else '').strip())


def path_for(url: str, params: dict) -> str | None:
    """이 요청의 중계 파일 경로 (중계하지 않는 요청이면 None)."""
    route = ROUTES.get(url)
    if route is None:
        return None
    parts = [_safe(params.get(k)) for k in route.keys]
    if any(not p for p in parts):
        return None
    if not parts:
        return route.prefix + '.json'
    joiner = '' if route.prefix.endswith('/') else '-'
    return route.prefix + joiner + '-'.join(parts) + '.json'


def plain(params: dict) -> dict:
    """키를 뺀 매개변수 (값은 글로) — 앱이 묻는 것과 중계가 묻는 것을 맞춰 볼 때."""
    return {k: str(v) for k, v in params.items() if k not in KEY_PARAMS}


def wrap(text: str, fetched: datetime, status: int = 200) -> str:
    return json.dumps({'fetched': fetched.strftime('%Y-%m-%dT%H:%M:%S'), 'status': status, 'text': text},
                      ensure_ascii=False)


def unwrap(body: str) -> tuple[datetime, int, str]:
    """중계 파일 → (받은 시각, 상태, 원래 응답). 모양이 다르면 ValueError."""
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get('text'), str):
        raise ValueError('중계 파일 모양이 다릅니다')
    return datetime.fromisoformat(str(data['fetched'])[:19]), int(data.get('status', 200)), data['text']


def _day_before(params: dict, key: str) -> dict | None:
    raw = str(params.get(key) or '')
    digits = re.sub(r'\D', '', raw)
    try:
        d = datetime.strptime(digits[:8], '%Y%m%d').date() - timedelta(days=1)
    except ValueError:
        return None
    return {**params, key: d.isoformat() if '-' in raw else d.strftime('%Y%m%d')}


class Feed:
    """중계 파일 받기 — 공급자의 get 자리에 들어간다: (url, params) → (상태, 원래 응답).
    http: (주소, 매개변수) → (상태, 글) — datagokr.http_get_text 와 같은 모양."""

    def __init__(self, http: Callable[[str, dict], tuple[int, str]], *,
                 base_fn: Callable[[], str] = lambda: BASE_URL, clock: Callable[[], datetime] = datetime.now):
        self.http, self.base_fn, self.clock = http, base_fn, clock
        self.last: datetime | None = None     # 가장 최근에 받은 파일의 중계 시각 (설정 창 '지금 쓰는 곳')

    def url_of(self, path: str) -> str:
        return (self.base_fn() or BASE_URL).rstrip('/') + '/' + quote(path)

    def _file(self, path: str) -> tuple[int, str, datetime | None]:
        status, body = self.http(self.url_of(path), {})
        if status != 200:
            return status, '', None
        try:
            fetched, st, text = unwrap(body)
        except (ValueError, TypeError):
            return 502, '', None
        return st, text, fetched

    def get(self, url: str, params: dict) -> tuple[int, str]:
        route, path = ROUTES.get(url), path_for(url, params)
        if route is None or path is None:
            return 404, ''
        status, text, fetched = self._file(path)
        if status == 404 and route.back:
            prev = _day_before(params, route.back)
            prev_path = path_for(url, prev) if prev else None
            if prev_path:
                status, text, fetched = self._file(prev_path)
        if status == 404 and route.missing is not None:
            return 200, route.missing
        if fetched is not None:
            if self.clock() - fetched > route.max_age:
                return 503, ''                    # 중계가 멈춰 오래된 자료 — 쓰지 않는다
            self.last = fetched if self.last is None else max(self.last, fetched)
        return status, text

    def __call__(self, url: str, params: dict) -> tuple[int, str]:
        return self.get(url, params)

    def get_json(self, url: str, params: dict):
        """fx.http_get_json 자리 (수출입은행 환율) — 실패는 예외."""
        status, text = self.get(url, params)
        if status != 200:
            raise RuntimeError(f'중계 파일 {status}')
        return json.loads(text)


def chooser(real_key: Callable[[], str], direct: Callable, relay: Callable,
            on: Callable[[], bool]) -> tuple[Callable[[], str], Callable]:
    """(key_fn, get) — 내 키가 있으면 바로 부르고, 없고 중계를 쓰면 중계 파일 (설정을 바꾸면 다음 수집부터)."""
    def use_relay() -> bool:
        return not (real_key() or '').strip() and bool(on())

    def key() -> str:
        return KEY if use_relay() else (real_key() or '')

    def get(url, params):
        return (relay if use_relay() else direct)(url, params)
    return key, get
