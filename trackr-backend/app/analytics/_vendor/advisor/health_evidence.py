"""Portfolio diagnostics; upstream owns alignment, currency and source attestations."""
from decimal import Decimal as D, ROUND_HALF_UP
from .core import Metric, decimal, precision, ZERO, ONE
from .stats import mean, variance

@precision
def expected_shortfall(returns, confidence=D('.95')):
    """Signed empirical loss quantile integral; exact fractional boundary mass."""
    c=decimal(confidence)
    if not ZERO<c<ONE:raise ValueError('INVALID_CONFIDENCE')
    a=sorted(decimal(x) for x in returns)
    if not a:raise ValueError('EMPTY_RETURNS')
    if any(x < -ONE for x in a):raise ValueError('RETURN_BELOW_MINUS_ONE')
    mass=D(len(a))*(ONE-c);k=int(mass);fraction=mass-D(k)
    return -(sum(a[:k],ZERO)+(fraction*a[k] if fraction else ZERO))/mass

@precision
def covariance_diagnostics(weights, returns, shrinkage='0'):
    """Aligned decimal returns. Identity-target sensitivity, not Ledoit-Wolf."""
    ids=sorted(weights);w=[decimal(weights[i]) for i in ids];alpha=decimal(shrinkage)
    if not ids or any(x<0 for x in w) or abs(sum(w,ZERO)-ONE)>D('1e-28'):
        raise ValueError('INVALID_COMPLETE_WEIGHTS')
    if not ZERO<=alpha<=ONE:raise ValueError('INVALID_SHRINKAGE')
    if set(ids)-set(returns):return Metric.unavailable('MISSING_ASSET_RETURNS')
    a=[[decimal(x) for x in returns[i]] for i in ids];n=len(a[0])
    if n<30 or any(len(x)!=n for x in a):return Metric.unavailable('INSUFFICIENT_ALIGNED_HISTORY')
    if any(x < -ONE for row in a for x in row):raise ValueError('RETURN_BELOW_MINUS_ONE')
    means=[mean(x) for x in a]
    cov=[[sum(((x-means[i])*(y-means[j]) for x,y in zip(a[i],a[j])),ZERO)/D(n-1) for j in range(len(ids))] for i in range(len(ids))]
    target=sum((cov[i][i] for i in range(len(ids))),ZERO)/D(len(ids))
    matrix=[[(ONE-alpha)*cov[i][j]+(alpha*target if i==j else ZERO) for j in range(len(ids))] for i in range(len(ids))]
    marginal=[sum((matrix[i][j]*w[j] for j in range(len(ids))),ZERO) for i in range(len(ids))]
    v=sum((w[i]*marginal[i] for i in range(len(ids))),ZERO)
    if v<=0:return Metric.unavailable('ZERO_PORTFOLIO_VARIANCE')
    vol=v.sqrt();rc=[w[i]*marginal[i]/vol for i in range(len(ids))]
    ratio=sum((w[i]*matrix[i][i].sqrt() for i in range(len(ids))),ZERO)/vol
    return Metric({'annual_volatility':vol*D(252).sqrt(),'risk_contributions':{i:{'annual_volatility_contribution':rc[k]*D(252).sqrt(),'risk_share':rc[k]/vol} for k,i in enumerate(ids)},'diversification_volatility_ratio':ratio,'shrinkage_strength':alpha,'observations':n},method='covariance_euler_identity_target_v1').caveat('ESTIMATED_FROM_HISTORICAL_RETURNS')

@precision
def drawdown_recovery(returns):
    wealth=peak=ONE;age=longest=0;recovered=[]
    for raw in returns:
        x=decimal(raw)
        if x < -ONE:raise ValueError('RETURN_BELOW_MINUS_ONE')
        wealth*=ONE+x
        if wealth>=peak:
            if age:recovered.append(age+1)
            age=0;peak=wealth
        else:age+=1;longest=max(longest,age)
    return {'longest_underwater_sessions':longest,'current_underwater_sessions':age,'completed_recovery_sessions':recovered,'terminal_drawdown':wealth/peak-ONE}

@precision
def exposure_overlap(exposures_a, exposures_b):
    """Complete normalized underlying maps, including residual cash buckets."""
    a={i:decimal(v) for i,v in exposures_a.items()};b={i:decimal(v) for i,v in exposures_b.items()}
    if any(v<0 for v in list(a.values())+list(b.values())) or any(abs(sum(x.values(),ZERO)-ONE)>D('1e-28') for x in (a,b)):
        return Metric.unavailable('INCOMPLETE_UNDERLYING_EXPOSURES')
    return Metric(sum((min(a.get(i,ZERO),b.get(i,ZERO)) for i in set(a)|set(b)),ZERO),method='sum_min_complete_exposures_v1')

@precision
def score_sensitivity(scores, weights, relative_change='.2'):
    """One-at-a-time +/- relative weight perturbations, renormalized explicitly."""
    if set(scores)!=set(weights) or any(scores[i] is None for i in scores):return Metric.unavailable('MISSING_COMPONENT_SCORES')
    s={i:decimal(v) for i,v in scores.items()};w={i:decimal(v) for i,v in weights.items()};d=decimal(relative_change)
    if not ZERO<=d<=ONE or any(x<0 for x in w.values()) or sum(w.values(),ZERO)!=ONE or any(not ZERO<=x<=100 for x in s.values()):raise ValueError('INVALID_SENSITIVITY_INPUT')
    values=[]
    for key in sorted(w):
        for sign in (-ONE,ONE):
            varied=dict(w);varied[key]*=ONE+sign*d;den=sum(varied.values(),ZERO)
            if den:values.append(sum((varied[i]*s[i] for i in w),ZERO)/den)
    if not values:return Metric.unavailable('NO_WEIGHT_SCENARIOS')
    from .health import BANDS
    def grade(v):
        rounded=v.quantize(ONE,rounding=ROUND_HALF_UP)
        return next(g for g,t in BANDS if rounded>=t)
    return Metric({'minimum':min(values),'maximum':max(values),'grades':sorted(set(grade(v) for v in values)),'scenario_count':len(values),'relative_weight_change':d},unit='score_range',method='one_at_time_weight_sensitivity_v1').caveat('SCENARIO_RANGE_NOT_CONFIDENCE_INTERVAL')

@precision
def linked_scenario(returns):
    r=[decimal(x) for x in returns]
    if not r:return Metric.unavailable('MISSING_SCENARIO_HISTORY')
    if any(x < -ONE for x in r):raise ValueError('RETURN_BELOW_MINUS_ONE')
    growth=ONE
    for x in r:growth*=ONE+x
    return Metric(growth-ONE,method='linked_historical_scenario_v1').caveat('HISTORICAL_SCENARIO_NOT_FORECAST')

@precision
def history_evidence(weights, histories, sessions, period):
    """Same upstream-approved window as health; never compress gaps."""
    from .stats import interval_returns
    if not period:return Metric.unavailable('MISSING_COMPLETE_252_INTERVAL_WINDOW')
    dates=[s for s in sessions if period['start']<=s<=period['end']]
    active={i:w for i,w in weights.items() if w>0};returns={};bases=set();refs=[];reasons=[]
    if len(dates)!=253:return Metric.unavailable('INCOMPLETE_252_INTERVAL_WINDOW')
    for i in active:
        h=histories.get(i)
        if not h or any(d not in h['levels'] for d in dates):return Metric.unavailable('INCOMPLETE_ASSET_HISTORY:'+i)
        bases.add(h['basis']);refs+=h['source_refs'];reasons+=h.get('reasons',[])
        returns[i]=[r['return'] for r in interval_returns({d:h['levels'][d] for d in dates},dates)]
    if len(bases)!=1:return Metric.unavailable('HISTORY_BASIS_MISMATCH')
    sample=covariance_diagnostics(active,returns)
    variants={a:covariance_diagnostics(active,returns,a) for a in ('0.1','0.3','0.5')}
    if sample.value is None:return sample
    result=Metric({'sample_covariance':sample,'identity_target_sensitivity':variants},method='health_covariance_evidence_v2',period=period,source_refs=sorted(set(refs)))
    result.caveat('CURRENT_WEIGHTS_HISTORICAL_CONSTANT_MIX_DIAGNOSTIC')
    for reason in reasons:result.caveat(reason)
    return result

@precision
def grade_boundary_sensitivity(score, shift='5'):
    """Shift all internal grade cutoffs, preserving display rounding semantics."""
    if score is None:return Metric.unavailable('MISSING_HEALTH_SCORE')
    from .health import BANDS
    value=decimal(score).quantize(ONE,rounding=ROUND_HALF_UP);delta=decimal(shift)
    if not ZERO<=value<=100 or not ZERO<=delta<=10:raise ValueError('INVALID_GRADE_SENSITIVITY')
    grades={str(move):next(g for g,t in BANDS if value>= (D(t)+move if t else ZERO)) for move in (-delta,ZERO,delta)}
    return Metric(grades,unit='grade_scenarios',method='uniform_grade_cutoff_shift_v1').caveat('DESIGN_SENSITIVITY_NOT_CALIBRATION')
