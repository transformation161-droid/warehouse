"""Cost arithmetic is NOT a provider billing cap. Only confirmed live prices may be approved."""
from decimal import Decimal


def estimate(hourly_rate='0.68',hours=720,extras='0',replicas=1):
    rate=Decimal(hourly_rate);extra=Decimal(extras)
    if not rate.is_finite() or not extra.is_finite() or rate<0 or extra<0 or hours<1 or hours>720 or replicas!=1:
        raise ValueError('One replica and at most 720 hours are permitted')
    result=(rate*hours+extra).quantize(Decimal('.01'))
    if result>Decimal(500):raise ValueError('Confirmed cost exceeds 500 RUB; separate approval required')
    return result
