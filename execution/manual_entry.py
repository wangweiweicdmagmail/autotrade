"""Manual VWAP limit pricing from confirmed indicator facts; no indicator calculation."""
from domain.contracts import finite_number


def vwap_limit_price(side, level, vwap, sigma):
    if side not in ('long', 'short') or level not in ('vwap', 'upper' if side == 'long' else 'lower'):
        raise ValueError('做多仅可选VWAP或上轨，做空仅可选VWAP或下轨')
    mid, deviation = finite_number(vwap), finite_number(sigma)
    if mid is None or mid <= 0:
        raise ValueError('确认VWAP中线未就绪')
    if level != 'vwap' and (deviation is None or deviation <= 0):
        raise ValueError('确认VWAP标准差未就绪，不能计算外轨限价')
    price = round(mid + (deviation if level == 'upper' else -deviation if level == 'lower' else 0), 2)
    if price <= 0:
        raise ValueError('VWAP限价取整后无效')
    return price
