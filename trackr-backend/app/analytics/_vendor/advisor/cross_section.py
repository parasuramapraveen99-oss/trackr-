"""Pure cross-sectional diagnostics over materialised engine holdings."""
from decimal import Decimal as D
from .core import decimal, Metric, ZERO, ONE, precision


def maybe(value):
    return None if value is None else decimal(value)


@precision
def overview(rows):
    keys=['invested','market_value','unrealised','unrealised_pct','day_change','day_change_pct',
          'hhi','effective_holdings','value_coverage','count_coverage','known_value_subtotal']
    units={'invested','market_value','unrealised','day_change','known_value_subtotal'}
    out={k:Metric.unavailable('EMPTY_PORTFOLIO',unit='INR' if k in units else 'ratio',method=k+'_v1') for k in keys}
    if not rows:
        return out
    if len({r['isin'] for r in rows})!=len(rows):
        raise ValueError('DUPLICATE_ISIN')
    values=[maybe(r.get('market_value')) for r in rows]
    costs=[maybe(r.get('invested')) for r in rows]
    if any(x is not None and x<0 for x in values+costs):
        raise ValueError('NEGATIVE_LONG_ONLY_VALUE_OR_COST')
    known=[x for x in values if x is not None]
    subtotal=sum(known,ZERO)
    complete=len(known)==len(rows)
    out['count_coverage']=Metric(D(len(known))/D(len(rows)),method='priced_count_v1')
    out['known_value_subtotal']=Metric(subtotal,unit='INR',method='known_subset_sum_v1') if known else Metric.unavailable('NO_PRICED_HOLDINGS',unit='INR')
    if not complete:
        out['known_value_subtotal'].caveat('PARTIAL_COVERAGE')
    out['value_coverage']=Metric(ONE,method='complete_market_value_v1') if complete else Metric.unavailable('UNKNOWN_VALUE_COVERAGE')
    out['market_value']=Metric(subtotal,unit='INR',method='engine_value_sum_v1') if complete else Metric.unavailable('MISSING_MARKET_VALUE',unit='INR')
    cost=sum((x for x in costs if x is not None),ZERO)
    out['invested']=Metric(cost,unit='INR',method='engine_cost_sum_v1') if all(x is not None for x in costs) else Metric.unavailable('MISSING_COST',unit='INR')
    if complete and out['invested'].value is not None:
        out['unrealised']=Metric(subtotal-cost,unit='INR',method='value_minus_cost_v1')
        out['unrealised_pct']=Metric((subtotal-cost)/cost,method='unrealised_on_remaining_cost_v1') if cost>0 else Metric.unavailable('UNDEFINED_DENOMINATOR')
    else:
        out['unrealised']=Metric.unavailable('MISSING_VALUE_OR_COST',unit='INR')
        out['unrealised_pct']=Metric.unavailable('MISSING_VALUE_OR_COST')
    if complete and subtotal>0:
        hhi=sum(((x/subtotal)**2 for x in values),ZERO)
        out['hhi']=Metric(hhi,method='market_value_hhi_v1')
        out['effective_holdings']=Metric(ONE/hhi,unit='count',method='inverse_hhi_v1')
    else:
        for k in ('hhi','effective_holdings'):
            out[k]=Metric.unavailable('MISSING_OR_ZERO_DENOMINATOR')
    daily=[maybe(r.get('day_change')) for r in rows]
    if complete and all(x is not None for x in daily):
        change=sum(daily,ZERO)
        out['day_change']=Metric(change,unit='INR',method='upstream_same_basis_day_change_v1')
        out['day_change_pct']=Metric(change/(subtotal-change)) if subtotal-change>0 else Metric.unavailable('UNDEFINED_DENOMINATOR')
    else:
        out['day_change']=Metric.unavailable('MISSING_COMPATIBLE_DAY_CHANGE',unit='INR')
        out['day_change_pct']=Metric.unavailable('MISSING_COMPATIBLE_DAY_CHANGE')
    return out


@precision
def allocation(rows,field):
    ov=overview(rows)
    total=ov['market_value'].value
    if total is None or total<=0:
        return Metric.unavailable('MISSING_OR_ZERO_DENOMINATOR',method='value_bucket_allocation_v1')
    buckets={}
    for row in rows:
        key=row.get(field) or 'UNKNOWN'
        buckets[key]=buckets.get(key,ZERO)+decimal(row['market_value'])
    out=Metric({k:{'value':v,'weight':v/total} for k,v in sorted(buckets.items())},unit='allocation',method='value_bucket_allocation_v1')
    if 'UNKNOWN' in buckets:
        out.caveat('MISSING_CLASSIFICATION')
    return out


@precision
def aggregate(pairs, *, harmonic=False):
    if any(w<0 for w,v in pairs):
        raise ValueError('NEGATIVE_WEIGHT')
    method='positive_weighted_harmonic_v1' if harmonic else 'weighted_arithmetic_v1'
    total=sum((w for w,v in pairs),ZERO)
    usable=[(w,v) for w,v in pairs if w>0 and v is not None and (not harmonic or v>0)]
    used=sum((w for w,v in usable),ZERO)
    coverage={'eligible_count':len(usable),'total_count':len(pairs),'weight_fraction':used/total if total>0 else None}
    if not usable or total<=0:
        return Metric.unavailable('NO_ELIGIBLE_INPUT',method=method,coverage=coverage)
    value=used/sum((w/v for w,v in usable),ZERO) if harmonic else sum((w*v for w,v in usable),ZERO)/used
    out=Metric(value,method=method,coverage=coverage)
    if used<total:
        out.caveat('PARTIAL_COVERAGE')
    if harmonic and any(v is not None and v<=0 for w,v in pairs):
        out.caveat('NONPOSITIVE_MULTIPLE')
    return out


@precision
def ranks(values):
    if len(values)<2:
        return {}
    groups={}
    for key,value in values.items():
        groups.setdefault(decimal(value),[]).append(key)
    out={}
    start=0
    for value,keys in sorted(groups.items()):
        rank=(D(start)+D(start+len(keys)-1))/D(2)
        for key in keys:
            out[key]=D(100)*rank/D(len(values)-1)
        start+=len(keys)
    return out


@precision
def factor(raw, weights, members, universe_id, *, pre_scored=False):
    if not members or not universe_id:
        return {'portfolio':Metric.unavailable('MISSING_UNIVERSE'),'holdings':{},'eligible_count':0}
    if len(set(members))!=len(members):
        raise ValueError('DUPLICATE_UNIVERSE_MEMBER')
    eligible={i:raw[i] for i in members if i in raw and raw[i] is not None}
    if len(eligible)<30:
        return {'portfolio':Metric.unavailable('INSUFFICIENT_ELIGIBLE_PEERS',coverage={'eligible_count':len(eligible),'total_count':len(members)}),'holdings':{},'eligible_count':len(eligible)}
    scored=eligible if pre_scored else ranks(eligible)
    metric=aggregate([(w,scored.get(i)) for i,w in weights.items()])
    metric.unit='score_0_100'
    metric.method='mean_component_percentiles_v1' if pre_scored else 'fixed_universe_tied_percentile_v1'
    if len(eligible)<len(members):
        metric.caveat('PARTIAL_UNIVERSE_COVERAGE')
    return {'portfolio':metric,'holdings':{i:scored[i] for i in weights if i in scored},
            'universe':universe_id,'eligible_count':len(eligible),'total_count':len(members)}


LENSES={'valuation':('pe','pb',True,True),'growth':('profit_growth_1y','sales_growth_1y',False,False),
        'profitability':('roe','net_margin',False,False),'quality':('debt_to_equity','interest_cover',True,False)}


@precision
def lenses(rows):
    out={}
    for key,(xf,yf,xlower,ylower) in LENSES.items():
        points=[]
        excluded={}
        for row in rows:
            x,y=maybe(row.get(xf)),maybe(row.get(yf))
            if x is None or y is None:
                excluded[row['isin']]='MISSING_AXIS'
            elif key=='valuation' and (x<=0 or y<=0):
                excluded[row['isin']]='NONPOSITIVE_MULTIPLE'
            else:
                points.append({'isin':row['isin'],'x':x,'y':y})
        mx=sum((p['x'] for p in points),ZERO)/D(len(points)) if points else None
        my=sum((p['y'] for p in points),ZERO)/D(len(points)) if points else None
        for point in points:
            dx=mx-point['x'] if xlower else point['x']-mx
            dy=my-point['y'] if ylower else point['y']-my
            point['relative_status']='both_favourable' if dx>0 and dy>0 else 'both_unfavourable' if dx<0 and dy<0 else 'mixed_or_equal'
        out[key]={'status':'provisional' if excluded and points else 'available' if points else 'unavailable',
                  'reasons':['EXCLUDED_AXES'] if excluded else ['EMPTY_PORTFOLIO'] if not points else [],
                  'x_field':xf,'y_field':yf,'x_average':mx,'y_average':my,'points':points,'excluded':excluded,
                  'method':'arithmetic_reference_plottable_holdings_v1'}
    return out
