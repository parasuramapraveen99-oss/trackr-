"""Offline analytics orchestration. No network, DB, accounting or UI dependencies."""
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal as D
from .core import decimal, encode, Metric, ZERO, ONE, precision
from .cross_section import overview, allocation, aggregate, factor, ranks, lenses
from .stats import risk, correlation, variance, interval_returns
from .periods import windows, subtract_months
from .hypothetical import buy_and_hold
from .upstream import kpis
from .benchmarks import fundamentals as benchmark_fundamentals
from .health import concentration, diversification as health_diversification, observed_risk, business_quality, valuation, assemble, capital_states

METHOD_VERSION='trackr-analytics-0.4'


def timestamp(value):
    dt=datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return dt


def validate_sources(data):
    cutoff=timestamp(data['knowledge_cutoff'])
    as_of=date.fromisoformat(data['as_of'])
    if cutoff.date()<as_of:
        raise ValueError('CUTOFF_BEFORE_AS_OF')
    timestamp(data['generated_at'])
    sources={}
    for source in data['sources']:
        for key in ('id','provider','origin','url','dataset','observed_at','retrieved_at',
                    'source_version','licence_note','usage_status'):
            if not source.get(key):
                raise ValueError('MISSING_SOURCE_'+key.upper())
        if source['id'] in sources:
            raise ValueError('CONFLICTING_SOURCE_ID')
        for key in ('observed_at','published_at'):
            if source.get(key) and timestamp(source[key])>cutoff:
                raise ValueError('LOOKAHEAD_INPUT')
        timestamp(source['retrieved_at'])
        if source['usage_status'] not in ('synthetic','local_research','authorised'):
            raise ValueError('UNRESOLVED_USAGE_RIGHTS')
        sources[source['id']]=source
    if not sources:
        raise ValueError('MISSING_SOURCE')
    return sources


def record_quality(record,sources):
    if record.get('quality','available') not in ('available','provisional'):
        raise ValueError('CONFLICTING_OR_UNAVAILABLE_INPUT')
    refs=record.get('source_refs',[])
    if not isinstance(refs,list) or any(not isinstance(ref,str) for ref in refs):
        raise ValueError('INVALID_SOURCE_REFERENCES')
    if not refs or any(ref not in sources for ref in refs):
        raise ValueError('MISSING_SOURCE_REFERENCE')
    if record.get('quality')=='provisional' and not record.get('reasons'):
        raise ValueError('MISSING_PROVISIONAL_REASON')
    if not isinstance(record.get('reasons',[]),list) or any(not isinstance(r,str) or not r for r in record.get('reasons',[])):
        raise ValueError('INVALID_QUALITY_REASONS')


def validated_intervals(records,data,sources):
    sessions=data['sessions']
    adjacency=set(zip(sessions,sessions[1:]))
    seen=set()
    result=[]
    for record in records:
        record_quality(record,sources)
        pair=(record['start'],record['end'])
        if pair in seen:
            raise ValueError('DUPLICATE_INTERVAL')
        seen.add(pair)
        if pair not in adjacency:
            raise ValueError('NON_ADJACENT_SESSION')
        if pair[1]>data['as_of']:
            raise ValueError('LOOKAHEAD_INPUT')
        value=decimal(record['return'])
        if value < -ONE:
            raise ValueError('RETURN_BELOW_MINUS_ONE')
        result.append({**record,'return':value})
    if result != sorted(result,key=lambda r:(r['start'],r['end'])):
        raise ValueError('UNSORTED_INTERVALS')
    if len({(r.get('basis'),r.get('scope')) for r in result})>1:
        raise ValueError('BASIS_MISMATCH')
    return result


def failed(reason):
    return {'schema_version':'0.1','method_version':METHOD_VERSION,'status':'unavailable',
            'reasons':[reason],'sections':{},'compatibility':{'compatible':False,'reasons':[reason]}}


def decorate(value,refs,reasons,period=None):
    if isinstance(value,Metric):
        value.source_refs=sorted(set(refs))
        value.period=period or {}
        for reason in reasons:
            value.caveat(reason)
    elif isinstance(value,dict):
        for item in value.values():
            decorate(item,refs,reasons,period)
    elif isinstance(value,list):
        for item in value:
            decorate(item,refs,reasons,period)


@precision
def analyse(data):
    """Fail explicitly on invalid essential facts; optional datasets degrade by section."""
    try:
        return _analyse(data)
    except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
        reason=str(exc) if isinstance(exc,ValueError) else 'INVALID_OR_MISSING_INPUT:'+type(exc).__name__
        return failed(reason)


def _analyse(data):
    if not isinstance(data,dict):
        raise ValueError('INPUT_OBJECT_REQUIRED')
    for key in ('sources','holdings','returns','fundamentals','classifications','risk_free_returns',
                'target_returns','benchmark_returns','asset_histories','upstream_kpis','benchmark_fundamentals','health_profiles'):
        records=data.get(key,[])
        if not isinstance(records,list) or any(not isinstance(r,dict) for r in records):
            raise ValueError('INVALID_RECORD_ARRAY:'+key)
    for key in ('calendar_coverage','hypothetical','universe'):
        if key in data and not isinstance(data[key],dict):
            raise ValueError('INPUT_OBJECT_REQUIRED:'+key)
    if not isinstance(data.get('sessions'),list) or any(not isinstance(s,str) for s in data['sessions']):
        raise ValueError('INVALID_CALENDAR')
    for entry in data.get('asset_histories',[]):
        if not isinstance(entry.get('levels'),list) or any(not isinstance(p,dict) for p in entry['levels']):
            raise ValueError('INVALID_HISTORY_LEVELS')
    if data['schema_version']!='0.1':
        raise ValueError('UNSUPPORTED_SCHEMA')
    if data['currency']!='INR' or data['portfolio_scope']!='long_only_indian_equities':
        raise ValueError('UNSUPPORTED_SCOPE')
    for k in ('engine_version','ledger_revision','calendar_id'):
        if not data.get(k):
            raise ValueError('MISSING_'+k.upper())
    sources=validate_sources(data)
    sessions=data['sessions']
    if sessions!=sorted(set(sessions)):
        raise ValueError('INVALID_CALENDAR')
    for session in sessions:
        date.fromisoformat(session)
        if session>data['as_of']:
            raise ValueError('LOOKAHEAD_INPUT')
    rows=[dict(row) for row in data['holdings']]
    refs=[]
    caveats=[]
    for row in rows:
        record_quality(row,sources)
        if not data.get('synthetic') and (len(row['isin'])!=12 or not row['isin'].startswith('IN')):
            raise ValueError('INVALID_INDIAN_ISIN')
        if row.get('asset_class','EQ')!='EQ':
            raise ValueError('UNSUPPORTED_ASSET')
        if decimal(row['quantity'])<=0:
            raise ValueError('NONPOSITIVE_HELD_QUANTITY')
        vdate=date.fromisoformat(row['valuation_date'])
        if vdate>date.fromisoformat(data['as_of']):
            raise ValueError('LOOKAHEAD_INPUT')
        if row.get('market_value') is not None and row['valuation_date']!=data['as_of']:
            raise ValueError('STALE_VALUATION_REQUIRES_UPSTREAM_REMARK')
        refs+=row['source_refs']
        caveats+=row.get('reasons',[]) if row.get('quality')=='provisional' else []
        if row.get('day_change') is not None and row.get('day_change_basis')!='upstream_verified_same_share_basis':
            raise ValueError('MISSING_DAY_CHANGE_BASIS')
    ov=overview(rows)
    ov['health']=Metric.unavailable('RUBRIC_NOT_VALIDATED',unit='score_0_100',method='health_rubric_unavailable')
    ov['capital_state']=Metric.unavailable('RUBRIC_NOT_VALIDATED',unit='buckets',method='capital_state_unavailable')
    total=ov['market_value'].value
    weights={r['isin']:decimal(r['market_value'])/total for r in rows} if total is not None and total>0 else {}
    decorate(ov,refs,caveats,{'as_of':data['as_of']})
    ov['upstream_kpis']=kpis(data.get('upstream_kpis',[]),data,sources)
    classification_errors=[]
    if not data.get('synthetic') or 'classifications' in data:
        for row in rows:
            row['sector']=None
            row['market_cap']=None
        classification_seen=set()
        by_isin={row['isin']:row for row in rows}
        for c in data.get('classifications',[]):
            try:
                record_quality(c,sources)
                field=c['field']
                if field not in ('sector','market_cap') or not c.get('scheme') or not c.get('scheme_version'):
                    raise ValueError('INVALID_CLASSIFICATION_SCHEME')
                key=(c['isin'],field)
                if key in classification_seen:
                    raise ValueError('CONFLICTING_CLASSIFICATION')
                classification_seen.add(key)
                if not date.fromisoformat(c['effective_from'])<=date.fromisoformat(data['as_of'])<=date.fromisoformat(c['effective_to']) or timestamp(c['known_at'])>timestamp(data['knowledge_cutoff']):
                    raise ValueError('CLASSIFICATION_NOT_POINT_IN_TIME')
                if c['isin'] in by_isin:
                    by_isin[c['isin']][field]=c['bucket']
                refs+=c['source_refs']
                if c.get('quality')=='provisional':
                    classification_errors+=c['reasons']
            except (KeyError,ValueError) as exc:
                classification_errors.append(str(exc))
                if c.get('isin') in by_isin and c.get('field') in ('sector','market_cap'):
                    by_isin[c['isin']][c['field']]=None
    diversification={'sector':allocation(rows,'sector'),'market_cap':allocation(rows,'market_cap')}
    decorate(diversification,refs,caveats,{'as_of':data['as_of']})
    for metric in diversification.values():
        for reason in classification_errors:
            metric.caveat(reason)

    # Optional fundamental observations: require a single dated basis per field.
    fundamentals={r['isin']:{'isin':r['isin']} for r in rows}
    all_fund={}
    fund_refs=[]
    fund_errors=[]
    hard_fund_errors=[]
    basis_by_field={}
    seen=set()
    for f in data.get('fundamentals',[]):
        try:
            record_quality(f,sources)
            if timestamp(f['published_at'])>timestamp(data['knowledge_cutoff']):
                raise ValueError('LOOKAHEAD_INPUT')
            if timestamp(f['published_at']).date()>date.fromisoformat(data['as_of']):
                raise ValueError('LOOKAHEAD_INPUT')
            timestamp(f['retrieved_at'])
            if date.fromisoformat(f['fiscal_end'])>date.fromisoformat(data['as_of']):
                raise ValueError('FUTURE_FISCAL_PERIOD')
            if f['field'] not in ('pe','pb','roe','roce','net_margin','debt_to_equity','interest_cover','dividend_yield','sales_growth_1y','profit_growth_1y','capital_adequacy_ratio','gross_npa_ratio'):
                raise ValueError('UNKNOWN_FUNDAMENTAL')
            expected='ratio' if f['field'] in ('pe','pb','debt_to_equity','interest_cover') else 'fraction'
            if f['unit']!=expected:
                raise ValueError('INVALID_UNIT')
            if not data.get('synthetic') and f['field'] in ('pe','pb','dividend_yield') and f.get('valuation_date')!=data['as_of']:
                raise ValueError('MISSING_OR_STALE_FUNDAMENTAL_VALUATION_DATE')
            pair=(f['isin'],f['field'])
            if pair in seen:
                raise ValueError('CONFLICTING_FUNDAMENTALS')
            seen.add(pair)
            basis=(f['basis'],f['consolidation'])
            allowed_fundamental_bases=('point_in_time',) if f['field'] in ('capital_adequacy_ratio','gross_npa_ratio') else ('TTM','annual')
            if f['basis'] not in allowed_fundamental_bases or f['consolidation'] not in ('consolidated','standalone'):
                raise ValueError('INVALID_FUNDAMENTAL_BASIS')
            previous=basis_by_field.get(f['field'])
            if previous and previous[0]!=basis[0]:
                raise ValueError('FUNDAMENTAL_BASIS_MISMATCH')
            if previous and previous[1]!=basis[1]:
                basis=(basis[0],'mixed')
                fund_errors.append('MIXED_STANDALONE_CONSOLIDATED_INPUTS')
            basis_by_field[f['field']]=basis
            value=decimal(f['value']) if f.get('value') is not None and f.get('applicability')=='applicable' else None
            all_fund.setdefault(f['isin'],{})[f['field']]=value
            if f['isin'] in fundamentals:
                fundamentals[f['isin']][f['field']]=value
            fund_refs+=f['source_refs']
            if f.get('quality')=='provisional':
                fund_errors+=f['reasons']
        except (ValueError,KeyError) as e:
            fund_errors.append(str(e))
            hard_fund_errors.append(str(e))
    fund={}
    for field in ('pe','pb','dividend_yield'):
        fund[field]=aggregate([(w,fundamentals[i].get(field)) for i,w in weights.items()],harmonic=field in ('pe','pb')) if weights else Metric.unavailable('UNKNOWN_VALUE_DENOMINATOR')
    # Conflicts are never settled by whichever row happened to come last.
    if hard_fund_errors:
        fund={field:Metric.unavailable('CONFLICTING_OR_INVALID_FUNDAMENTALS',coverage={'errors':sorted(set(hard_fund_errors))}) for field in fund}
        fundamentals={r['isin']:{'isin':r['isin']} for r in rows}
        all_fund={}
    decorate(fund,fund_refs+refs,caveats+fund_errors,{'as_of':data['as_of']})
    fund['dividend_yield'].unit='fraction'
    bfund,comparison,definitions,berrors=benchmark_fundamentals(data.get('benchmark_fundamentals',[]),data,
        fund,basis_by_field,lambda r:record_quality(r,sources),timestamp)
    fund.update(benchmark=bfund,comparison=comparison,benchmark_definitions=definitions,benchmark_errors=berrors)
    map_data=lenses(list(fundamentals.values()))
    for lens in map_data.values():
        lens['source_refs']=sorted(set(fund_refs))
        if fund_errors:
            lens['status']='unavailable' if hard_fund_errors else 'provisional'
            lens['reasons']=sorted(set(lens['reasons']+fund_errors))

    # No calendar compression: discontinuous input cannot form a complete growth path.
    portfolio=validated_intervals(data.get('returns',[]),data,sources)
    allowed_basis={'upstream_cashflow_adjusted_total_return','hypothetical_initial_weight_price_return','hypothetical_initial_weight_total_return'}
    if any(r.get('basis') not in allowed_basis or not r.get('scope') for r in portfolio):
        raise ValueError('MISSING_OR_UNSUPPORTED_RETURN_BASIS')
    gaps=any(a['end']!=b['start'] for a,b in zip(portfolio,portfolio[1:]))
    rr={}
    period={'start':portfolio[0]['start'],'end':portfolio[-1]['end']} if portfolio else {}
    days=(date.fromisoformat(period['end'])-date.fromisoformat(period['start'])).days if period else None
    optional_errors={}
    optional_refs={}
    optional_caveats={}
    def match_optional(key, records=portfolio):
        try:
            other=validated_intervals(data.get(key,[]),data,sources)
            optional_refs[key]=[ref for r in other for ref in r['source_refs']]
            optional_caveats[key]=[reason for r in other if r.get('quality')=='provisional' for reason in r.get('reasons',[])]
            by={(x['start'],x['end']):x['return'] for x in other}
            if not records or any((r['start'],r['end']) not in by for r in records):
                return None
            return [by[(r['start'],r['end'])] for r in records]
        except (ValueError,KeyError,TypeError) as exc:
            optional_errors[key]=str(exc)
            return None
    annual=False
    if period:
        start=date.fromisoformat(period['start'])
        try:
            anniversary=start.replace(year=start.year+1)
        except ValueError:
            anniversary=start.replace(year=start.year+1,day=28)
        annual=date.fromisoformat(period['end'])>=anniversary
    rr=risk([r['return'] for r in portfolio],rf=match_optional('risk_free_returns'),
            target=match_optional('target_returns'),elapsed_days=days,annualise_allowed=annual,
            portfolio_value=total)
    if gaps:
        for key in ('cum_return','cagr','growth_100k','max_drawdown'):
            rr[key]=Metric.unavailable('GAPPED_PORTFOLIO_SERIES',unit='INR' if key=='growth_100k' else 'ratio')
        for metric in rr.values():
            metric.caveat('PARTIAL_INTERVAL_COVERAGE')
    # Benchmark pairing never shortens portfolio-only statistics.
    try:
        bench=validated_intervals(data.get('benchmark_returns',[]),data,sources)
        if bench and portfolio and bench[0].get('basis') != ('total_return' if 'total_return' in portfolio[0]['basis'] else 'price_return'):
            raise ValueError('BENCHMARK_BASIS_MISMATCH')
        by={(x['start'],x['end']):x['return'] for x in bench}
        paired_records=[x for x in portfolio if (x['start'],x['end']) in by]
        pairs=[(x['return'],by[(x['start'],x['end'])]) for x in paired_records]
        compared=risk([x for x,y in pairs],benchmark=[y for x,y in pairs])
        for key in ('beta','tracking_error','information_ratio'):
            rr[key]=compared[key]
            rr[key].coverage={'eligible_count':len(pairs),'total_count':len(portfolio),
                              'intervals':[[r['start'],r['end']] for r in paired_records]}
            if len(pairs)<len(portfolio):
                rr[key].caveat('PARTIAL_BENCHMARK_COVERAGE')
    except (ValueError,KeyError,TypeError) as e:
        bench=[]
        for key in ('beta','tracking_error','information_ratio'):
            rr[key]=Metric.unavailable(str(e))
    return_refs=[ref for r in portfolio for ref in r['source_refs']]
    return_caveats=[reason for r in portfolio if r.get('quality')=='provisional' for reason in r.get('reasons',[])]
    if any(r.get('excluded_isins') for r in portfolio):
        return_caveats.append('UPSTREAM_EXCLUDED_HOLDINGS')
    decorate(rr,return_refs,return_caveats,period)
    decorate(rr['var_1d_95_value'],return_refs+ov['market_value'].source_refs,
             return_caveats+ov['market_value'].reasons,period)
    for field,key in [('sharpe','risk_free_returns'),('sortino','target_returns')]:
        rr[field].source_refs=sorted(set(return_refs+optional_refs.get(key,[])))
        for reason in optional_caveats.get(key,[])+([optional_errors[key]] if key in optional_errors else []):
            rr[field].caveat(reason)
    for key in ('beta','tracking_error','information_ratio'):
        rr[key].source_refs=sorted(set(return_refs+[ref for r in bench for ref in r['source_refs']]))
        for reason in [reason for r in bench if r.get('quality')=='provisional' for reason in r.get('reasons',[])]:
            rr[key].caveat(reason)
    growth=[]
    if portfolio and not gaps:
        value=D('100000')
        growth=[{'date':portfolio[0]['start'],'value':format(value,'.2f')}]
        for r in portfolio:
            value*=ONE+r['return']
            growth.append({'date':r['end'],'value':Metric(value,unit='INR').json()['value']})
    benchmark_growth=[]
    benchmark_risk=None
    if portfolio and not gaps and bench:
        lookup={(r['start'],r['end']):r for r in bench}
        if all((r['start'],r['end']) in lookup for r in portfolio):
            matching=[lookup[(r['start'],r['end'])] for r in portfolio]
            value=D('100000')
            benchmark_growth=[{'date':portfolio[0]['start'],'value':'100000.00'}]
            for r in matching:
                value*=ONE+r['return']
                benchmark_growth.append({'date':r['end'],'value':Metric(value,unit='INR').json()['value']})
            benchmark_risk=risk([r['return'] for r in matching],elapsed_days=days,annualise_allowed=annual,
                                rf=match_optional('risk_free_returns',matching),target=match_optional('target_returns',matching))
            decorate(benchmark_risk,[ref for r in matching for ref in r['source_refs']],
                     [reason for r in matching if r.get('quality')=='provisional' for reason in r.get('reasons',[])],period)
    performance={'growth_series':Metric(growth,unit='INR_series',method='linked_upstream_returns_v1') if growth else Metric.unavailable('MISSING_OR_GAPPED_HISTORY',unit='INR_series'),
                 'basis':portfolio[0]['basis'] if portfolio else None,'scope':portfolio[0]['scope'] if portfolio else None,
                 'cum_return':rr['cum_return'],'cagr':rr['cagr'],
                 'benchmark_growth':Metric(benchmark_growth,unit='INR_series',method='matched_benchmark_link_v1') if benchmark_growth else Metric.unavailable('MISSING_MATCHED_BENCHMARK_HISTORY',unit='INR_series'),
                 'benchmark_risk':benchmark_risk,
                 'growth_delta':Metric(decimal(growth[-1]['value'])-decimal(benchmark_growth[-1]['value']),unit='INR',method='displayed_growth_endpoint_difference_v1') if growth and benchmark_growth else Metric.unavailable('MISSING_MATCHED_BENCHMARK_HISTORY',unit='INR')}
    decorate(performance,return_refs,return_caveats,period)
    if benchmark_growth:
        breasons=[reason for r in bench if r.get('quality')=='provisional' for reason in r.get('reasons',[])]
        brefs=[ref for r in bench for ref in r['source_refs']]
        decorate(performance['benchmark_growth'],brefs,breasons,period)
        decorate(performance['growth_delta'],return_refs+brefs,return_caveats+breasons,period)
        decorate(performance['benchmark_risk'],brefs,breasons,period)
        for field,key in [('sharpe','risk_free_returns'),('sortino','target_returns')]:
            decorate(performance['benchmark_risk'][field],brefs+optional_refs.get(key,[]),
                     breasons+optional_caveats.get(key,[])+([optional_errors[key]] if key in optional_errors else []),period)

    calendar=None
    calendar_error=None
    if data.get('calendar_coverage'):
        try:
            calendar=data['calendar_coverage']
            record_quality(calendar,sources)
            if date.fromisoformat(calendar['start'])>date.fromisoformat(calendar['end']):
                raise ValueError('INVALID_CALENDAR_COVERAGE')
        except (ValueError,KeyError,TypeError) as exc:
            calendar=None
            calendar_error=str(exc)
    optionals={}
    for key in ('risk_free_returns','target_returns'):
        try:
            optionals[key]=validated_intervals(data.get(key,[]),data,sources)
        except (ValueError,KeyError,TypeError):
            optionals[key]=[]
    performance['windows']=windows(portfolio,sessions,data['as_of'],calendar=calendar,benchmark=bench,
                                  rf=optionals['risk_free_returns'],target=optionals['target_returns'],portfolio_value=total)
    for window in performance['windows'].values():
        if window.value:
            metric=window.value['risk']['var_1d_95_value']
            metric.source_refs=sorted(set(metric.source_refs+ov['market_value'].source_refs))
            for reason in ov['market_value'].reasons:
                metric.caveat(reason)
    if calendar_error:
        for window in performance['windows'].values():
            window.caveat(calendar_error)

    # Per-security histories have explicit source and adjustment basis.
    histories={}
    hist_errors={}
    history_seen=set()
    for entry in data.get('asset_histories',[]):
        try:
            record_quality(entry,sources)
            i=entry['isin']
            if i in history_seen:
                raise ValueError('DUPLICATE_ASSET_HISTORY')
            history_seen.add(i)
            if entry['basis'] not in ('split_adjusted_price','total_return'):
                raise ValueError('UNSUPPORTED_ADJUSTMENT_BASIS')
            if not entry.get('adjustment_version'):
                raise ValueError('MISSING_ADJUSTMENT_VERSION')
            levels={}
            for p in entry['levels']:
                if p['date'] in levels:
                    raise ValueError('DUPLICATE_LEVEL_DATE')
                value=decimal(p['value'])
                if value<=0:
                    raise ValueError('NONPOSITIVE_LEVEL')
                levels[p['date']]=value
            intervals=interval_returns(levels,sessions)
            histories[i]={'levels':levels,'intervals':intervals,'basis':entry['basis'],'source_refs':entry['source_refs'],
                          'reasons':entry.get('reasons',[]) if entry.get('quality')=='provisional' else []}
        except (ValueError,KeyError) as e:
            hist_errors[entry.get('isin','UNKNOWN')]=str(e)
            histories.pop(entry.get('isin'),None)
    pairs=[]
    ids=sorted(r['isin'] for r in rows)
    for a_index,a in enumerate(ids):
        for b in ids[a_index+1:]:
            ha,hb=histories.get(a),histories.get(b)
            if not ha or not hb:
                metric=Metric.unavailable('MISSING_ASSET_HISTORY')
            elif ha['basis']!=hb['basis']:
                metric=Metric.unavailable('BASIS_MISMATCH')
            else:
                by={(r['start'],r['end']):r['return'] for r in hb['intervals']}
                matched=[r for r in ha['intervals'] if (r['start'],r['end']) in by]
                aligned=[(r['return'],by[(r['start'],r['end'])]) for r in matched]
                metric=correlation([x for x,y in aligned],[y for x,y in aligned])
                metric.coverage.update(total_count=max(0,len(sessions)-1),intervals=[[r['start'],r['end']] for r in matched])
                decorate(metric,ha['source_refs']+hb['source_refs'],ha['reasons']+hb['reasons'],
                         {'start':matched[0]['start'],'end':matched[-1]['end']} if matched else {})
                if len(matched)<max(0,len(sessions)-1):
                    metric.caveat('PARTIAL_INTERVAL_COVERAGE')
            pairs.append({'isin_a':a,'isin_b':b,'correlation':metric})
    diversification['correlation_pairs']=pairs
    diversification['history_errors']=hist_errors
    performance['hypothetical']=buy_and_hold(data.get('hypothetical'),weights,histories,sessions,data['as_of'],refs,caveats)
    diversification['effective_holdings']=ov['effective_holdings']
    factors={}
    universe=data.get('universe',{})
    members=universe.get('members',[])
    uid=universe.get('id')
    universe_error=None
    if universe:
        try:
            record_quality(universe,sources)
            if not date.fromisoformat(universe['effective_from'])<=date.fromisoformat(data['as_of'])<=date.fromisoformat(universe['effective_to']) or timestamp(universe['known_at'])>timestamp(data['knowledge_cutoff']):
                raise ValueError('UNIVERSE_NOT_POINT_IN_TIME')
        except (ValueError,KeyError) as e:
            universe_error=str(e)
            members=[]
    raw={'momentum':{},'low_volatility':{},'value':{},'quality':{}}
    tail=sessions[-253:]
    if len(tail)==253:
        for i,h in histories.items():
            levels=h['levels']
            if h['basis']!='split_adjusted_price' or not all(d in levels for d in tail):
                continue
            vals=[levels[d] for d in tail]
            raw['momentum'][i]=vals[-22]/vals[0]-ONE
            ret=[b/a-ONE for a,b in zip(vals,vals[1:])]
            raw['low_volatility'][i]=-variance(ret).sqrt()*D(252).sqrt()
    for key,fields in [('value',('pe','pb')),('quality',('roe','net_margin','debt_to_equity'))]:
        eligible={i for i in members if all(all_fund.get(i,{}).get(field) is not None for field in fields)}
        if key=='value':
            eligible={i for i in eligible if all(all_fund[i][field]>0 for field in fields)}
        component_ranks=[]
        for field in fields:
            component={i:(ONE/all_fund[i][field] if key=='value' else -all_fund[i][field] if field=='debt_to_equity' else all_fund[i][field]) for i in eligible}
            component_ranks.append(ranks(component))
        if len(eligible)>=2:
            raw[key]={i:sum((rank[i] for rank in component_ranks),ZERO)/D(len(fields)) for i in eligible}
    # v2: a holding outside the universe gets its value percentile against the same peers
    # (each field ranked with the peers plus that one holding), so the portfolio's value
    # score covers it instead of dropping it.
    outside=[i for i in weights if i not in members and raw['value']
             and all((all_fund.get(i,{}).get(f) or ZERO)>0 for f in ('pe','pb'))]
    peers={f:{i:ONE/all_fund[i][f] for i in raw['value']} for f in ('pe','pb')}
    for i in outside:
        raw['value'][i]=sum((ranks({**peers[f],i:ONE/all_fund[i][f]})[i] for f in ('pe','pb')),ZERO)/D(2)
    for key in raw:
        factors[key]=factor(raw[key],weights,members,uid,pre_scored=key in ('value','quality'))
        if key=='value' and outside and factors[key]['eligible_count']>=30:
            metric=aggregate([(w,raw[key].get(i)) for i,w in weights.items()])
            metric.unit='score_0_100'
            metric.method='mean_component_percentiles_v1'
            metric.caveat('HOLDINGS_OUTSIDE_UNIVERSE_RANKED_AGAINST_PEERS')
            factors[key]['portfolio']=metric
            factors[key]['holdings']={i:raw[key][i] for i in weights if i in raw[key]}
        if universe_error:
            factors[key]['portfolio']=Metric.unavailable(universe_error)
        if not weights:
            factors[key]['portfolio']=Metric.unavailable('UNKNOWN_VALUE_DENOMINATOR')
        hcaveats=[reason for h in histories.values() for reason in h['reasons']] if key in ('momentum','low_volatility') else fund_errors
        if universe.get('quality')=='provisional':
            hcaveats=hcaveats+universe.get('reasons',[])
        decorate(factors[key],refs+fund_refs+[ref for h in histories.values() for ref in h['source_refs']]+universe.get('source_refs',[]),caveats+hcaveats)
    profiles={}
    profile_errors=[]
    profile_refs=[]
    profile_reasons=[]
    for profile in data.get('health_profiles',[]):
        try:
            record_quality(profile,sources)
            if profile['isin'] in profiles:
                raise ValueError('CONFLICTING_HEALTH_PROFILE')
            if profile['as_of']!=data['as_of']:
                raise ValueError('HEALTH_PROFILE_DATE_MISMATCH')
            if profile['profile'] not in ('non_financial','bank','nbfc'):
                raise ValueError('UNSUPPORTED_HEALTH_PROFILE')
            profiles[profile['isin']]=profile['profile']
            profile_refs+=profile['source_refs']
            if profile.get('quality')=='provisional':profile_reasons+=profile['reasons']
        except (ValueError,KeyError,TypeError) as exc:
            profile_errors.append(str(exc))
    quality_freshness_errors=[]
    for observation in data.get('fundamentals',[]):
        profile=profiles.get(observation.get('isin'))
        from .health import PROFILE_FIELDS
        used_fields=PROFILE_FIELDS.get(profile,())
        if observation.get('field') in used_fields:
            try:
                months=6 if observation['field'] in ('capital_adequacy_ratio','gross_npa_ratio') else 18
                if date.fromisoformat(observation['fiscal_end'])<subtract_months(date.fromisoformat(data['as_of']),months):
                    quality_freshness_errors.append('STALE_HEALTH_FUNDAMENTALS:'+observation['isin']+':'+observation['field'])
            except (ValueError,KeyError,TypeError):
                quality_freshness_errors.append('INVALID_HEALTH_FUNDAMENTAL_DATE')
    health_sessions=sessions[-253:]
    health_window=None
    if len(health_sessions)==253 and calendar and calendar['start']<=health_sessions[0] and calendar['end']>=data['as_of']:
        selected_health_returns=[r for r in portfolio if health_sessions[0]<=r['start'] and r['end']<=health_sessions[-1]]
        # v2: risk may cover the reconciled book only if the exclusions never change.
        scopes={tuple(sorted(r.get('excluded_isins') or [])) for r in selected_health_returns}
        if len(scopes)>1:
            health_window=Metric.unavailable('CHANGING_UPSTREAM_SCOPE',unit='window')
        else:
            health_window=windows(selected_health_returns,health_sessions,data['as_of'],calendar=calendar)['All']
    if health_window is None:
        health_window=Metric.unavailable('MISSING_COMPLETE_252_INTERVAL_WINDOW',unit='window')
    health_period=health_window.period if health_window.value else None
    components={
        'concentration':concentration(weights,diversification['sector']),
        'diversification':health_diversification(weights,histories,sessions,health_period),
        'risk':observed_risk(health_window),
        'quality':business_quality(weights,all_fund,profiles,errors=hard_fund_errors+profile_errors+quality_freshness_errors,
                                   refs=fund_refs+profile_refs,reasons=fund_errors+profile_reasons),
        'valuation':valuation(factors['value'])}
    ov['health']=assemble(components,as_of=data['as_of'],refs=refs,reasons=caveats)
    from .health_evidence import score_sensitivity, history_evidence, grade_boundary_sensitivity, drawdown_recovery
    from .health import WEIGHTS
    ov['health_weight_sensitivity']=score_sensitivity({k:v['score'].value for k,v in components.items()},WEIGHTS)
    ov['health_grade_sensitivity']=grade_boundary_sensitivity(ov['health']['score'].value)
    recovery_complete=(health_period is not None and health_window.coverage.get('eligible_count')==252
                       and health_period.get('start')==health_sessions[0]
                       and health_period.get('end')==data['as_of']
                       and 'UPSTREAM_EXCLUDED_HOLDINGS' not in health_window.reasons)
    ov['health_recovery']=(Metric(drawdown_recovery([r['return'] for r in selected_health_returns]),
                                  method='underwater_sessions_v1',period=health_period,
                                  source_refs=list(health_window.source_refs),coverage=dict(health_window.coverage))
                           .caveat('WINDOW_CENSORED_RECOVERY') if recovery_complete
                           else Metric.unavailable('INCOMPLETE_HEALTH_RECOVERY_WINDOW'))
    for reason in health_window.reasons:
        ov['health_recovery'].caveat(reason)
    ov['health_covariance_evidence']=history_evidence(weights,histories,sessions,health_period)
    ov['health_validation']={'grade_calibration':'pending','indian_equity_mf_validation':'pending','independent_human_review':'pending','model':'portfolio-health-v1','evidence_extension':'v2'}
    ov['capital_state']=capital_states(rows,histories,sessions,data['as_of'])
    result={'schema_version':'0.1','method_version':METHOD_VERSION,
            'input_digest':hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
            'as_of':data['as_of'],'generated_at':data['generated_at'],'sources':data['sources'],
            'sections':{'overview':ov,'factor_score':factors,'risk_return':rr,'diversification':diversification,
                        'fundamentals':fund,'performance':performance,'valuation_map':map_data},
            'exclusions':{'unpriced_isins':[r['isin'] for r in rows if r.get('market_value') is None]},
            'status':'provisional','reasons':['INDEPENDENT_BACKEND_NOT_INTEGRATED','LIVE_KALPI_PARITY_UNVERIFIED']}
    result=encode(result)
    result['compatibility']=legacy_projection(result)
    return result


def legacy_projection(result):
    """Explicit refusal, not an apparently compatible payload with fake health values."""
    return {'compatible':False,'status':'unavailable','value':None,
            'reasons':['LEGACY_UNREPRESENTABLE'],
            'details':['Existing HealthOut requires an integer score and UI imputes missing components as 50.',
                       'Existing UI has hardcoded backtest basis and no metric-level status contract.',
                       'No existing route or UI was changed; independent output is not a drop-in response.']}
