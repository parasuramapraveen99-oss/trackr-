"""Statistics on already-produced returns; never computes accounting flows."""
from decimal import Decimal as D
from .core import Metric, ZERO, ONE, precision, decimal


def mean(xs):
    return sum(xs, ZERO)/D(len(xs))


def variance(xs):
    m = mean(xs)
    return sum(((x-m)**2 for x in xs),ZERO)/D(len(xs)-1)


@precision
def quantile(xs, p):
    if not xs or not ZERO <= p <= ONE:
        raise ValueError('INVALID_QUANTILE')
    a=sorted(xs)
    pos=D(len(a)-1)*p
    lo=int(pos)
    hi=min(lo+1,len(a)-1)
    return a[lo]+(pos-D(lo))*(a[hi]-a[lo])


@precision
def interval_returns(levels, sessions):
    """Never collapse missing sessions before computing returns."""
    if sessions != sorted(set(sessions)):
        raise ValueError('INVALID_CALENDAR')
    if set(levels)-set(sessions):
        raise ValueError('UNKNOWN_SESSION')
    out=[]
    for start,end in zip(sessions,sessions[1:]):
        if start in levels and end in levels:
            a,b=decimal(levels[start]),decimal(levels[end])
            if a<=0 or b<=0:
                raise ValueError('NONPOSITIVE_LEVEL')
            out.append({'start':start,'end':end,'return':b/a-ONE})
    return out


@precision
def correlation(a,b):
    if len(a)!=len(b) or len(a)<30:
        return Metric.unavailable('INSUFFICIENT_PAIRED_HISTORY',method='pearson_sample_v1')
    sa,sb=variance(a).sqrt(),variance(b).sqrt()
    if sa==0 or sb==0:
        return Metric.unavailable('UNDEFINED_DENOMINATOR',method='pearson_sample_v1')
    ma,mb=mean(a),mean(b)
    cov=sum(((x-ma)*(y-mb) for x,y in zip(a,b)),ZERO)/D(len(a)-1)
    # Only final rounding noise can exceed the mathematical bounds.
    value=max(-ONE,min(ONE,cov/(sa*sb)))
    return Metric(value,method='pearson_sample_v1',coverage={'eligible_count':len(a)})


@precision
def risk(returns, *, rf=None, target=None, benchmark=None, elapsed_days=None,
         portfolio_value=None, annualise_allowed=None):
    """Kernel. Caller validates interval/calendar continuity and basis first.

    rf and target are matched interval RETURNS, never unlabeled annual yields.
    Minimum samples are design thresholds, not statistical confidence guarantees.
    """
    r=[decimal(x) for x in returns]
    if any(x < -ONE for x in r):
        raise ValueError('RETURN_BELOW_MINUS_ONE')
    keys=['cum_return','cagr','volatility','sharpe','sortino','beta','tracking_error',
          'information_ratio','var_1d_95','var_1d_99','cvar_1d_95','max_drawdown',
          'growth_100k','var_1d_95_value']
    out={k:Metric.unavailable('INSUFFICIENT_HISTORY',method=k+'_v1',
                             unit='INR' if k in ('growth_100k','var_1d_95_value') else 'ratio') for k in keys}
    if not r:
        return out
    n=len(r)
    growth=peak=ONE
    dd=ZERO
    for x in r:
        growth*=ONE+x
        peak=max(peak,growth)
        dd=min(dd,growth/peak-ONE)
    out['cum_return']=Metric(growth-ONE,method='linked_returns_v1')
    out['growth_100k']=Metric(D('100000')*growth,unit='INR',method='linked_returns_v1')
    out['max_drawdown']=Metric(dd,method='running_peak_v1')
    can_annualise=annualise_allowed if annualise_allowed is not None else (elapsed_days is not None and elapsed_days>=365)
    if can_annualise and elapsed_days and elapsed_days>0:
        if growth>0:
            out['cagr']=Metric(growth**(D(365)/D(elapsed_days))-ONE,method='actual_days_365_v1')
        else:
            out['cagr']=Metric.unavailable('NONPOSITIVE_GROWTH',method='actual_days_365_v1')
    else:
        out['cagr']=Metric.unavailable('SUBYEAR_OR_UNKNOWN_PERIOD',method='actual_days_365_v1')
    root=D(252).sqrt()
    if n>=30:
        out['volatility']=Metric(variance(r).sqrt()*root,method='sample_sd_252_v1')
    for key,base,missing in [('sharpe',rf,'MISSING_RISK_FREE_SERIES'),('sortino',target,'MISSING_TARGET_SERIES')]:
        method='differential_sample_252_v1' if key=='sharpe' else 'target_rms_all_n_252_v1'
        if base is None:
            out[key]=Metric.unavailable(missing,method=method)
        elif len(base)!=n:
            out[key]=Metric.unavailable('INTERVAL_MISMATCH',method=method)
        elif n>=30:
            excess=[x-decimal(y) for x,y in zip(r,base)]
            denom=variance(excess).sqrt() if key=='sharpe' else (sum((min(ZERO,x)**2 for x in excess),ZERO)/D(n)).sqrt()
            out[key]=Metric(mean(excess)/denom*root,method=method) if denom else Metric.unavailable('UNDEFINED_DENOMINATOR',method=method)
    if benchmark is not None and len(benchmark)==n and n>=30:
        b=[decimal(x) for x in benchmark]
        vb=variance(b)
        if vb:
            mr,mb=mean(r),mean(b)
            cov=sum(((x-mr)*(y-mb) for x,y in zip(r,b)),ZERO)/D(n-1)
            out['beta']=Metric(cov/vb,method='sample_covariance_beta_v1')
        else:
            out['beta']=Metric.unavailable('UNDEFINED_DENOMINATOR',method='sample_covariance_beta_v1')
        active=[x-y for x,y in zip(r,b)]
        te=variance(active).sqrt()*root
        out['tracking_error']=Metric(te,method='active_sample_sd_252_v1')
        out['information_ratio']=Metric(mean(active)*D(252)/te,method='active_mean_252_over_te_v1') if te else Metric.unavailable('UNDEFINED_DENOMINATOR',method='active_mean_252_over_te_v1')
    else:
        for k in ('beta','tracking_error','information_ratio'):
            out[k]=Metric.unavailable('MISSING_OR_INSUFFICIENT_PAIRED_BENCHMARK',method=k+'_v1')
    if n>=252:
        for p,key in [(D('.05'),'var_1d_95'),(D('.01'),'var_1d_99')]:
            out[key]=Metric(max(ZERO,-quantile(r,p)),method='historical_r7_loss_floor_v1')
        threshold=quantile(r,D('.05'))
        out['cvar_1d_95']=Metric(max(ZERO,-mean([x for x in r if x<=threshold])),method='threshold_tail_including_ties_v1')
        if portfolio_value is not None:
            value=decimal(portfolio_value)
            if value<0:
                raise ValueError('NEGATIVE_PORTFOLIO_VALUE')
            out['var_1d_95_value']=Metric(value*out['var_1d_95'].value,unit='INR',method='value_times_historical_var_v1')
        else:
            out['var_1d_95_value']=Metric.unavailable('MISSING_PORTFOLIO_VALUE',unit='INR',method='value_times_historical_var_v1')
    for m in out.values():
        m.coverage={'eligible_count':n,'total_count':n}
    return out
