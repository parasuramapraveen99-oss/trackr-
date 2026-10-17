"""Reported benchmark fundamentals with a strict comparability gate."""
from datetime import date
from .core import Metric, decimal


def fundamentals(records, data, portfolio, basis_by_field, validate, timestamp):
    fields = ('pe', 'pb', 'dividend_yield')
    reported = {f: Metric.unavailable('MISSING_BENCHMARK_FUNDAMENTAL', unit='fraction' if f == 'dividend_yield' else 'ratio') for f in fields}
    comparison = {f: Metric.unavailable('MISSING_COMPARABLE_BENCHMARK_FUNDAMENTALS', unit='fraction' if f == 'dividend_yield' else 'ratio') for f in fields}
    seen = set()
    errors = []
    definitions = {}
    for record in records:
        field = record.get('field')
        if field not in fields:
            errors.append('UNKNOWN_BENCHMARK_FUNDAMENTAL:'+str(field))
            continue
        unit = 'fraction' if field == 'dividend_yield' else 'ratio'
        try:
            if field in seen:
                raise ValueError('CONFLICTING_BENCHMARK_FUNDAMENTALS')
            seen.add(field)
            validate(record)
            if not data.get('benchmark_id') or record.get('index_id') != data['benchmark_id']:
                raise ValueError('BENCHMARK_ID_MISMATCH')
            if record['unit'] != unit:
                raise ValueError('INVALID_BENCHMARK_UNIT')
            vdate = date.fromisoformat(record['valuation_date'])
            as_of = date.fromisoformat(data['as_of'])
            published = timestamp(record['published_at'])
            timestamp(record['retrieved_at'])
            if vdate > as_of or published.date() > as_of or published > timestamp(data['knowledge_cutoff']):
                raise ValueError('LOOKAHEAD_INPUT')
            if record['basis'] not in ('TTM', 'annual') or record['consolidation'] not in ('consolidated', 'standalone', 'mixed'):
                raise ValueError('INVALID_BENCHMARK_FUNDAMENTAL_BASIS')
            if not record.get('aggregation') or not record.get('loss_policy'):
                raise ValueError('MISSING_BENCHMARK_METHOD')
            metric = Metric(decimal(record['value']), unit=unit, method=record['aggregation'],
                            source_refs=record['source_refs'].copy(), period={'as_of':record['valuation_date']})
            if record.get('quality') == 'provisional':
                for reason in record['reasons']:
                    metric.caveat(reason)
            if vdate < as_of:
                metric.caveat('STALE_BENCHMARK_OBSERVATION')
            reported[field] = metric
            definitions[field] = {key:record[key] for key in ('index_id','basis','consolidation','aggregation','loss_policy')}
            p = portfolio[field]
            reason = None
            expected_policy = 'include_all' if field == 'dividend_yield' else 'exclude_nonpositive'
            if p.value is None:
                reason = 'PORTFOLIO_FUNDAMENTAL_UNAVAILABLE'
            elif p.coverage.get('weight_fraction') != decimal('1'):
                reason = 'PARTIAL_PORTFOLIO_FUNDAMENTAL_COVERAGE'
            elif vdate != as_of:
                reason = 'BENCHMARK_DATE_MISMATCH'
            elif basis_by_field.get(field) != (record['basis'], record['consolidation']):
                reason = 'BENCHMARK_FUNDAMENTAL_BASIS_MISMATCH'
            elif p.method != record['aggregation'] or record['loss_policy'] != expected_policy:
                reason = 'BENCHMARK_AGGREGATION_MISMATCH'
            if reason:
                comparison[field] = Metric.unavailable(reason, unit=unit)
            else:
                comparison[field] = Metric(p.value-metric.value, unit=unit, method='comparable_fundamental_spread_v1',
                                           period={'as_of':data['as_of']}, source_refs=sorted(set(p.source_refs+metric.source_refs)))
                for reason in p.reasons+metric.reasons:
                    comparison[field].caveat(reason)
        except (ValueError, KeyError, TypeError) as exc:
            reported[field] = Metric.unavailable(str(exc), unit=unit)
            comparison[field] = Metric.unavailable(str(exc), unit=unit)
            definitions.pop(field, None)
    return reported, comparison, definitions, errors
