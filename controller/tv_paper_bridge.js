async (account, action, args = {}) => {
  if (!['tradingview.com', 'www.tradingview.com', 'cn.tradingview.com'].includes(location.hostname)
      || !location.pathname.startsWith('/chart/')) throw new Error('不是 TradingView 图表');
  const key = '__autotradePaperBridge';
  if (action === 'detach') {
    window[key]?.dispose();
    delete window[key];
    return true;
  }
  const activeBroker = () => window.TradingViewApi?.trading?.()?.activeBroker?.();
  const broker = activeBroker();
  const value = x => typeof x?.value === 'function' ? x.value() : x;
  function check() {
    if (!broker || activeBroker() !== broker || broker.metainfo()?.id !== 'Paper'
        || String(value(broker.currentAccount?.())) !== account
        || ![undefined, null, 'demo'].includes(value(broker.currentAccountType?.()))
        || (broker.isConnected?.() !== true && value(broker.connectionStatus?.()) !== 1)) {
      throw new Error('TV 模拟账户未连接或账户不匹配');
    }
  }
  check();
  const clean = (rows, fields) => rows.map(row =>
    Object.fromEntries(fields.filter(field => row[field] != null).map(field => [field, row[field]])));
  if (action === 'snapshot') {
    const [positions, orders, history] = await Promise.all([
      broker.positions(), broker.orders(), args.history ? broker.ordersHistory() : [],
    ]);
    check();
    const orderFields = ['id', 'parentId', 'parentType', 'symbol', 'side', 'qty', 'status', 'type', 'avgPrice', 'stopPrice'];
    return {
      positions: clean(positions, ['id', 'symbol', 'side', 'qty', 'avgPrice', 'stopLoss', 'takeProfit']),
      orders: clean(orders, orderFields),
      history: clean(history, orderFields),
    };
  }
  if (action === 'attach') {
    if (window[key]?.broker === broker) return true;
    window[key]?.dispose();
    const subscriptions = [];
    const owner = {};
    const callback = () => window.__autotradePaperChanged('changed');
    const dispose = () => subscriptions.forEach(event => event.unsubscribe(owner, callback));
    try {
      for (const name of ['orderUpdate', 'positionUpdate', 'executionUpdate', 'ordersFullUpdate',
        'positionsFullUpdate', 'currentAccountUpdate', 'connectionStatusUpdate']) {
        const event = broker[name];
        if (typeof event?.subscribe !== 'function') {
          if (['orderUpdate', 'positionUpdate', 'executionUpdate'].includes(name)) throw new Error('TV 事件接口不可用');
          continue;
        }
        event.subscribe(owner, callback);
        subscriptions.push(event);
      }
      window[key] = { broker, dispose };
      return true;
    } catch (error) {
      dispose();
      throw error;
    }
  }

  const { symbol, side, qty, previousQty, previousStop, positionId, stop } = args;
  if (!['open', 'reduce', 'stop'].includes(action) || !/^[A-Z0-9_.-]+:[A-Z0-9_.!-]+$/.test(symbol)
      || ![1, -1].includes(side) || !Number.isInteger(previousQty) || previousQty < 0
      || (action !== 'stop' && (!Number.isInteger(qty) || qty <= 0))
      || (action !== 'reduce' && (!Number.isFinite(stop) || stop <= 0))) throw new Error('模拟操作参数无效');
  const [positions, orders] = await Promise.all([broker.positions(), broker.orders()]);
  check();
  const pos = positions.find(row => row.symbol === symbol && row.qty > 0);
  const working = orders.filter(row => row.symbol === symbol && [3, 4, 6].includes(row.status));
  const protection = working.filter(row => row.type === 3 && row.parentId === pos?.id && row.parentType === 2);
  if ((pos?.qty ?? 0) !== previousQty || (pos && (pos.id !== positionId || pos.side !== side
      || (pos.stopLoss ?? null) !== previousStop)) || working.length !== protection.length) {
    throw new Error('TV 模拟仓位或保护单已变化');
  }
  if (action === 'open') {
    if (pos || working.length) throw new Error('标的已有模拟仓位或挂单');
    const base = await broker.createInitialPreOrder({ symbol, side, type: 2 });
    check();
    const [freshPositions, freshOrders] = await Promise.all([broker.positions(), broker.orders()]);
    check();
    if (freshPositions.some(row => row.symbol === symbol && row.qty > 0)
        || freshOrders.some(row => row.symbol === symbol && [3, 4, 6].includes(row.status))) {
      throw new Error('提交前 TV 模拟仓位或挂单已变化');
    }
    return await broker.placeOrder({ ...base, symbol, side, type: 2, qty, stopLoss: stop });
  }
  if (action === 'reduce') {
    if (!pos || qty <= 0 || qty > pos.qty) throw new Error('模拟减仓数量无效');
    return await broker.closePosition(pos.id, qty === pos.qty ? undefined : qty);
  }
  if (action === 'stop') {
    if (!pos || protection.length !== 1 || String(protection[0].id) !== args.stopId
        || protection[0].qty !== pos.qty || protection[0].stopPrice !== previousStop) {
      throw new Error('模拟止损单身份不匹配');
    }
    return await broker.editPositionBrackets(pos.id, { stopLoss: stop, takeProfit: pos.takeProfit });
  }
  throw new Error('未知 TV 模拟操作');
}
