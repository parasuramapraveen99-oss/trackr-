"""Decimal boundaries and explicit, serialisable result quality."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP
from functools import wraps
from typing import Any

ZERO = Decimal('0')
ONE = Decimal('1')


def precision(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec = 34
            return fn(*args, **kwargs)
    return wrapped


def decimal(value):
    if not isinstance(value, (str, Decimal)):
        raise ValueError('DECIMAL_STRING_REQUIRED')
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('INVALID_DECIMAL') from exc
    if not d.is_finite():
        raise ValueError('NONFINITE_INPUT')
    return d


def encode(value):
    if isinstance(value, Metric):
        return value.json()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    return value


@dataclass
class Metric:
    value: Any = None
    status: str = 'available'
    reasons: list[str] = field(default_factory=list)
    unit: str = 'ratio'
    method: str = 'independent_v1'
    coverage: dict = field(default_factory=dict)
    period: dict = field(default_factory=dict)
    source_refs: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.status not in ('available','provisional','unavailable'):
            raise ValueError('INVALID_STATUS')
        if self.status == 'unavailable' and (self.value is not None or not self.reasons):
            raise ValueError('UNAVAILABLE_REQUIRES_NULL_AND_REASON')
        if self.status != 'unavailable' and self.value is None:
            raise ValueError('NULL_REQUIRES_UNAVAILABLE')
        if self.status == 'provisional' and not self.reasons:
            raise ValueError('PROVISIONAL_REQUIRES_REASON')

    @classmethod
    def unavailable(cls, reason, **kwargs):
        return cls(None, 'unavailable', [reason], **kwargs)

    def caveat(self, reason):
        if self.status != 'unavailable':
            self.status = 'provisional'
        if reason not in self.reasons:
            self.reasons.append(reason)
        return self

    @precision
    def json(self):
        value = self.value
        if isinstance(value, Decimal) and self.unit == 'INR':
            value = format(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')
        return {'value':encode(value),'status':self.status,'reasons':self.reasons,
                'unit':self.unit,'method':self.method,'coverage':encode(self.coverage),
                'period':self.period,'source_refs':self.source_refs}
