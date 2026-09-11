"""渐进式探每个 vendor — 从近往老探, 步长递增, 上限 2023.

不像之前粗探每年 1 个采样 (老日期 timeout 浪费时间), 这里逐步往老挪:
- yesterday - 7d (一周前)
- - 30d (一月前)
- - 180d (半年前)
- - 365d (一年前)
- - 730d (两年前)
- - 1095d (三年前, ~2023)

某天没数据就停 (说明账号到这就开始没了), 别再往老打.
"""
import sys, os, datetime as dt, signal
sys.path.insert(0, '.')


def load_env():
    path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

from ingest.job import _load_vendor_config
from ingest.adapters import ADAPTERS

OFFSETS = [7, 30, 90, 180, 365, 730, 1095]   # 1095d ≈ 3 年, 大概 2023
PROBE_TIMEOUT_S = 15
MIN_YEAR = 2023


class _Timeout(Exception):
    pass


def _alarm(_s, _f):
    raise _Timeout('timeout')


def has_data(vendor_id: str, day: dt.date) -> tuple[bool, str]:
    if day.year < MIN_YEAR:
        return False, f'< {MIN_YEAR} 不探'
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(PROBE_TIMEOUT_S)
    try:
        vendor = _load_vendor_config(vendor_id)
        adapter_cls = ADAPTERS.get(vendor_id)
        if vendor is None or adapter_cls is None:
            return False, 'no vendor/adapter'
        adapter = adapter_cls(vendor)
        rows = adapter.fetch_one_day(day)
        if not rows:
            return False, '0 行'
        real = any(
            (r.total_tokens or 0) > 0 or (r.cost_native or 0) > 0 or (r.request_count or 0) > 0
            for r in rows
        )
        return real, f'{len(rows)} 行' + ('' if real else ' (全 0)')
    except _Timeout:
        return False, f'>{PROBE_TIMEOUT_S}s'
    except Exception as e:
        return False, f'{type(e).__name__}: {str(e)[:50]}'
    finally:
        signal.alarm(0)


def probe_vendor(vendor_id: str):
    print(f'\n--- {vendor_id} ---')
    today = dt.date.today()
    earliest_ok = None
    for off in OFFSETS:
        day = today - dt.timedelta(days=off)
        if day.year < MIN_YEAR:
            print(f'  -{off}d ({day})  < {MIN_YEAR}, 停')
            break
        ok, msg = has_data(vendor_id, day)
        flag = '✓' if ok else '✗'
        print(f'  -{off:>4}d ({day}) {flag} {msg}')
        if ok:
            earliest_ok = day
        else:
            # 一旦无数据停 (前提至少近期有过), 不再往老探
            if earliest_ok:
                print(f'  → 在 {day} 断了, 推测最早 ≈ {earliest_ok}')
                break
            # 否则继续 (可能近期账号没用, 再往老看看)
    return earliest_ok


def main():
    targets = ['openai', 'wangsu', 'kimi', 'ucloud', 'apevon',
               'road2all', 'blueshirt']
    results = {}
    for vid in targets:
        try:
            results[vid] = probe_vendor(vid)
        except Exception as e:
            print(f'  {vid} 异常: {e}')
            results[vid] = None

    print('\n' + '=' * 60)
    print('汇总 (探到最早还有数据的日期):')
    for vid in sorted(results, key=lambda x: (results[x] or dt.date(9999, 1, 1))):
        d = results[vid]
        print(f'  {vid:12} ≈ {d if d else "无 / 失败"}')

    print('\n[跳过] tencent / volcengine / xhub (需特殊处理)')


if __name__ == '__main__':
    main()
