"""Portfolio-only health rubric v1 and capital-state description.

Trackr-authored, 21 October 2026, from the supplied ``PORTFOLIO_HEALTH.md``
(portfolio-health-v1). Version 2 (owner decisions, 23 October 2026): ROCE replaces ROE
for non-financial companies, NBFCs are scored on ROE and net margin, and a part is scored
when at least 80% of portfolio value has its inputs (``MIN_COVERAGE``); the uncovered
holdings are named in the part's evidence. docs/PORTFOLIO_HEALTH.md. The supplied 0.4.0 package imports this module but did not
include it. Every weight, anchor, band, sample gate and freshness rule below is
copied from that specification; nothing is tuned here. Acceptance is the package's
own hand reference (43773/754, displayed 58, grade C) and its health tests.

Rules carried from the specification:
  * No missing component is replaced with 50. A component below MIN_COVERAGE withholds
    the overall score and grade and exposes a bounded interval.
  * Every calculated overall score is provisional: the rubric is uncalibrated.
  * Display score rounds HALF_UP to an integer and the grade uses that same integer.
  * No input is inferred: profiles, sectors, histories and fundamentals are supplied.
"""
from datetime import date
from decimal import Decimal as D, ROUND_HALF_UP
from .core import Metric, ZERO, ONE, precision, decimal
from .stats import correlation, interval_returns, mean

RULE_VERSION = 'portfolio-health-v2'

#: Share of portfolio value a part needs inputs for before it is scored.
MIN_COVERAGE = D('0.80')
SCORE_UNIT = 'score_0_100'
HUNDRED = D(100)

#: Component weights; order is the published component order and the summation order.
WEIGHTS = {
    'concentration': D('0.25'),
    'diversification': D('0.15'),
    'risk': D('0.30'),
    'quality': D('0.20'),
    'valuation': D('0.10'),
}
LABELS = {
    'concentration': 'Concentration',
    'diversification': 'Diversification',
    'risk': 'Observed risk',
    'quality': 'Business quality',
    'valuation': 'Valuation',
}

#: Lower bound per grade, descending. Applied to the HALF_UP display score.
BANDS = (('A', 80), ('B', 65), ('C', 50), ('D', 35), ('E', 20), ('F', 0))

#: (input, score) anchors, ascending input. Linear between anchors, clamped at the ends.
ANCHORS = {
    'effective_holdings': ((1, 0), (5, 40), (10, 70), (20, 100)),
    'largest_holding': (('.10', 100), ('.20', 70), ('.35', 30), ('.50', 0)),
    'effective_sectors': ((1, 0), (3, 50), (5, 80), (8, 100)),
    'correlation': (('-.20', 100), (0, 90), ('.30', 70), ('.60', 35), ('.85', 0)),
    'volatility': (('.10', 100), ('.20', 80), ('.30', 50), ('.45', 15), ('.60', 0)),
    'drawdown': (('.05', 100), ('.10', 85), ('.20', 60), ('.35', 25), ('.50', 0)),
    'var95': (('.005', 100), ('.010', 85), ('.020', 60), ('.035', 25), ('.050', 0)),
    'roe': (('-.10', 0), (0, 20), ('.08', 50), ('.15', 80), ('.25', 100)),
    'roce': (('-.10', 0), (0, 20), ('.10', 50), ('.18', 80), ('.30', 100)),
    'net_margin': (('-.10', 0), (0, 20), ('.05', 50), ('.12', 80), ('.25', 100)),
    'debt_to_equity': ((0, 100), ('.5', 90), (1, 70), (2, 35), (3, 0)),
    'interest_cover': ((0, 0), (1, 20), (2, 50), (4, 80), (8, 100)),
    'capital_adequacy_ratio': (('.09', 0), ('.12', 40), ('.15', 70), ('.18', 90), ('.22', 100)),
    'gross_npa_ratio': ((0, 100), ('.01', 95), ('.03', 75), ('.06', 40), ('.10', 0)),
}
ANCHORS = {k: tuple((D(str(x)), D(str(s))) for x, s in v) for k, v in ANCHORS.items()}

#: Business-quality fields per supported profile. Other financials are unsupported in v1.
PROFILE_FIELDS = {
    'non_financial': ('roce', 'net_margin', 'debt_to_equity', 'interest_cover'),
    'bank': ('roe', 'capital_adequacy_ratio', 'gross_npa_ratio'),
    'nbfc': ('roe', 'net_margin'),
}
WINDOW_INTERVALS = 252
TREND_SESSIONS = 50

LIMITATIONS = [
    'EXPERIMENTAL_UNCALIBRATED_RUBRIC',
    'DESCRIPTIVE_NOT_A_RECOMMENDATION_OR_FORECAST',
    'NO_INVESTOR_GOAL_OR_SUITABILITY_ASSESSMENT',
    'RISK_COMPOSITE_DID_NOT_BEAT_RAW_VOLATILITY_BASELINE_IN_US_STUDY',
    'COMPONENTS_OVERLAP_AND_CAN_COMPENSATE',
    'RELATIVE_VALUATION_DEPENDS_ON_PEER_UNIVERSE',
]


@precision
def scaled(value, key):
    """Piecewise-linear anchor score in [0, 100]."""
    x = decimal(value) if not isinstance(value, D) else value
    if not x.is_finite():
        raise ValueError('NONFINITE_INPUT')
    anchors = ANCHORS[key]
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, s0), (x1, s1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            return s0 + (x - x0) * (s1 - s0) / (x1 - x0)
    raise ValueError('ANCHOR_SEGMENT_NOT_FOUND')


def _component(score, evidence, *, method, refs=(), reasons=(), period=None, coverage=None):
    metric = Metric(score, unit=SCORE_UNIT, method=method, source_refs=sorted(set(refs)),
                    period=dict(period or {}), coverage=dict(coverage or {}))
    for reason in reasons:
        metric.caveat(reason)
    return {'score': metric, 'evidence': evidence}


def _missing(reason, evidence=None, *, method, refs=(), period=None, extra=()):
    metric = Metric.unavailable(reason, unit=SCORE_UNIT, method=method,
                                source_refs=sorted(set(refs)), period=dict(period or {}))
    for item in extra:
        if item not in metric.reasons:
            metric.reasons.append(item)
    return {'score': metric, 'evidence': evidence or {}}


def _positive(weights):
    return {i: w for i, w in weights.items() if w > ZERO}


@precision
def concentration(weights, sector_allocation):
    """Mean of effective-holdings, largest-holding and effective-sector scores."""
    method = 'health_concentration_v1'
    if not weights:
        return _missing('UNKNOWN_VALUE_DENOMINATOR', method=method)
    if sector_allocation.value is None:
        return _missing('MISSING_SECTOR_ALLOCATION', method=method, extra=sector_allocation.reasons)
    buckets = dict(sector_allocation.value)
    unknown = buckets.pop('UNKNOWN', {'weight': ZERO})['weight']
    if ONE - unknown < MIN_COVERAGE or not buckets:
        # Too much value unclassified: the effective sector count is not knowable.
        return _missing('MISSING_SECTOR_CLASSIFICATION', {'unknown_weight': unknown},
                        method=method, refs=sector_allocation.source_refs)
    known = ONE - unknown
    hhi = sum((w * w for w in weights.values()), ZERO)
    # Sector concentration over the classified value only.
    sector_hhi = sum(((b['weight'] / known) ** 2 for b in buckets.values()), ZERO)
    if hhi <= ZERO or sector_hhi <= ZERO:
        return _missing('UNDEFINED_DENOMINATOR', method=method)
    effective = ONE / hhi
    largest = max(weights.values())
    sectors = ONE / sector_hhi
    subscores = {'effective_holdings': scaled(effective, 'effective_holdings'),
                 'largest_holding': scaled(largest, 'largest_holding'),
                 'effective_sectors': scaled(sectors, 'effective_sectors')}
    score = (subscores['effective_holdings'] + subscores['largest_holding']
             + subscores['effective_sectors']) / D(3)
    evidence = {'effective_holdings': effective, 'largest_holding_weight': largest,
                'largest_holding_isin': max(sorted(weights), key=lambda i: weights[i]),
                'effective_sectors': sectors, 'sector_count': len(buckets),
                'sector_coverage': known, 'subscores': subscores}
    return _component(score, evidence, method=method, refs=sector_allocation.source_refs,
                      reasons=sector_allocation.reasons, period=sector_allocation.period)


@precision
def diversification(weights, histories, sessions, period):
    """Weight-pair average correlation over the exact 252-interval health window."""
    method = 'health_weighted_pair_correlation_v1'
    if not weights:
        return _missing('UNKNOWN_VALUE_DENOMINATOR', method=method)
    if not period:
        return _missing('MISSING_COMPLETE_252_INTERVAL_WINDOW', method=method)
    dates = [s for s in sessions if period['start'] <= s <= period['end']]
    if len(dates) != WINDOW_INTERVALS + 1:
        return _missing('INCOMPLETE_252_INTERVAL_WINDOW', method=method, period=period)
    active = _positive(weights)
    ids = sorted(active)
    if len(ids) == 1:
        evidence = {'weighted_pair_correlation': None, 'pair_count': 0,
                    'structural': 'SINGLE_HOLDING_CORRELATION_UNDEFINED'}
        return _component(ZERO, evidence, method=method, period=period,
                          reasons=['SINGLE_HOLDING_STRUCTURAL_SCORE'])
    complete = [i for i in ids if histories.get(i)
                and all(d in histories[i]['levels'] for d in dates)]
    coverage = sum((active[i] for i in complete), ZERO) / sum(active.values(), ZERO)
    if coverage < MIN_COVERAGE or len(complete) < 2:
        missing = sorted(set(ids) - set(complete))
        return _missing('INCOMPLETE_ASSET_HISTORY:' + ','.join(missing), method=method, period=period)
    ids = complete
    returns, bases, refs, reasons = {}, set(), [], []
    for isin in ids:
        history = histories[isin]
        bases.add(history['basis'])
        refs += history['source_refs']
        reasons += history.get('reasons', [])
        window = interval_returns({d: history['levels'][d] for d in dates}, dates)
        returns[isin] = [r['return'] for r in window]
    if len(bases) != 1:
        return _missing('HISTORY_BASIS_MISMATCH', method=method, refs=refs, period=period)
    numerator = denominator = ZERO
    pairs = []
    for index, a in enumerate(ids):
        for b in ids[index + 1:]:
            rho = correlation(returns[a], returns[b])
            if rho.value is None:
                return _missing('PAIR_CORRELATION_UNAVAILABLE:' + a + ':' + b, method=method,
                                refs=refs, period=period, extra=rho.reasons)
            pair_weight = active[a] * active[b]
            numerator += pair_weight * rho.value
            denominator += pair_weight
            pairs.append({'isin_a': a, 'isin_b': b, 'correlation': rho.value})
    average = numerator / denominator
    evidence = {'weighted_pair_correlation': average, 'pair_count': len(pairs),
                'observations': WINDOW_INTERVALS, 'basis': bases.pop(), 'pairs': pairs,
                'coverage': coverage}
    return _component(scaled(average, 'correlation'), evidence, method=method, refs=refs,
                      reasons=reasons, period=period,
                      coverage={'eligible_count': WINDOW_INTERVALS, 'total_count': WINDOW_INTERVALS})


@precision
def observed_risk(window):
    """Equal mean of annual volatility, drawdown magnitude and 1-day 95% VaR scores."""
    method = 'health_observed_risk_v1'
    if window.value is None:
        return _missing('MISSING_COMPLETE_252_INTERVAL_WINDOW', method=method,
                        period=window.period, extra=window.reasons)
    # v2: a complete window only links intervals with identical exclusions, so an
    # excluded holding is excluded for the whole year; the score is the reconciled book's
    # risk, caveated and named upstream, rather than withheld.
    excluded_scope = 'UPSTREAM_EXCLUDED_HOLDINGS' in window.reasons
    if window.coverage.get('eligible_count') != WINDOW_INTERVALS:
        return _missing('INCOMPLETE_252_INTERVAL_WINDOW', method=method, period=window.period)
    metrics = window.value['risk']
    needed = {'volatility': 'volatility', 'max_drawdown': 'drawdown', 'var_1d_95': 'var95'}
    for key in needed:
        if metrics[key].value is None:
            return _missing('MISSING_RISK_INPUT:' + key, method=method, period=window.period,
                            extra=metrics[key].reasons)
    inputs = {'volatility': metrics['volatility'].value,
              'max_drawdown_magnitude': abs(metrics['max_drawdown'].value),
              'var_1d_95': metrics['var_1d_95'].value}
    subscores = {'volatility': scaled(inputs['volatility'], 'volatility'),
                 'drawdown': scaled(inputs['max_drawdown_magnitude'], 'drawdown'),
                 'var95': scaled(inputs['var_1d_95'], 'var95')}
    score = (subscores['volatility'] + subscores['drawdown'] + subscores['var95']) / D(3)
    evidence = {**inputs, 'observations': WINDOW_INTERVALS, 'subscores': subscores,
                'basis': window.value.get('basis')}
    reasons = list(window.reasons) + (['EXCLUDES_UNRECONCILED_SEGMENTS'] if excluded_scope else [])
    return _component(score, evidence, method=method, refs=window.source_refs,
                      reasons=reasons, period=window.period, coverage=window.coverage)


@precision
def business_quality(weights, fundamentals, profiles, *, errors=(), refs=(), reasons=()):
    """Market-value-weighted mean of per-holding profile scores; every holding required."""
    method = 'health_business_quality_v1'
    errors = sorted(set(errors))
    if errors:
        return _missing('CONFLICTING_STALE_OR_INVALID_QUALITY_INPUT', {'input_errors': errors},
                        method=method, refs=refs)
    if not weights:
        return _missing('UNKNOWN_VALUE_DENOMINATOR', method=method)
    active = _positive(weights)
    missing, per_holding = [], {}
    for isin in sorted(active):
        profile = profiles.get(isin)
        if profile not in PROFILE_FIELDS:
            missing.append('MISSING_HEALTH_PROFILE:' + isin)
            continue
        values = fundamentals.get(isin, {})
        absent = [f for f in PROFILE_FIELDS[profile] if values.get(f) is None]
        if absent:
            missing += ['MISSING_QUALITY_INPUT:' + isin + ':' + f for f in absent]
            continue
        if profile == 'bank' and (values['capital_adequacy_ratio'] < ZERO
                                  or not ZERO <= values['gross_npa_ratio'] <= ONE):
            missing.append('INVALID_BANK_RATIO:' + isin)
            continue
        subscores = {}
        for field in PROFILE_FIELDS[profile]:
            value = values[field]
            # Negative D/E may reflect negative equity; it is not low leverage.
            subscores[field] = ZERO if field == 'debt_to_equity' and value < ZERO else scaled(value, field)
        per_holding[isin] = {'profile': profile, 'subscores': subscores,
                             'score': sum(subscores.values(), ZERO) / D(len(subscores))}
    covered = sum((active[i] for i in per_holding), ZERO)
    fraction = covered / sum(active.values(), ZERO)
    if fraction < MIN_COVERAGE:
        return _missing('INCOMPLETE_QUALITY_COVERAGE',
                        {'input_errors': missing, 'holdings': per_holding, 'coverage': fraction},
                        method=method, refs=refs)
    score = sum((active[i] * per_holding[i]['score'] for i in sorted(per_holding)), ZERO) / covered
    return _component(score, {'holdings': per_holding, 'input_errors': missing, 'coverage': fraction},
                      method=method, refs=refs, reasons=reasons,
                      coverage={'eligible_count': len(per_holding), 'total_count': len(active),
                                'weight_fraction': fraction})


@precision
def valuation(value_factor):
    """Existing value-factor percentile composite with complete holding coverage."""
    method = 'health_relative_valuation_v1'
    portfolio = value_factor['portfolio']
    if portfolio.value is None:
        return _missing('VALUE_FACTOR_UNAVAILABLE', method=method, refs=portfolio.source_refs,
                        extra=portfolio.reasons)
    fraction = portfolio.coverage.get('weight_fraction')
    if fraction is None or fraction < MIN_COVERAGE:
        return _missing('PARTIAL_VALUATION_COVERAGE', {'weight_fraction': fraction}, method=method,
                        refs=portfolio.source_refs)
    if not value_factor.get('universe') or value_factor.get('eligible_count', 0) < 30:
        return _missing('INSUFFICIENT_ELIGIBLE_PEERS', method=method, refs=portfolio.source_refs)
    evidence = {'universe': value_factor['universe'], 'eligible_count': value_factor['eligible_count'],
                'total_count': value_factor.get('total_count'), 'weight_fraction': fraction,
                'coverage': fraction}
    score = max(ZERO, min(HUNDRED, portfolio.value))
    return _component(score, evidence, method=method, refs=portfolio.source_refs,
                      reasons=portfolio.reasons, period=portfolio.period, coverage=portfolio.coverage)


def grade_for(display_score):
    return next(grade for grade, floor in BANDS if display_score >= floor)


@precision
def assemble(components, *, as_of, refs=(), reasons=()):
    """Overall score only when every component is available; otherwise a bounded interval."""
    if list(components) != list(WEIGHTS):
        raise ValueError('HEALTH_COMPONENT_SET_MISMATCH')
    rows, known, missing_weight = [], ZERO, ZERO
    all_refs, caveats = set(refs), list(reasons)
    for key, component in components.items():
        metric = component['score']
        weight = WEIGHTS[key]
        contribution = None
        if metric.value is not None:
            if not ZERO <= metric.value <= HUNDRED:
                raise ValueError('COMPONENT_SCORE_OUT_OF_RANGE:' + key)
            contribution = weight * metric.value
            known += contribution
            caveats += metric.reasons
        else:
            missing_weight += weight
        all_refs.update(metric.source_refs)
        rows.append({'key': key, 'label': LABELS[key], 'weight': weight, 'score': metric,
                     'contribution': contribution, 'evidence': component['evidence']})
    available = [r for r in rows if r['score'].value is not None]
    bounds = {'lower': known, 'upper': known + missing_weight * HUNDRED,
              'kind': 'missing_information_bound_not_confidence_interval'}
    if missing_weight:
        missing = [r['key'] for r in rows if r['score'].value is None]
        score = Metric.unavailable('MISSING_HEALTH_COMPONENT', unit=SCORE_UNIT, method=RULE_VERSION,
                                   source_refs=sorted(all_refs), period={'as_of': as_of})
        score.reasons += ['MISSING_COMPONENT:' + key for key in missing]
        display, grade = None, None
    else:
        score = Metric(known, unit=SCORE_UNIT, method=RULE_VERSION, source_refs=sorted(all_refs),
                       period={'as_of': as_of})
        score.caveat('RUBRIC_NOT_CALIBRATED')
        for reason in caveats:
            score.caveat(reason)
        display = int(known.quantize(ONE, rounding=ROUND_HALF_UP))
        grade = grade_for(display)
    weakest = sorted(available, key=lambda r: (r['score'].value, r['key']))
    return {'score': score, 'display_score': display, 'grade': grade, 'components': rows,
            'weights': dict(WEIGHTS), 'bands': [list(b) for b in BANDS], 'rule_version': RULE_VERSION,
            'data_status': {'complete': not missing_weight, 'available_components': len(available),
                            'total_components': len(rows), 'calibration': 'uncalibrated'},
            'score_bounds': bounds, 'drivers': [r['key'] for r in weakest[:2]],
            'limitations': list(LIMITATIONS)}


@precision
def capital_states(rows, histories, sessions, as_of):
    """Unrealised sign x latest split-adjusted level versus its 50-session mean.

    Descriptive only; never a forecast, signal or health input. Unknowns stay visible.
    """
    method = 'capital_state_50_session_v1'
    # v2: the trend ends at the latest closed session within a week of as_of, so the
    # buckets do not go blank between midnight and the evening bhavcopy.
    trend = [s for s in sessions if s <= as_of][-TREND_SESSIONS:]
    trend_ok = (len(trend) == TREND_SESSIONS and
                (date.fromisoformat(as_of) - date.fromisoformat(trend[-1])).days <= 7)
    per_isin, buckets, refs = {}, {}, set()
    total, complete = ZERO, True
    for row in sorted(rows, key=lambda r: r['isin']):
        isin = row['isin']
        value = None if row.get('market_value') is None else decimal(row['market_value'])
        cost = None if row.get('invested') is None else decimal(row['invested'])
        state, reason, evidence = None, None, {}
        history = histories.get(isin)
        if value is None:
            complete, reason = False, 'MISSING_MARKET_VALUE'
        elif cost is None:
            reason = 'MISSING_COST'
        elif not trend_ok:
            reason = 'INSUFFICIENT_SESSION_CALENDAR'
        elif not history:
            reason = 'MISSING_PRICE_HISTORY'
        elif history['basis'] != 'split_adjusted_price':
            reason = 'TREND_REQUIRES_SPLIT_ADJUSTED_PRICE'
        elif any(d not in history['levels'] for d in trend):
            reason = 'INCOMPLETE_50_SESSION_HISTORY'
        if reason:
            state = 'UNKNOWN'
        else:
            levels = [history['levels'][d] for d in trend]
            average = mean(levels)
            latest = levels[-1]
            gain = value - cost
            refs.update(history['source_refs'])
            evidence = {'unrealised': gain, 'latest_level': latest, 'moving_average_50': average}
            if gain == ZERO or latest == average:
                state = 'NEUTRAL'
            elif gain > ZERO:
                state = 'STRONG' if latest > average else 'WEAKENING'
            else:
                state = 'RECOVERY' if latest > average else 'CAUTION'
        per_isin[isin] = {'state': state, 'reason': reason, 'market_value': value, 'evidence': evidence}
        if value is not None:
            total += value
            buckets[state] = buckets.get(state, ZERO) + value
        refs.update(row.get('source_refs', []))
    ordered = {s: buckets.get(s, ZERO) for s in ('STRONG', 'WEAKENING', 'CAUTION', 'RECOVERY', 'NEUTRAL', 'UNKNOWN')}
    if not rows:
        return Metric.unavailable('EMPTY_PORTFOLIO', unit='buckets', method=method)
    if complete and total <= ZERO:
        return Metric.unavailable('ZERO_PORTFOLIO_VALUE', unit='buckets', method=method)
    value = {'buckets': ordered, 'states': per_isin,
             'total': total if complete else None,
             'bucket_weights': {k: v / total for k, v in ordered.items()} if complete else None,
             'trend_window': {'start': trend[0], 'end': trend[-1]} if trend_ok else None}
    out = Metric(value, unit='buckets', method=method, source_refs=sorted(refs), period={'as_of': as_of})
    out.caveat('DESCRIPTIVE_NOT_A_SIGNAL')
    if trend_ok and trend[-1] != as_of:
        out.caveat('PREVIOUS_SESSION_CLOSE')
    if not complete:
        out.caveat('UNKNOWN_VALUE_COVERAGE')
    if ordered['UNKNOWN']:
        out.caveat('UNKNOWN_CAPITAL_STATE')
    return out
