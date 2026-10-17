"""Illustrative buy-and-hold only; consumes adjusted levels, never adjusts them."""
from decimal import Decimal as D
from .core import Metric, ONE, ZERO, decimal, precision


@precision
def buy_and_hold(request, weights, histories, sessions, as_of, weight_refs=(), weight_caveats=()):
    if not request:
        return Metric.unavailable('HYPOTHETICAL_NOT_REQUESTED', unit='illustration')
    try:
        start, end, basis = request['start'], request['end'], request['basis']
        if start not in sessions or end not in sessions or start >= end or end > as_of:
            raise ValueError('INVALID_HYPOTHETICAL_PERIOD')
        if basis not in ('split_adjusted_price', 'total_return'):
            raise ValueError('UNSUPPORTED_ADJUSTMENT_BASIS')
        if not weights or any(w < ZERO for w in weights.values()) or abs(sum(weights.values(), ZERO)-ONE) > D('1e-30'):
            raise ValueError('INCOMPLETE_INITIAL_WEIGHTS')
        active = {i: w for i, w in weights.items() if w > ZERO}
        dates = [s for s in sessions if start <= s <= end]
        refs = set(weight_refs)
        reasons = set(weight_caveats) | {'CURRENT_CONSTITUENT_SELECTION_BIAS', 'HYPOTHETICAL_NOT_INVESTOR_PERFORMANCE'}
        for isin in active:
            h = histories.get(isin)
            if not h:
                raise ValueError('MISSING_REQUIRED_ASSET_HISTORY:'+isin)
            if h['basis'] != basis:
                raise ValueError('HYPOTHETICAL_BASIS_MISMATCH:'+isin)
            if any(s not in h['levels'] for s in dates):
                raise ValueError('INCOMPLETE_REQUIRED_ASSET_HISTORY:'+isin)
            if any(decimal(h['levels'][s]) <= ZERO for s in dates):
                raise ValueError('NONPOSITIVE_LEVEL')
            refs.update(h['source_refs'])
            reasons.update(h.get('reasons', []))
        levels = [D('100000') * sum((w*histories[i]['levels'][s]/histories[i]['levels'][start]
                                   for i, w in active.items()), ZERO) for s in dates]
        returns = [{'start': a, 'end': b, 'return': y/x-ONE}
                   for a, b, x, y in zip(dates, dates[1:], levels, levels[1:])]
        return Metric({'growth_series': [{'date': s, 'value': Metric(v, unit='INR').json()['value']} for s, v in zip(dates, levels)],
                       'returns': returns, 'initial_weights': active, 'weights_as_of': as_of, 'basis': basis},
                      status='provisional', reasons=sorted(reasons), unit='illustration',
                      method='initial_weight_buy_and_hold_v1', period={'start': start, 'end': end},
                      coverage={'eligible_count': len(active), 'total_count': len(active), 'session_count': len(dates)},
                      source_refs=sorted(refs))
    except (KeyError, ValueError, TypeError) as exc:
        return Metric.unavailable(str(exc), unit='illustration', method='initial_weight_buy_and_hold_v1')
