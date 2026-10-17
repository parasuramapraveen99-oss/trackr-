"""Validate and convey engine-owned KPIs without recomputing their accounting."""
from datetime import date
from .core import Metric, decimal

UNITS = {'xirr': 'fraction', 'realised_pnl': 'INR', 'net_contributions': 'INR', 'dividends_received': 'INR'}


def kpis(records, data, sources):
    out = {name: Metric.unavailable('MISSING_UPSTREAM_KPI', unit=unit) for name, unit in UNITS.items()}
    seen = set()
    errors = []
    for record in records:
        name = record.get('name')
        if name not in UNITS:
            errors.append('UNSUPPORTED_UPSTREAM_KPI:'+str(name))
            continue
        try:
            if name in seen:
                raise ValueError('DUPLICATE_UPSTREAM_KPI')
            seen.add(name)
            for key in ('ledger_revision', 'engine_version', 'currency', 'as_of'):
                if record.get(key) != data[key]:
                    raise ValueError('UPSTREAM_KPI_'+key.upper()+'_MISMATCH')
            if record.get('unit') != UNITS[name] or not record.get('basis'):
                raise ValueError('UPSTREAM_KPI_UNIT_OR_BASIS_MISSING')
            refs = record.get('source_refs', [])
            if not refs or any(ref not in sources for ref in refs):
                raise ValueError('MISSING_SOURCE_REFERENCE')
            period = record['period']
            if not date.fromisoformat(period['start']) <= date.fromisoformat(period['end']) <= date.fromisoformat(data['as_of']):
                raise ValueError('INVALID_UPSTREAM_KPI_PERIOD')
            status = record['status']
            value = decimal(record['value']) if record.get('value') is not None else None
            out[name] = Metric(value, status=status, reasons=record.get('reasons', []), unit=UNITS[name],
                               period=period.copy(), source_refs=list(refs), method='upstream:'+record['basis'])
        except (ValueError, KeyError, TypeError) as exc:
            out[name] = Metric.unavailable(str(exc), unit=UNITS[name], method='upstream_passthrough_v1')
    return {'metrics': out, 'errors': errors}
