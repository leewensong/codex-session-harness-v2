export const effortLabels = { none: '无', minimal: '最少', low: '低', medium: '中', high: '高', xhigh: '更高', max: '最高', ultra: '超高' };
export function effortsFor(catalog, model) {
  const known = (catalog?.models || []).find(m => m.model === model);
  return known && Array.isArray(known.supportedReasoningEfforts) ? known.supportedReasoningEfforts : catalog?.efforts || [];
}
export function describeConfiguration(configuration) {
  if (!configuration) return '正在读取模型设置…';
  const observed = configuration.observed || {}, overrides = configuration.overrides || {};
  const actual = `最近一轮：${observed.model || '模型未记录'} · ${observed.reasoningEffort || '思考强度未知'}`;
  if (!overrides.model && !overrides.reasoningEffort) return actual;
  const effective = configuration.effective || {};
  return `${actual}；下一轮：${effective.model || '默认'} · ${effective.reasoningEffort || '默认'}`;
}
