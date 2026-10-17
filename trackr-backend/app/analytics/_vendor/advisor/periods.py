"""Explicit calendar windows over materialised returns, with no calendar guessing."""
from calendar import monthrange
from datetime import date
from decimal import Decimal as D
from .core import Metric, ONE, decimal, precision
from .stats import risk

WINDOWS = {'1M': 1, '3M': 3, '6M': 6, '1Y': 12, '3Y': 36, '5Y': 60, 'All': None}


def subtract_months(day, count):
    year, month = divmod(day.year * 12 + day.month - 1 - count, 12)
    month += 1
    return day.replace(year=year, month=month, day=min(day.day, monthrange(year, month)[1]))


def annual_period(start, end):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    return end >= subtract_months(start, -12)


def linked_series(records):
    if not records:
        return []
    value = D('100000')
    result = [{'date': records[0]['start'], 'value': '100000.00'}]
    for record in records:
        value *= ONE + decimal(record['return'])
        result.append({'date': record['end'], 'value': Metric(value, unit='INR').json()['value']})
    return result


@precision
def windows(records, sessions, as_of, *, calendar=None, benchmark=(), rf=(), target=(), portfolio_value=None):
    """Inputs already passed source/basis/adjacency validation in service."""
    out = {}
    by = {(r['start'], r['end']): r for r in records}
    bmap = {(r['start'], r['end']): r for r in benchmark}
    optional = {key: {(r['start'], r['end']): r for r in rows}
                for key, rows in [('rf', rf), ('target', target)]}
    for name, months in WINDOWS.items():
        reason = None
        requested = subtract_months(date.fromisoformat(as_of), months).isoformat() if months else None
        period = {'requested_start': requested, 'as_of': as_of}
        if not records:
            reason = 'MISSING_HISTORY'
        elif months is None:
            start, end = records[0]['start'], records[-1]['end']
        elif not calendar:
            reason = 'MISSING_CALENDAR_COVERAGE'
        elif calendar['start'] > requested or calendar['end'] < as_of:
            reason = 'INSUFFICIENT_CALENDAR_COVERAGE'
        else:
            before = [s for s in sessions if calendar['start'] <= s <= requested]
            ending = [s for s in sessions if calendar['start'] <= s <= as_of]
            if not before or not ending or before[-1] == ending[-1]:
                reason = 'INSUFFICIENT_WINDOW_HISTORY'
            else:
                start, end = before[-1], ending[-1]
        if reason:
            out[name] = Metric.unavailable(reason, unit='window', period=period)
            continue
        period.update(start=start, end=end)
        inside = [s for s in sessions if start <= s <= end]
        expected = list(zip(inside, inside[1:]))
        coverage = {'eligible_count': sum(pair in by for pair in expected), 'total_count': len(expected)}
        if not expected or any(pair not in by for pair in expected):
            out[name] = Metric.unavailable('INCOMPLETE_WINDOW_HISTORY', unit='window', period=period, coverage=coverage)
            continue
        selected = [by[pair] for pair in expected]
        caveats = sorted({reason for r in selected if r.get('quality') == 'provisional' for reason in r.get('reasons', [])})
        if any(r.get('excluded_isins') for r in selected):
            caveats.append('UPSTREAM_EXCLUDED_HOLDINGS')
        if name == 'All' and (not calendar or calendar['end'] < as_of or not sessions or end != sessions[-1]):
            caveats.append('ACTUAL_SUPPLIED_RANGE_NOT_VERIFIED_CURRENT')
        if calendar and calendar.get('quality') == 'provisional':
            caveats.extend(calendar['reasons'])
        refs = sorted({ref for r in selected for ref in r['source_refs']} | set((calendar or {}).get('source_refs', [])))
        matched_optional = {}
        for key, mapping in optional.items():
            matched_optional[key] = [mapping[p]['return'] for p in expected] if all(p in mapping for p in expected) else None
        paired = [p for p in expected if p in bmap]
        metrics = risk([r['return'] for r in selected], elapsed_days=(date.fromisoformat(end)-date.fromisoformat(start)).days,
                       annualise_allowed=annual_period(start, end), rf=matched_optional['rf'],
                       target=matched_optional['target'], portfolio_value=portfolio_value)
        comparison = risk([by[p]['return'] for p in paired], benchmark=[bmap[p]['return'] for p in paired])
        for key in ('beta', 'tracking_error', 'information_ratio'):
            metrics[key] = comparison[key]
            metrics[key].coverage = {'eligible_count': len(paired), 'total_count': len(expected), 'intervals': paired}
            if len(paired) != len(expected):
                metrics[key].caveat('PARTIAL_BENCHMARK_COVERAGE')
        for key, metric in metrics.items():
            used = [bmap[p] for p in paired] if key in ('beta', 'tracking_error', 'information_ratio') else []
            optional_key = {'sharpe': 'rf', 'sortino': 'target'}.get(key)
            if optional_key and matched_optional[optional_key] is not None:
                used += [optional[optional_key][p] for p in expected]
            metric.source_refs = sorted(set(refs + [ref for r in used for ref in r['source_refs']]))
            metric.period = dict(period)
            for c in caveats + [c for r in used if r.get('quality') == 'provisional' for c in r.get('reasons', [])]:
                metric.caveat(c)
        series = Metric(linked_series(selected), unit='INR_series', method='linked_returns_v1', period=period, source_refs=refs)
        for c in caveats:
            series.caveat(c)
        if len(paired) == len(expected):
            matching = [bmap[p] for p in expected]
            benchmark_series = Metric(linked_series(matching), unit='INR_series', period=period,
                                      source_refs=sorted({ref for r in matching for ref in r['source_refs']}))
            for c in caveats + [c for r in matching if r.get('quality') == 'provisional' for c in r.get('reasons', [])]:
                benchmark_series.caveat(c)
        else:
            benchmark_series = Metric.unavailable('MISSING_MATCHED_BENCHMARK_HISTORY', unit='INR_series', period=period)
        out[name] = Metric({'risk': metrics, 'growth_series': series, 'benchmark_growth': benchmark_series,
                            'basis': selected[0]['basis'], 'scope': selected[0]['scope']}, unit='window',
                           period=period, coverage=coverage, source_refs=refs)
        for c in caveats:
            out[name].caveat(c)
    return out
