"""
relay.py - DesktopCal 중계 (GitHub 자동 작업이 10분마다 — 2026-10-10 사용자 '배포 C 안')

내 키(저장소 Secrets → 환경 변수)로 전국 공통 공공데이터를 받아 site/ 아래 파일로 둔다. 그 파일을 GitHub Pages 가
공개 주소로 내고, 키가 없는 DesktopCal 은 그 파일만 읽는다 — 사용자가 늘어도 내 키로 부르는 수는 그대로다.
  * 받을 때가 된 것만 부른다 (site/index.json 에 언제 받았는지). 발표 전·휴일 같은 빈 응답은 30분마다 다시.
  * 키는 어디에도 남기지 않는다 — 공개 저장소라 실행 기록을 누구나 본다. 주소·매개변수·예외 글을 찍지 않고
    파일 경로와 상태 번호만.
  * 실패하면 앞서 받은 파일을 그대로 둔다 (앱은 파일 속 받은 시각으로 너무 오래된 것을 버린다).
  * 어떤 요청이 어느 파일인지는 feed.py — 앱의 marquee_core/feed.py 를 그대로 복사한 것.
실행: python relay.py site   (환경 변수 DATA_GO_KR_KEY · EXIM_KEY · KOBIS_KEY)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

try:
    from marquee_core import feed as F          # 앱 저장소 (테스트)
except ImportError:                             # 중계 저장소 — feed.py 가 옆에
    import feed as F

KST = timezone(timedelta(hours=9), 'KST')        # 한국은 서머타임이 없다 (tzdata 없는 곳에서도)
SLACK = timedelta(minutes=3)                    # 예약 작업이 몇 분 늦게 돌아도 '때가 됐다'로
RETRY = timedelta(minutes=10)                   # 실패한 것은 다음 차례에 다시
EMPTY_RETRY = timedelta(minutes=30)             # 아직 발표 전(빈 응답)은 30분마다
RUN_BUDGET = 300                                # 한 번에 5분까지만 부른다 (나머지는 다음 차례)
DEAD_AFTER = 3                                  # 한 곳이 연달아 이만큼 연결 실패면 이번 차례엔 그만
OK_CODES = ('0', '00', '000', '0000')
PSEUDO = {F.AIR_SIDO: 'air/now-all'}            # 파일로 쓰지 않는 요청 (측정소별 파일로 나눈다) — 받은 때만 적는다

SITE_PAGE = """<!doctype html><meta charset="utf-8"><title>DesktopCal 중계 파일</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<body style="font-family:sans-serif;max-width:720px;margin:2em auto;padding:0 16px;line-height:1.6">
<h1>DesktopCal 중계 파일</h1>
<p>DesktopCal 달력 앱이 키 없이 쓰는 공공데이터 사본입니다. 원래 응답을 바꾸지 않고 받은 시각만 붙여 둡니다.</p>
<p>출처: 기상청(기상특보·중기예보) · 산림청 국립산림과학원(산불위험예보) · 환경부/한국환경공단 에어코리아(미세먼지
실측·예보·경보 — 인증받지 않은 실시간 자료라 실제와 다를 수 있습니다) · 한국관광공사(축제) · 한국천문연구원(특일 정보) ·
영화진흥위원회 KOBIS(박스오피스) · 한국수출입은행(환율). 공공데이터포털(data.go.kr) 이용허락 범위 안에서 출처를 밝혀 씁니다.</p>
</body>
"""


@dataclass
class Req:
    url: str
    params: dict
    ttl: timedelta | None                # 이만큼 지나면 다시 받는다 (None = 한 번 받으면 끝)
    final: datetime | None = None        # 이 시각 뒤에 받은 빈 응답은 '정말 없음'(휴일·발표 없음) — 다시 묻지 않는다
    fetch: bool = True                   # False = 받지는 않고 남겨 두기만 (자정 직후 앱이 전날 파일을 찾는다)

    @property
    def path(self) -> str:
        return PSEUDO.get(self.url) or F.path_for(self.url, self.params)


def plan(now: datetime) -> list[Req]:
    """지금(한국 시각) 있어야 할 파일 모두 — 앱의 공급자가 묻는 것과 같은 매개변수로 (키만 빼고)."""
    today = now.date()

    def ymd(d):
        return d.strftime('%Y%m%d')

    reqs = [Req(F.WARNING, {'pageNo': 1, 'numOfRows': 10, 'dataType': 'JSON'}, timedelta(minutes=10)),
            Req(F.FIRE, {'_type': 'json', 'numOfRows': 300, 'pageNo': 1}, timedelta(hours=3)),
            Req(F.DUST_ALARM, {'returnType': 'json', 'numOfRows': 500, 'pageNo': 1, 'year': now.year},
                timedelta(minutes=30)),
            Req(F.STATIONS, {'returnType': 'json', 'numOfRows': 1000, 'pageNo': 1, 'ver': '1.1'}, timedelta(days=7)),
            Req(F.AIR_SIDO, {'returnType': 'json', 'numOfRows': 1000, 'pageNo': 1, 'sidoName': '전국', 'ver': '1.3'},
                timedelta(hours=1))]
    for d in (today, today - timedelta(days=1)):                         # 05시 전엔 앱이 어젯밤 발표를 본다
        reqs.append(Req(F.DUST_CAST, {'returnType': 'json', 'numOfRows': 100, 'pageNo': 1, 'searchDate': d.isoformat()},
                        timedelta(hours=1)))
    latest = F.mid_issued(now)
    for at in (latest, latest - timedelta(hours=12)):                    # 막 발표된 때 앱은 앞 발표로
        for url, ids in ((F.MID_LAND, F.MID_LAND_IDS), (F.MID_TA, F.MID_TA_IDS)):
            for reg in ids:
                reqs.append(Req(url, {'dataType': 'JSON', 'numOfRows': 10, 'pageNo': 1, 'regId': reg,
                                      'tmFc': at.strftime('%Y%m%d%H%M')}, None, final=at + timedelta(hours=3)))
    for d, fetch in ((today, True), (today - timedelta(days=1), False)):
        for code in F.SIDO_CODES:
            reqs.append(Req(F.FESTIVAL, {'MobileOS': 'ETC', 'MobileApp': 'DesktopCal', '_type': 'json',
                                         'eventStartDate': ymd(d), 'lDongRegnCd': code, 'arrange': 'A',
                                         'numOfRows': 100, 'pageNo': 1}, timedelta(hours=6), fetch=fetch))
    for y in (now.year, now.year + 1):
        reqs.append(Req(F.HOLIDAYS, {'solYear': y, '_type': 'json', 'numOfRows': 100, 'pageNo': 1}, timedelta(days=7)))
    for back in (1, 2):                                                  # 어제 — 집계 전이면 앱이 그 전날로
        d = today - timedelta(days=back)
        reqs.append(Req(F.KOBIS, {'targetDt': ymd(d)}, None, final=datetime.combine(d + timedelta(days=1), time(12))))
    for back in range(11):                                               # 앱은 최근 영업일 둘을 열흘 안에서 찾는다
        d = today - timedelta(days=back)
        reqs.append(Req(F.EXIM, {'searchdate': ymd(d), 'data': 'AP01'}, timedelta(hours=1) if back == 0 else None,
                        final=datetime.combine(d + timedelta(days=1), time(0))))
    return reqs


def _t(s) -> datetime:
    try:
        return datetime.fromisoformat(str(s)[:19])
    except ValueError:
        return datetime.min


def _iso(t: datetime) -> str:
    return t.strftime('%Y-%m-%dT%H:%M:%S')


def due(req: Req, entry: dict | None, now: datetime) -> bool:
    if not req.fetch:
        return False
    if not entry:
        return True
    if not entry.get('ok'):
        return now - _t(entry.get('tried')) >= RETRY - SLACK
    fetched = _t(entry.get('fetched'))
    if entry.get('empty'):
        if req.final is not None and fetched >= req.final:
            return False
        wait = EMPTY_RETRY if req.ttl is None else min(req.ttl, EMPTY_RETRY)
        return now - fetched >= wait - SLACK
    return req.ttl is not None and now - fetched >= req.ttl - SLACK


def classify(url: str, status: int, text: str) -> str:
    """'ok' · 'empty'(자료 없음 — 발표 전·휴일) · 'error'(키·한도·점검 — 앞 파일을 그대로 둔다)."""
    if status != 200 or not (text or '').strip():
        return 'error'
    if url == F.KOBIS:
        try:
            data = json.loads(text)
        except ValueError:
            return 'error'
        if not isinstance(data, dict) or data.get('faultInfo'):
            return 'error'
        rows = (data.get('boxOfficeResult') or {}).get('dailyBoxOfficeList')
        if not isinstance(rows, list):
            return 'error'
        return 'ok' if rows else 'empty'
    if url == F.EXIM:
        try:
            rows = json.loads(text)
        except ValueError:
            return 'error'
        if rows is None:
            return 'empty'
        if not isinstance(rows, list):
            return 'error'
        if any(isinstance(r, dict) and r.get('result') not in (None, 1, '1') for r in rows):
            return 'error'                                               # 2 요청 오류 · 3 키 · 4 한도
        return 'ok' if rows else 'empty'
    if 'returnAuthMsg' in text:                                          # 공공데이터포털 인증 오류 (XML)
        return 'error'
    if text.lstrip().startswith('<'):
        m = re.search(r'<resultCode>\s*(\d+)\s*</resultCode>', text)
        code = m.group(1) if m else ''
        if code == '03':
            return 'empty'
        if code not in OK_CODES:
            return 'error'
        return 'ok' if '<item>' in text else 'empty'
    try:
        data = json.loads(text)
    except ValueError:
        return 'error'
    resp = data.get('response') if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        return 'error'
    head = resp.get('header') or {}
    code, msg = str(head.get('resultCode', '')), str(head.get('resultMsg', ''))
    if code == '03' or 'NO_DATA' in msg:
        return 'empty'
    if code not in OK_CODES:
        return 'error'
    items = (resp.get('body') or {}).get('items')
    if isinstance(items, dict):
        items = items.get('item')
    return 'ok' if items and not isinstance(items, str) else 'empty'


def key_param(url: str, keys: dict) -> dict:
    if url == F.KOBIS:
        return {'key': keys.get('kobis', '')}
    if url == F.EXIM:
        return {'authkey': keys.get('exim', '')}
    return {'serviceKey': keys.get('data', '')}


def normalize_key(key: str) -> str:
    key = (key or '').strip()
    return unquote(key) if '%' in key else key


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, 'utf-8')
    os.replace(tmp, path)


def station_files(text: str, now: datetime) -> dict[str, str]:
    """'전국' 실시간 측정 → 측정소별 파일 {경로: 내용} — 앱이 측정소 하나를 물을 때와 같은 응답 모양 (항목은 그대로)."""
    data = json.loads(text)
    items = ((data.get('response') or {}).get('body') or {}).get('items') or []
    if isinstance(items, dict):
        items = items.get('item') or []
    out = {}
    for it in items:
        name = str((it or {}).get('stationName') or '').strip()
        path = F.path_for(F.AIR_NOW, {'stationName': name}) if name else None
        if path:
            body = {'response': {'header': {'resultCode': '00', 'resultMsg': 'NORMAL_CODE'},
                                 'body': {'items': [it], 'totalCount': 1}}}
            out[path] = F.wrap(json.dumps(body, ensure_ascii=False), now)
    return out


def _route_prefixes() -> tuple[str, ...]:
    return tuple(r.prefix for r in F.ROUTES.values())


def run(site: Path, keys: dict, now: datetime, http: Callable[[str, dict], tuple[int, str]], *,
        log: Callable[[str], None] = print, budget: float = RUN_BUDGET, clock: Callable[[], float] = _time.monotonic) -> dict:
    """받을 때가 된 것을 받아 site/ 에 쓰고, 이제 필요 없는 파일은 지운다. 돌려줌: 경로 묶음별 {ok, empty, error, skip}."""
    site.mkdir(parents=True, exist_ok=True)
    index_path = site / 'index.json'
    try:
        index = json.loads(index_path.read_text('utf-8'))
    except (OSError, ValueError):
        index = {}
    files: dict = index.get('files') if isinstance(index.get('files'), dict) else {}
    start, fails, dead = clock(), {}, set()
    stats: dict[str, dict[str, int]] = {}
    wanted, stations = set(), None
    for req in plan(now):
        path = req.path
        wanted.add(path)
        if not due(req, files.get(path), now):
            continue
        group = stats.setdefault(PSEUDO.get(req.url) or F.ROUTES[req.url].prefix.rstrip('/'),
                                 {'ok': 0, 'empty': 0, 'error': 0, 'skip': 0})
        kp = key_param(req.url, keys)
        host = urlsplit(req.url).hostname
        if not next(iter(kp.values())) or host in dead or clock() - start > budget:
            group['skip'] += 1
            continue
        try:
            status, text = http(req.url, {**req.params, **kp})
            fails[host] = 0
        except Exception as e:                       # 예외 글에 주소(키)가 들어 있을 수 있다 — 종류만
            status, text = 0, ''
            fails[host] = fails.get(host, 0) + 1
            if fails[host] >= DEAD_AFTER:
                dead.add(host)
            log(f'✗ {path} — 연결 실패 ({type(e).__name__})')
        kind = classify(req.url, status, text)
        entry = dict(files.get(path) or {})
        entry['tried'] = _iso(now)
        if kind == 'error':
            entry['ok'] = False
            if status:
                log(f'✗ {path} — 응답 {status}' + _why(text))
        else:
            if req.url == F.AIR_SIDO:
                try:
                    stations = station_files(text, now)
                except (ValueError, AttributeError):
                    kind, stations = 'error', None
                    entry['ok'] = False
                else:
                    for p, body in stations.items():
                        _write(site / p, body)
            else:
                _write(site / path, F.wrap(text, now))
            if kind != 'error':
                entry.update(ok=True, empty=kind == 'empty', fetched=_iso(now))
        files[path] = entry
        group[kind] += 1
    # 이제 필요 없는 파일 (지난 날짜·지난 발표) — 측정소 파일은 이번에 새로 받았을 때만 정리
    prefixes = _route_prefixes()
    for f in sorted(site.rglob('*.json')):
        rel = f.relative_to(site).as_posix()
        if rel == 'index.json' or not rel.startswith(prefixes):
            continue
        if rel.startswith(F.ROUTES[F.AIR_NOW].prefix):
            if stations is not None and rel not in stations:
                f.unlink()
        elif rel not in wanted:
            f.unlink()
    files = {p: e for p, e in files.items() if p in wanted}
    _write(index_path, json.dumps({'updated': _iso(now), 'files': files}, ensure_ascii=False, indent=0))
    (site / '.nojekyll').write_text('', 'utf-8')
    if not (site / 'index.html').exists():
        _write(site / 'index.html', SITE_PAGE)
    return stats


def _why(text: str) -> str:
    """오류 응답의 까닭 글 (resultCode·resultMsg·returnAuthMsg 만 — 응답에 키가 들어 있지 않다)."""
    for pat in (r'<returnAuthMsg>([^<]{1,80})</returnAuthMsg>', r'"resultMsg"\s*:\s*"([^"]{1,80})"',
                r'"message"\s*:\s*"([^"]{1,80})"'):
        m = re.search(pat, text or '')
        if m:
            return f' ({m.group(1)})'
    return ''


def http_get(url: str, params: dict) -> tuple[int, str]:
    import requests
    resp = requests.get(url, params=params, timeout=(5, 30),          # 미세먼지 경보(한 해 목록)가 15초를 넘기곤 했다
                        headers={'User-Agent': 'DesktopCal-relay (+https://github.com/gityauls/desktopcal-feed)'})
    if not resp.encoding or resp.encoding.lower() == 'iso-8859-1':
        return resp.status_code, resp.content.decode('utf-8', 'replace')
    return resp.status_code, resp.text


def main(argv: list[str]) -> int:
    site = Path(argv[1] if len(argv) > 1 else 'site')
    keys = {'data': normalize_key(os.environ.get('DATA_GO_KR_KEY', '')),
            'exim': (os.environ.get('EXIM_KEY', '') or '').strip(),
            'kobis': (os.environ.get('KOBIS_KEY', '') or '').strip()}
    print('키: ' + ' · '.join(f"{name} {'✓' if keys[k] else '없음'}"
                              for k, name in (('data', '공공데이터포털'), ('exim', '수출입은행'), ('kobis', 'KOBIS'))))
    if not any(keys.values()):
        print('키가 하나도 없습니다 — 저장소 Settings → Secrets 에 넣어 주세요')
        return 1
    now = datetime.now(KST).replace(tzinfo=None)      # GitHub 서버는 UTC — 앱(한국 PC)과 같은 날짜·발표 시각으로
    stats = run(site, keys, now, http_get)
    for group, n in sorted(stats.items()):
        print(f"{group}: " + ' · '.join(f'{k} {v}' for k, v in n.items() if v))
    print(f'{now:%Y-%m-%d %H:%M} 한국 시각 — 받은 묶음 {len(stats)}개')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
