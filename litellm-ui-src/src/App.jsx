import React, { useState, useEffect } from 'react';
import axios from 'axios';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  ResponsiveContainer,
} from 'recharts';
import { BarChart2, KeyRound, Database, Plus, Pencil, Trash2, X, Layers, Building2, Search, RefreshCw, Tag, AlertTriangle } from 'lucide-react';

const API_KEY = import.meta.env.VITE_LITELLM_API_KEY || 'dev-only-change-me';
const API_BASE = '/api/litellm';
const headers = { 'x-api-key': API_KEY };

function formatNumber(n) {
  return n != null ? n.toLocaleString() : '0';
}

/* ───── 通用组件 ───── */

function DateRangePicker({ start, end, onStartChange, onEndChange }) {
  return (
    <div className="flex items-center gap-3">
      <div>
        <label className="block text-xs text-gray-500 mb-1">开始日期</label>
        <input
          type="date"
          value={start}
          onChange={(e) => onStartChange(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none"
        />
      </div>
      <span className="text-gray-400 mt-5">—</span>
      <div>
        <label className="block text-xs text-gray-500 mb-1">结束日期</label>
        <input
          type="date"
          value={end}
          onChange={(e) => onEndChange(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none"
        />
      </div>
    </div>
  );
}

function SearchableSelect({ label, value, onChange, options, placeholder }) {
  const [search, setSearch] = useState('');
  const [open, setOpen] = useState(false);
  const filtered = options.filter((o) => o.toLowerCase().includes(search.toLowerCase()));
  return (
    <div className="relative">
      <label className="block text-xs text-gray-500 mb-1">{label}</label>
      <div
        className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-64 cursor-pointer flex items-center justify-between bg-white focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-blue-500"
        onClick={() => setOpen(!open)}
      >
        <span className={value ? 'text-gray-900' : 'text-gray-400'}>{value || placeholder}</span>
        <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </div>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute z-20 mt-1 w-64 bg-white border border-gray-200 rounded-lg shadow-lg max-h-64 overflow-hidden">
            <div className="p-2 border-b border-gray-100">
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜索..."
                className="w-full border border-gray-200 rounded px-2 py-1.5 text-sm outline-none focus:border-blue-400"
                autoFocus
                onClick={(e) => e.stopPropagation()}
              />
            </div>
            <div className="overflow-y-auto max-h-48">
              <div
                className="px-3 py-2 text-sm text-gray-400 hover:bg-gray-50 cursor-pointer"
                onClick={() => { onChange(''); setOpen(false); setSearch(''); }}
              >
                {placeholder}
              </div>
              {filtered.map((opt) => (
                <div
                  key={opt}
                  className={`px-3 py-2 text-sm cursor-pointer hover:bg-blue-50 ${opt === value ? 'bg-blue-50 text-blue-700 font-medium' : 'text-gray-700'}`}
                  onClick={() => { onChange(opt); setOpen(false); setSearch(''); }}
                >
                  {opt}
                </div>
              ))}
              {filtered.length === 0 && <div className="px-3 py-2 text-sm text-gray-400">无匹配结果</div>}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function SummaryCards({ data }) {
  if (!data) return null;
  const totalUsd = data.cost_usd ?? data.total_cost_usd ?? 0;
  const totalCny = data.cost_cny ?? data.total_cost_cny ?? 0;
  const requestCount = data.request_count ?? data.total_request_count ?? 0;
  const rate = data.exchange_rate ?? 0;
  const cards = [
    { label: '总花费（美元）', value: `$${totalUsd.toFixed(4)}`, color: 'text-blue-700' },
    { label: '总花费（人民币）', value: `¥${totalCny.toFixed(4)}`, color: 'text-green-700' },
    { label: '请求数', value: formatNumber(requestCount), color: 'text-gray-900' },
    { label: '输入 Tokens', value: formatNumber(data.input_tokens ?? data.total_input_tokens), color: 'text-gray-900' },
    { label: '输出 Tokens', value: formatNumber(data.output_tokens ?? data.total_output_tokens), color: 'text-gray-900' },
    { label: '缓存 Tokens', value: formatNumber(data.cache_tokens ?? data.total_cache_tokens), color: 'text-gray-900' },
  ];
  return (
    <div>
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        {cards.map((c) => (
          <div key={c.label} className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
            <div className="text-xs text-gray-500 mb-1">{c.label}</div>
            <div className={`text-xl font-bold ${c.color}`}>{c.value}</div>
          </div>
        ))}
      </div>
      {rate > 0 && (
        <div className="text-xs text-gray-400 mt-2 text-right">
          实时汇率：1 USD = {rate.toFixed(4)} CNY
        </div>
      )}
    </div>
  );
}

function CurrencySummary({ records }) {
  if (!records || records.length === 0) return null;
  const byCurrency = new Map();
  records.forEach((r) => {
    const cur = r.currency || 'USD';
    if (!byCurrency.has(cur)) byCurrency.set(cur, { input: 0, output: 0, cache: 0, cost: 0 });
    const agg = byCurrency.get(cur);
    agg.input += r.input_tokens;
    agg.output += r.output_tokens;
    agg.cache += r.cache_tokens;
    agg.cost += r.cost;
  });
  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      {Array.from(byCurrency.entries()).map(([cur, agg]) => (
        <div key={cur} className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
          <div className="text-xs text-gray-500 mb-2">币种：{cur}</div>
          <div className="space-y-1.5 text-sm">
            <div className="flex justify-between">
              <span className="text-gray-500">总花费</span>
              <span className="font-semibold text-blue-700">{agg.cost.toFixed(6)} {cur}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">输入 Tokens</span>
              <span className="font-mono">{formatNumber(agg.input)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">输出 Tokens</span>
              <span className="font-mono">{formatNumber(agg.output)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">缓存 Tokens</span>
              <span className="font-mono">{formatNumber(agg.cache)}</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function RecordsTable({ records, columns }) {
  if (!records || records.length === 0) {
    return <div className="text-center text-gray-400 py-8">暂无数据</div>;
  }
  const sorted = [...records].sort((a, b) => (b.request_count ?? 0) - (a.request_count ?? 0));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-200 bg-gray-50">
            {columns.map((col) => (
              <th key={col.key} className={`px-4 py-3 font-medium text-gray-600 ${col.align === 'right' ? 'text-right' : 'text-left'}`}>
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, i) => (
            <tr key={i} className="border-b border-gray-100 hover:bg-blue-50/30 transition-colors">
              {columns.map((col) => (
                <td key={col.key} className={`px-4 py-3 ${col.align === 'right' ? 'text-right font-mono' : 'text-left'}`}>
                  {col.render ? col.render(row) : (row[col.key] ?? '-')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function getDefaultDates() {
  const now = new Date();
  const end = now.toISOString().slice(0, 10);
  const start = new Date(now.getTime() - 30 * 86400000).toISOString().slice(0, 10);
  return { start, end };
}

/* ───────────────────────────────────────────────────
   Tab 1: 按模型 / 供应商查询
   ─────────────────────────────────────────────────── */
function ModelProviderPanel({ modelOptions, providerOptions }) {
  const defaults = getDefaultDates();
  const [start, setStart] = useState(defaults.start);
  const [end, setEnd] = useState(defaults.end);
  const [model, setModel] = useState('');
  const [provider, setProvider] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [records, setRecords] = useState(null);

  const handleQuery = async () => {
    if (!start || !end) { setError('请选择日期范围'); return; }
    setLoading(true); setError(null); setResult(null); setRecords(null);
    try {
      const groupBy = ['model'];
      const hasModelFilter = !!model;
      if (provider) groupBy.push('provider');
      if (hasModelFilter) groupBy.push('date');
      const payload = { start: `${start}T00:00:00`, end: `${end}T23:59:59`, group_by: groupBy, date_granularity: 'day' };
      if (model) payload.model = model;
      if (provider) payload.provider = provider;
      const resp = await axios.post(`${API_BASE}/analytics/spend`, payload, { headers });
      const data = resp.data;
      setResult(data);
      setRecords(data.records || []);
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setLoading(false);
    }
  };

  const hasModelFilter = !!model;
  const columns = [];
  if (hasModelFilter) columns.push({ key: 'date', label: '日期' });
  columns.push({ key: 'model', label: '模型' });
  if (provider) columns.push({ key: 'provider', label: '供应商' });
  columns.push(
    { key: 'request_count', label: '请求数', align: 'right', render: (r) => formatNumber(r.request_count) },
    { key: 'input_tokens', label: '输入 Tokens', align: 'right', render: (r) => formatNumber(r.input_tokens) },
    { key: 'output_tokens', label: '输出 Tokens', align: 'right', render: (r) => formatNumber(r.output_tokens) },
    { key: 'cache_tokens', label: '缓存 Tokens', align: 'right', render: (r) => formatNumber(r.cache_tokens) },
    { key: 'cost_usd', label: '花费（USD）', align: 'right', render: (r) => `$${(r.cost_usd ?? 0).toFixed(6)}` },
    { key: 'cost_cny', label: '花费（CNY）', align: 'right', render: (r) => `¥${(r.cost_cny ?? 0).toFixed(4)}` },
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-4 items-end">
        <DateRangePicker start={start} end={end} onStartChange={setStart} onEndChange={setEnd} />
        <SearchableSelect label="模型" value={model} onChange={setModel} options={modelOptions} placeholder="全部模型" />
        <SearchableSelect label="供应商" value={provider} onChange={setProvider} options={providerOptions} placeholder="不限供应商" />
        <button
          onClick={handleQuery}
          disabled={loading}
          className="bg-blue-600 text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors h-[38px]"
        >
          {loading ? '查询中...' : '查询'}
        </button>
      </div>
      <p className="text-xs text-gray-400">
        {hasModelFilter
          ? (provider ? '当前按「模型 + 供应商 + 日期」分组展示' : '当前按「模型 + 日期」分组展示')
          : (provider ? '当前按「模型 + 供应商」汇总展示' : '当前按「模型」汇总展示')}
      </p>
      {error && <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg text-sm">{error}</div>}
      {result && (
        <>
          <SummaryCards data={result} />
        </>
      )}
      {records && records.length > 0 && (() => {
        const chartData = records.filter((r) => hasModelFilter ? true : (r.cost_usd || 0) > 0);
        return chartData.length === 0 ? null : (
          <>
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
              <div className="px-5 py-4 border-b border-gray-200">
                <h3 className="font-semibold text-gray-800">{hasModelFilter ? '按日期费用柱状图' : '按模型费用柱状图'}</h3>
              </div>
              <div className="px-5 py-4 h-72">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey={hasModelFilter ? 'date' : 'model'} tick={{ fontSize: 10 }} tickMargin={8} />
                    <YAxis tick={{ fontSize: 10 }} />
                    <Tooltip formatter={(v) => typeof v === 'number' ? `$${v.toFixed(4)}` : v} />
                    <Bar dataKey="cost_usd" name="花费 (USD)" fill="#2563eb" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
              <div className="px-5 py-4 border-b border-gray-200">
                <h3 className="font-semibold text-gray-800">明细（共 {records.length} 条）</h3>
              </div>
              <RecordsTable records={records} columns={columns} />
            </div>
          </>
        );
      })()}
    </div>
  );
}



/* ───────────────────────────────────────────────────
   按厂商汇总（模型按第一个 - 前分类）
   ─────────────────────────────────────────────────── */

const FAMILY_COLORS = {
  claude: '#8b5cf6', gpt: '#2563eb', gemini: '#059669', kimi: '#d97706',
  deepseek: '#0891b2', doubao: '#dc2626', qwen: '#7c3aed', glm: '#be185d',
  grok: '#4f46e5', other: '#6b7280',
};

function getModelFamily(modelName) {
  if (!modelName) return 'other';
  const s = modelName.trim();
  if (s.startsWith('ep-')) return 'doubao';
  const first = s.split('-')[0].toLowerCase();
  return first || 'other';
}

function FamilySummaryPanel() {
  const defaults = getDefaultDates();
  const [start, setStart] = useState(defaults.start);
  const [end, setEnd] = useState(defaults.end);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [familyData, setFamilyData] = useState(null);

  const handleQuery = async () => {
    if (!start || !end) { setError('请选择日期范围'); return; }
    setLoading(true); setError(null); setFamilyData(null);
    try {
      const payload = { start: `${start}T00:00:00`, end: `${end}T23:59:59`, group_by: ['model'], date_granularity: 'day' };
      const resp = await axios.post(`${API_BASE}/analytics/spend`, payload, { headers });
      const records = resp.data.records || [];

      // 按模型名聚合
      const map = {};
      records.forEach((r) => {
        const key = r.model || 'unknown';
        if (!map[key]) map[key] = { model: key, requests: 0, input: 0, output: 0, cache: 0, cost_usd: 0, cost_cny: 0 };
        const d = map[key];
        d.requests += r.request_count ?? 0;
        d.input += r.input_tokens ?? 0;
        d.output += r.output_tokens ?? 0;
        d.cache += r.cache_tokens ?? 0;
        d.cost_usd += r.cost_usd ?? 0;
        d.cost_cny += r.cost_cny ?? 0;
      });

      const models = Object.values(map)
        .sort((a, b) => b.cost_cny - a.cost_cny);

      const totalCny = models.reduce((s, f) => s + f.cost_cny, 0);
      models.forEach((f) => { f.pct = totalCny > 0 ? (f.cost_cny / totalCny * 100) : 0; });

      setFamilyData({ families: models, totalCny, totalUsd: models.reduce((s, f) => s + f.cost_usd, 0), totalReq: models.reduce((s, f) => s + f.requests, 0), exchange_rate: resp.data.exchange_rate });
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-4 items-end">
        <DateRangePicker start={start} end={end} onStartChange={setStart} onEndChange={setEnd} />
        <button onClick={handleQuery} disabled={loading}
          className="bg-blue-600 text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors h-[38px]">
          {loading ? '查询中...' : '查询'}
        </button>
      </div>
      {error && <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg text-sm">{error}</div>}
      {familyData && (
        <>
          {/* 汇总卡片 */}
          <div className="grid gap-4 md:grid-cols-4">
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">总请求数</div>
              <div className="text-2xl font-bold text-gray-900 mt-1">{formatNumber(familyData.totalReq)}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">总花费 (USD)</div>
              <div className="text-2xl font-bold text-blue-700 mt-1">${familyData.totalUsd.toFixed(2)}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">总花费 (CNY)</div>
              <div className="text-2xl font-bold text-red-600 mt-1">¥{familyData.totalCny.toFixed(2)}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">模型数量</div>
              <div className="text-2xl font-bold text-gray-900 mt-1">{familyData.families.length}</div>
            </div>
          </div>

          {/* 柱状图 */}
          {(() => {
            const chartData = familyData.families.filter((f) => f.cost_cny > 0);
            return chartData.length > 0 ? (
              <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                <div className="px-5 py-4 border-b border-gray-200">
                  <h3 className="font-semibold text-gray-800">按模型费用（CNY）</h3>
                </div>
                <div className="px-5 py-4 h-72">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={chartData}>
                      <CartesianGrid strokeDasharray="3 3" vertical={false} />
                      <XAxis dataKey="model" tick={{ fontSize: 10 }} tickMargin={8} angle={-30} textAnchor="end" height={60} />
                      <YAxis tick={{ fontSize: 10 }} />
                      <Tooltip formatter={(v) => typeof v === 'number' ? `¥${v.toFixed(2)}` : v} />
                      <Bar dataKey="cost_cny" name="花费 (CNY)" fill="#2563eb" radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            ) : null;
          })()}

          {/* 明细表 */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <div className="px-5 py-4 border-b border-gray-200">
              <h3 className="font-semibold text-gray-800">模型明细</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-200">
                    <th className="px-4 py-3 text-left font-medium text-gray-600">模型</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">请求数</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">输入 Tokens</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">输出 Tokens</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">花费 (USD)</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">花费 (CNY)</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">占比</th>
                  </tr>
                </thead>
                <tbody>
                  {familyData.families.map((f) => (
                    <tr key={f.model} className="border-b border-gray-100 hover:bg-gray-50">
                      <td className="px-4 py-3">
                        <span className="font-medium font-mono text-sm">{f.model}</span>
                      </td>
                      <td className="px-4 py-3 text-right font-mono">{formatNumber(f.requests)}</td>
                      <td className="px-4 py-3 text-right font-mono">{formatNumber(f.input)}</td>
                      <td className="px-4 py-3 text-right font-mono">{formatNumber(f.output)}</td>
                      <td className="px-4 py-3 text-right font-mono">${f.cost_usd.toFixed(2)}</td>
                      <td className="px-4 py-3 text-right font-mono">¥{f.cost_cny.toFixed(2)}</td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <div className="w-16 bg-gray-200 rounded-full h-1.5">
                            <div className="h-1.5 rounded-full bg-blue-500" style={{ width: `${Math.min(f.pct, 100)}%` }} />
                          </div>
                          <span className="text-xs text-gray-500 w-12 text-right">{f.pct.toFixed(1)}%</span>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ───────────────────────────────────────────────────
   按供应商汇总
   ─────────────────────────────────────────────────── */
function ProviderSummaryPanel() {
  const defaults = getDefaultDates();
  const [start, setStart] = useState(defaults.start);
  const [end, setEnd] = useState(defaults.end);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null);

  const handleQuery = async () => {
    if (!start || !end) { setError('请选择日期范围'); return; }
    setLoading(true); setError(null); setData(null);
    try {
      const payload = { start: `${start}T00:00:00`, end: `${end}T23:59:59`, group_by: ['provider'], date_granularity: 'day' };
      const resp = await axios.post(`${API_BASE}/analytics/spend`, payload, { headers });
      const records = resp.data.records || [];

      const map = {};
      records.forEach((r) => {
        const key = r.provider || 'unknown';
        if (!map[key]) map[key] = { provider: key, requests: 0, input: 0, output: 0, cache: 0, cost_usd: 0, cost_cny: 0 };
        const d = map[key];
        d.requests += r.request_count ?? 0;
        d.input += r.input_tokens ?? 0;
        d.output += r.output_tokens ?? 0;
        d.cache += r.cache_tokens ?? 0;
        d.cost_usd += r.cost_usd ?? 0;
        d.cost_cny += r.cost_cny ?? 0;
      });

      const providers = Object.values(map).sort((a, b) => b.cost_cny - a.cost_cny);
      const totalCny = providers.reduce((s, p) => s + p.cost_cny, 0);
      providers.forEach((p) => { p.pct = totalCny > 0 ? (p.cost_cny / totalCny * 100) : 0; });

      setData({ providers, totalCny, totalUsd: providers.reduce((s, p) => s + p.cost_usd, 0), totalReq: providers.reduce((s, p) => s + p.requests, 0), exchange_rate: resp.data.exchange_rate });
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-4 items-end">
        <DateRangePicker start={start} end={end} onStartChange={setStart} onEndChange={setEnd} />
        <button onClick={handleQuery} disabled={loading}
          className="bg-blue-600 text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors h-[38px]">
          {loading ? '查询中...' : '查询'}
        </button>
      </div>
      {error && <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg text-sm">{error}</div>}
      {data && (
        <>
          <div className="grid gap-4 md:grid-cols-4">
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">总请求数</div>
              <div className="text-2xl font-bold text-gray-900 mt-1">{formatNumber(data.totalReq)}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">总花费 (USD)</div>
              <div className="text-2xl font-bold text-blue-700 mt-1">${data.totalUsd.toFixed(2)}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">总花费 (CNY)</div>
              <div className="text-2xl font-bold text-red-600 mt-1">¥{data.totalCny.toFixed(2)}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">供应商数量</div>
              <div className="text-2xl font-bold text-gray-900 mt-1">{data.providers.length}</div>
            </div>
          </div>

          {(() => {
            const chartData = data.providers.filter((p) => p.cost_cny > 0);
            return chartData.length > 0 ? (
              <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                <div className="px-5 py-4 border-b border-gray-200">
                  <h3 className="font-semibold text-gray-800">按供应商费用（CNY）</h3>
                </div>
                <div className="px-5 py-4 h-72">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={chartData}>
                      <CartesianGrid strokeDasharray="3 3" vertical={false} />
                      <XAxis dataKey="provider" tick={{ fontSize: 11 }} tickMargin={8} />
                      <YAxis tick={{ fontSize: 10 }} />
                      <Tooltip formatter={(v) => typeof v === 'number' ? `¥${v.toFixed(2)}` : v} />
                      <Bar dataKey="cost_cny" name="花费 (CNY)" fill="#7c3aed" radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            ) : null;
          })()}

          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <div className="px-5 py-4 border-b border-gray-200">
              <h3 className="font-semibold text-gray-800">供应商明细</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-200">
                    <th className="px-4 py-3 text-left font-medium text-gray-600">供应商</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">请求数</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">输入 Tokens</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">输出 Tokens</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">花费 (USD)</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">花费 (CNY)</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-600">占比</th>
                  </tr>
                </thead>
                <tbody>
                  {data.providers.map((p) => (
                    <tr key={p.provider} className="border-b border-gray-100 hover:bg-gray-50">
                      <td className="px-4 py-3">
                        <span className="font-medium text-sm">{p.provider}</span>
                      </td>
                      <td className="px-4 py-3 text-right font-mono">{formatNumber(p.requests)}</td>
                      <td className="px-4 py-3 text-right font-mono">{formatNumber(p.input)}</td>
                      <td className="px-4 py-3 text-right font-mono">{formatNumber(p.output)}</td>
                      <td className="px-4 py-3 text-right font-mono">${p.cost_usd.toFixed(2)}</td>
                      <td className="px-4 py-3 text-right font-mono">¥{p.cost_cny.toFixed(2)}</td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <div className="w-16 bg-gray-200 rounded-full h-1.5">
                            <div className="h-1.5 rounded-full bg-violet-500" style={{ width: `${Math.min(p.pct, 100)}%` }} />
                          </div>
                          <span className="text-xs text-gray-500 w-12 text-right">{p.pct.toFixed(1)}%</span>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ───────────────────────────────────────────────────
   Tab 2: 按 API Key 查询
   ─────────────────────────────────────────────────── */
function ApiKeyPanel() {
  const defaults = getDefaultDates();
  const [start, setStart] = useState(defaults.start);
  const [end, setEnd] = useState(defaults.end);
  const [apiKey, setApiKey] = useState('');
  const [apiKeyOptions, setApiKeyOptions] = useState([]);
  const [loadingKeys, setLoadingKeys] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [records, setRecords] = useState(null);

  // 拉 LiteLLM_VerificationToken 列表 (alias 优先 → spend 倒序)
  useEffect(() => {
    setLoadingKeys(true);
    axios.get(`${API_BASE}/analytics/api-keys`, { headers })
      .then((r) => setApiKeyOptions(r.data || []))
      .catch(() => setApiKeyOptions([]))
      .finally(() => setLoadingKeys(false));
  }, []);

  const handleQuery = async () => {
    if (!start || !end) { setError('请选择日期范围'); return; }
    if (!apiKey) { setError('请选择 API Key'); return; }
    setLoading(true); setError(null); setResult(null); setRecords(null);
    try {
      const payload = { start: `${start}T00:00:00`, end: `${end}T23:59:59`, group_by: ['api_key', 'model', 'date'], date_granularity: 'day' };
      const resp = await axios.post(`${API_BASE}/analytics/spend`, payload, { headers });
      const data = resp.data;
      // 选中后精确匹配 token (不再模糊)
      const filtered = (data.records || []).filter((r) => r.api_key === apiKey);
      setResult({
        cost_usd: filtered.reduce((s, r) => s + (r.cost_usd ?? 0), 0),
        cost_cny: filtered.reduce((s, r) => s + (r.cost_cny ?? 0), 0),
        input_tokens: filtered.reduce((s, r) => s + r.input_tokens, 0),
        output_tokens: filtered.reduce((s, r) => s + r.output_tokens, 0),
        cache_tokens: filtered.reduce((s, r) => s + r.cache_tokens, 0),
        request_count: filtered.reduce((s, r) => s + (r.request_count ?? 0), 0),
        exchange_rate: data.exchange_rate ?? 0,
      });
      setRecords(filtered);
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setLoading(false);
    }
  };

  const columns = [
    { key: 'date', label: '日期' },
    { key: 'model', label: '模型' },
    { key: 'api_key', label: 'API Key', render: (r) => <span className="font-mono text-xs">{r.api_key || '-'}</span> },
    { key: 'request_count', label: '请求数', align: 'right', render: (r) => formatNumber(r.request_count) },
    { key: 'input_tokens', label: '输入 Tokens', align: 'right', render: (r) => formatNumber(r.input_tokens) },
    { key: 'output_tokens', label: '输出 Tokens', align: 'right', render: (r) => formatNumber(r.output_tokens) },
    { key: 'cache_tokens', label: '缓存 Tokens', align: 'right', render: (r) => formatNumber(r.cache_tokens) },
    { key: 'cost_usd', label: '花费（USD）', align: 'right', render: (r) => `$${(r.cost_usd ?? 0).toFixed(6)}` },
    { key: 'cost_cny', label: '花费（CNY）', align: 'right', render: (r) => `¥${(r.cost_cny ?? 0).toFixed(4)}` },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-4 items-end">
        <DateRangePicker start={start} end={end} onStartChange={setStart} onEndChange={setEnd} />
        <div>
          <label className="block text-xs text-gray-500 mb-1">API Key</label>
          <select
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            disabled={loadingKeys}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-80 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none bg-white h-[38px]"
          >
            <option value="">{loadingKeys ? '加载中...' : `选择 API Key (共 ${apiKeyOptions.length} 个)`}</option>
            {apiKeyOptions.map((k) => (
              <option key={k.token} value={k.token}>
                {k.label}{k.blocked ? ' [封禁]' : ''}
              </option>
            ))}
          </select>
        </div>
        <button
          onClick={handleQuery}
          disabled={loading}
          className="bg-blue-600 text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors h-[38px]"
        >
          {loading ? '查询中...' : '查询'}
        </button>
      </div>
      {error && <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg text-sm">{error}</div>}
      {result && <SummaryCards data={result} />}
      {records && records.length > 0 && (
        <>
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <div className="px-5 py-4 border-b border-gray-200">
              <h3 className="font-semibold text-gray-800">按日期费用柱状图</h3>
            </div>
            <div className="px-5 py-4 h-72">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={records}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="date" tick={{ fontSize: 10 }} tickMargin={8} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip formatter={(v) => typeof v === 'number' ? `$${v.toFixed(4)}` : v} />
                  <Bar dataKey="cost_usd" name="花费 (USD)" fill="#16a34a" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <div className="px-5 py-4 border-b border-gray-200">
              <h3 className="font-semibold text-gray-800">明细（共 {records.length} 条）</h3>
            </div>
            <RecordsTable records={records} columns={columns} />
          </div>
        </>
      )}
    </div>
  );
}

/* ───────────────────────────────────────────────────
   模型定价管理
   ─────────────────────────────────────────────────── */

const PRICING_TYPE_OPTIONS = [
  { value: 'standard', label: '标准（输入/输出）' },
  { value: 'with_cache', label: '含缓存读写' },
  { value: 'with_thinking', label: '含思考 Token' },
  { value: 'combined', label: '缓存+思考组合' },
  { value: 'tiered', label: '简单阶梯' },
  { value: 'advanced', label: '高级定价' },
];


const KNOWN_PROVIDERS = ['openai', 'anthropic', 'google', 'deepseek', 'moonshot', 'ali', 'zhipu', 'volcengine', 'x-ai', 'road', 'blueshirt', 'nulls', 'openrouter', 'azure'];

// 国内模型前缀 → 供应商映射
const DOMESTIC_MODEL_PROVIDER_MAP = {
  kimi: 'moonshot', deepseek: 'deepseek', qwen: 'ali', glm: 'zhipu', doubao: 'volcengine',
};

/**
 * 从 model_group 解析出 provider 和 model_name。
 * 规则：
 *   1. model_group 以已知供应商前缀开头（如 "openai-gpt-4.1"）→ provider=openai, model=gpt-4.1
 *   2. model_group 以国内模型名开头（如 "deepseek-r1"）→ provider=deepseek, model=deepseek-r1（保留原名）
 *   3. 其他（国外模型）→ provider='blueshirt', model=model_group
 */
function parseModelGroup(modelGroup) {
  if (!modelGroup || !modelGroup.trim()) return { provider: '', modelName: '' };
  const s = modelGroup.trim();
  // Step 1: 检查已知供应商前缀
  if (s.includes('-')) {
    const firstSeg = s.split('-')[0].toLowerCase();
    if (KNOWN_PROVIDERS.includes(firstSeg)) {
      return { provider: firstSeg, modelName: s.substring(firstSeg.length + 1) };
    }
  }
  // Step 2: 检查国内模型名前缀
  const sLower = s.toLowerCase();
  for (const [prefix, prov] of Object.entries(DOMESTIC_MODEL_PROVIDER_MAP)) {
    if (sLower.startsWith(prefix)) {
      return { provider: prov, modelName: s };
    }
  }
  // Step 3: 国外模型（无前缀）→ blueshirt
  return { provider: 'blueshirt', modelName: s };
}

function parsePricingType(config) {
  const t = config?.type;
  if (['standard', 'with_cache', 'with_thinking', 'combined', 'tiered', 'advanced'].includes(t)) return t;
  return 'standard';
}

function initFieldsFromConfig(config) {
  return {
    input_per_1m: config?.input_per_1m ?? '',
    output_per_1m: config?.output_per_1m ?? '',
    cache_read_per_1m: config?.cache_read_per_1m ?? '',
    cache_write_per_1m: config?.cache_write_per_1m ?? '',
    thinking_per_1m: config?.thinking_per_1m ?? '',
  };
}

function mapTiersFromConfig(tiers) {
  return tiers?.map((t) => ({ up_to_k: t.up_to_k ?? '', price_per_1m: t.price_per_1m ?? '' })) || [{ up_to_k: '', price_per_1m: '' }];
}

function initAdvancedState(cfg) {
  if (cfg?.type !== 'advanced') {
    return {
      inputTiers: [{ up_to_k: '', price_per_1m: '' }],
      outputTiers: [{ up_to_k: '', price_per_1m: '' }],
      thinkingTiersEnabled: false,
      thinkingTiers: [{ up_to_k: '', price_per_1m: '' }],
      cacheReadTiersEnabled: false,
      cacheReadTiers: [{ up_to_k: '', price_per_1m: '' }],
      cacheWriteTiersEnabled: false,
      cacheWriteTiers: [{ up_to_k: '', price_per_1m: '' }],
      comboTiersEnabled: false,
      comboTiers: [{ input_up_to_k: '', output_up_to_k: '', input_price_per_1m: '', output_price_per_1m: '', cache_read_price_per_1m: '' }],
      cache_write_per_1m: '', cache_write_5min_per_1m: '', cache_write_1h_per_1m: '',
      cache_write_explicit_per_1m: '', cache_write_implicit_per_1m: '',
      cache_storage_per_1m: '', cache_storage_per_1m_per_hour: '',
      batch_discount: '',
    };
  }
  return {
    inputTiers: mapTiersFromConfig(cfg.input_tiers),
    outputTiers: mapTiersFromConfig(cfg.output_tiers),
    thinkingTiersEnabled: !!cfg.thinking_tiers,
    thinkingTiers: cfg.thinking_tiers ? mapTiersFromConfig(cfg.thinking_tiers) : [{ up_to_k: '', price_per_1m: '' }],
    cacheReadTiersEnabled: !!cfg.cache_read_tiers,
    cacheReadTiers: cfg.cache_read_tiers ? mapTiersFromConfig(cfg.cache_read_tiers) : [{ up_to_k: '', price_per_1m: '' }],
    cacheWriteTiersEnabled: !!cfg.cache_write_tiers,
    cacheWriteTiers: cfg.cache_write_tiers ? mapTiersFromConfig(cfg.cache_write_tiers) : [{ up_to_k: '', price_per_1m: '' }],
    comboTiersEnabled: !!cfg.combo_tiers,
    comboTiers: cfg.combo_tiers?.map((t) => ({
      input_up_to_k: t.input_up_to_k ?? '', output_up_to_k: t.output_up_to_k ?? '',
      input_price_per_1m: t.input_price_per_1m ?? '', output_price_per_1m: t.output_price_per_1m ?? '',
      cache_read_price_per_1m: t.cache_read_price_per_1m ?? '',
    })) || [{ input_up_to_k: '', output_up_to_k: '', input_price_per_1m: '', output_price_per_1m: '', cache_read_price_per_1m: '' }],
    cache_write_per_1m: cfg.cache_write_per_1m ?? '',
    cache_write_5min_per_1m: cfg.cache_write_5min_per_1m ?? '',
    cache_write_1h_per_1m: cfg.cache_write_1h_per_1m ?? '',
    cache_write_explicit_per_1m: cfg.cache_write_explicit_per_1m ?? '',
    cache_write_implicit_per_1m: cfg.cache_write_implicit_per_1m ?? '',
    cache_storage_per_1m: cfg.cache_storage_per_1m ?? '',
    cache_storage_per_1m_per_hour: cfg.cache_storage_per_1m_per_hour ?? '',
    batch_discount: cfg.batch_discount ?? '',
  };
}

function initTieredState(cfg) {
  if (cfg?.type !== 'tiered') return [{ up_to_tokens: '', input_per_1m: '', output_per_1m: '' }];
  return cfg.tiers.map((t) => ({ up_to_tokens: t.up_to_tokens ?? '', input_per_1m: t.input_per_1m ?? '', output_per_1m: t.output_per_1m ?? '' }));
}

function buildPricingConfig(pricingType, fields, currency, advState, tieredTiers) {
  const base = { currency };
  if (pricingType === 'standard') {
    const cfg = { ...base, type: 'standard', input_per_1m: +fields.input_per_1m, output_per_1m: +fields.output_per_1m };
    if (fields.cache_read_per_1m !== '') cfg.cache_read_per_1m = +fields.cache_read_per_1m;
    return cfg;
  }
  if (pricingType === 'with_cache') {
    return { ...base, type: 'with_cache', input_per_1m: +fields.input_per_1m, output_per_1m: +fields.output_per_1m, cache_write_per_1m: +fields.cache_write_per_1m, cache_read_per_1m: +fields.cache_read_per_1m };
  }
  if (pricingType === 'with_thinking') {
    const cfg = { ...base, type: 'with_thinking', input_per_1m: +fields.input_per_1m, output_per_1m: +fields.output_per_1m };
    if (fields.thinking_per_1m !== '') cfg.thinking_per_1m = +fields.thinking_per_1m;
    if (fields.cache_read_per_1m !== '') cfg.cache_read_per_1m = +fields.cache_read_per_1m;
    return cfg;
  }
  if (pricingType === 'combined') {
    const cfg = { ...base, type: 'combined', input_per_1m: +fields.input_per_1m, output_per_1m: +fields.output_per_1m };
    if (fields.cache_write_per_1m !== '') cfg.cache_write_per_1m = +fields.cache_write_per_1m;
    if (fields.cache_read_per_1m !== '') cfg.cache_read_per_1m = +fields.cache_read_per_1m;
    if (fields.thinking_per_1m !== '') cfg.thinking_per_1m = +fields.thinking_per_1m;
    return cfg;
  }
  if (pricingType === 'tiered') {
    return {
      type: 'tiered',
      tiers: tieredTiers.map((t) => ({
        up_to_tokens: t.up_to_tokens === '' || t.up_to_tokens === null ? null : +t.up_to_tokens,
        input_per_1m: +t.input_per_1m || 0,
        output_per_1m: +t.output_per_1m || 0,
      })),
    };
  }
  if (pricingType === 'advanced') {
    const parseTiers = (tiers) => tiers.map((t) => ({
      up_to_k: t.up_to_k === '' || t.up_to_k === null ? null : +t.up_to_k,
      price_per_1m: +t.price_per_1m || 0,
    }));
    const cfg = { ...base, type: 'advanced', input_tiers: parseTiers(advState.inputTiers), output_tiers: parseTiers(advState.outputTiers) };
    if (advState.thinkingTiersEnabled && advState.thinkingTiers.length > 0) cfg.thinking_tiers = parseTiers(advState.thinkingTiers);
    if (advState.cacheReadTiersEnabled && advState.cacheReadTiers.length > 0) cfg.cache_read_tiers = parseTiers(advState.cacheReadTiers);
    if (advState.cacheWriteTiersEnabled && advState.cacheWriteTiers.length > 0) cfg.cache_write_tiers = parseTiers(advState.cacheWriteTiers);
    if (advState.comboTiersEnabled && advState.comboTiers.length > 0) {
      cfg.combo_tiers = advState.comboTiers.map((t) => ({
        input_up_to_k: t.input_up_to_k === '' ? null : +t.input_up_to_k,
        output_up_to_k: t.output_up_to_k === '' ? null : +t.output_up_to_k,
        input_price_per_1m: +t.input_price_per_1m || 0,
        output_price_per_1m: +t.output_price_per_1m || 0,
        cache_read_price_per_1m: t.cache_read_price_per_1m === '' ? null : (+t.cache_read_price_per_1m || null),
      }));
    }
    if (advState.cache_write_per_1m !== '') cfg.cache_write_per_1m = +advState.cache_write_per_1m;
    if (advState.cache_write_5min_per_1m !== '') cfg.cache_write_5min_per_1m = +advState.cache_write_5min_per_1m;
    if (advState.cache_write_1h_per_1m !== '') cfg.cache_write_1h_per_1m = +advState.cache_write_1h_per_1m;
    if (advState.cache_write_explicit_per_1m !== '') cfg.cache_write_explicit_per_1m = +advState.cache_write_explicit_per_1m;
    if (advState.cache_write_implicit_per_1m !== '') cfg.cache_write_implicit_per_1m = +advState.cache_write_implicit_per_1m;
    if (advState.cache_storage_per_1m !== '') cfg.cache_storage_per_1m = +advState.cache_storage_per_1m;
    if (advState.cache_storage_per_1m_per_hour !== '') cfg.cache_storage_per_1m_per_hour = +advState.cache_storage_per_1m_per_hour;
    if (advState.batch_discount !== '') cfg.batch_discount = +advState.batch_discount;
    return cfg;
  }
  return null;
}

/* ── 阶梯编辑器组件 ── */

function TierListEditor({ label, tiers, onChange, required }) {
  const tierInputCls = 'border border-gray-300 rounded px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-blue-500';
  const addTier = () => onChange([...tiers, { up_to_k: '', price_per_1m: '' }]);
  const removeTier = (i) => onChange(tiers.filter((_, idx) => idx !== i));
  const update = (i, key, val) => { const next = [...tiers]; next[i] = { ...next[i], [key]: val }; onChange(next); };
  return (
    <div className="mb-3">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs font-medium text-gray-600">{label} {required && <span className="text-red-500">*</span>}</span>
        <button type="button" onClick={addTier} className="text-xs text-blue-600 hover:text-blue-800 font-medium">+ 添加档位</button>
      </div>
      {tiers.map((tier, i) => (
        <div key={i} className="flex items-center gap-2 mb-1.5">
          <input type="number" step="any" placeholder="上限(K)" value={tier.up_to_k} onChange={(e) => update(i, 'up_to_k', e.target.value)} className={`${tierInputCls} w-24`} />
          <span className="text-xs text-gray-400 whitespace-nowrap">K tokens</span>
          <input type="number" step="any" placeholder="价格" value={tier.price_per_1m} onChange={(e) => update(i, 'price_per_1m', e.target.value)} className={`${tierInputCls} w-28`} />
          <span className="text-xs text-gray-400 whitespace-nowrap">/1M</span>
          {tiers.length > 1 && (
            <button type="button" onClick={() => removeTier(i)} className="text-red-400 hover:text-red-600"><X size={14} /></button>
          )}
        </div>
      ))}
      <p className="text-xs text-gray-400">上限留空 = 无上限（最后一档）</p>
    </div>
  );
}

function OldTierListEditor({ tiers, onChange }) {
  const tierInputCls = 'border border-gray-300 rounded px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-blue-500';
  const addTier = () => onChange([...tiers, { up_to_tokens: '', input_per_1m: '', output_per_1m: '' }]);
  const removeTier = (i) => onChange(tiers.filter((_, idx) => idx !== i));
  const update = (i, key, val) => { const next = [...tiers]; next[i] = { ...next[i], [key]: val }; onChange(next); };
  return (
    <div className="mb-3">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs font-medium text-gray-600">阶梯档位 <span className="text-red-500">*</span></span>
        <button type="button" onClick={addTier} className="text-xs text-blue-600 hover:text-blue-800 font-medium">+ 添加档位</button>
      </div>
      <div className="grid grid-cols-[1fr_1fr_1fr_auto] gap-x-2 gap-y-1 text-xs text-gray-500 mb-1">
        <span>上限 (tokens)</span><span>输入价格/1M</span><span>输出价格/1M</span><span />
      </div>
      {tiers.map((tier, i) => (
        <div key={i} className="grid grid-cols-[1fr_1fr_1fr_auto] gap-x-2 gap-y-1.5 mb-1.5">
          <input type="number" step="1" placeholder="如 128000" value={tier.up_to_tokens} onChange={(e) => update(i, 'up_to_tokens', e.target.value)} className={tierInputCls} />
          <input type="number" step="any" placeholder="输入/1M" value={tier.input_per_1m} onChange={(e) => update(i, 'input_per_1m', e.target.value)} className={tierInputCls} />
          <input type="number" step="any" placeholder="输出/1M" value={tier.output_per_1m} onChange={(e) => update(i, 'output_per_1m', e.target.value)} className={tierInputCls} />
          {tiers.length > 1 ? (
            <button type="button" onClick={() => removeTier(i)} className="text-red-400 hover:text-red-600 self-center"><X size={14} /></button>
          ) : <span />}
        </div>
      ))}
      <p className="text-xs text-gray-400">上限留空 = 无上限（最后一档）。阶梯按累计 token 分段计费</p>
    </div>
  );
}

function ComboTierListEditor({ tiers, onChange }) {
  const tierInputCls = 'border border-gray-300 rounded px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-blue-500';
  const addTier = () => onChange([...tiers, { input_up_to_k: '', output_up_to_k: '', input_price_per_1m: '', output_price_per_1m: '', cache_read_price_per_1m: '' }]);
  const removeTier = (i) => onChange(tiers.filter((_, idx) => idx !== i));
  const update = (i, key, val) => { const next = [...tiers]; next[i] = { ...next[i], [key]: val }; onChange(next); };
  return (
    <div className="mb-3">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs font-medium text-gray-600">组合阶梯档位</span>
        <button type="button" onClick={addTier} className="text-xs text-blue-600 hover:text-blue-800 font-medium">+ 添加档位</button>
      </div>
      <div className="space-y-2">
        {tiers.map((tier, i) => (
          <div key={i} className="bg-gray-50 rounded-lg p-2.5 border border-gray-200">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs text-gray-500 font-medium">档位 {i + 1}</span>
              {tiers.length > 1 && (
                <button type="button" onClick={() => removeTier(i)} className="text-red-400 hover:text-red-600"><X size={14} /></button>
              )}
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-gray-400">Input上限(K)</label>
                <input type="number" step="any" placeholder="留空=无上限" value={tier.input_up_to_k} onChange={(e) => update(i, 'input_up_to_k', e.target.value)} className={`${tierInputCls} w-full`} />
              </div>
              <div>
                <label className="text-xs text-gray-400">Output上限(K)</label>
                <input type="number" step="any" placeholder="留空=无上限" value={tier.output_up_to_k} onChange={(e) => update(i, 'output_up_to_k', e.target.value)} className={`${tierInputCls} w-full`} />
              </div>
              <div>
                <label className="text-xs text-gray-400">Input价格/1M</label>
                <input type="number" step="any" placeholder="0" value={tier.input_price_per_1m} onChange={(e) => update(i, 'input_price_per_1m', e.target.value)} className={`${tierInputCls} w-full`} />
              </div>
              <div>
                <label className="text-xs text-gray-400">Output价格/1M</label>
                <input type="number" step="any" placeholder="0" value={tier.output_price_per_1m} onChange={(e) => update(i, 'output_price_per_1m', e.target.value)} className={`${tierInputCls} w-full`} />
              </div>
              <div className="col-span-2">
                <label className="text-xs text-gray-400">缓存读取价格/1M（可选）</label>
                <input type="number" step="any" placeholder="留空=不单独计费" value={tier.cache_read_price_per_1m} onChange={(e) => update(i, 'cache_read_price_per_1m', e.target.value)} className={`${tierInputCls} w-full`} />
              </div>
            </div>
          </div>
        ))}
      </div>
      <p className="text-xs text-gray-400 mt-1">按 input×output 区间组合匹配，优先于独立阶梯</p>
    </div>
  );
}

function ModelPriceForm({ initialData, onSubmit, onCancel, submitting, submitError }) {
  const isEdit = !!initialData;
  const cfg = initialData?.pricing_config;
  const initType = isEdit ? parsePricingType(cfg) : 'standard';

  const [modelGroup, setModelGroup] = useState(initialData?.model_group ?? '');
  const [modelName, setModelName] = useState(initialData?.model_name ?? '');
  const [provider, setProvider] = useState(initialData?.provider ?? '');
  const [currency, setCurrency] = useState(initialData?.currency ?? 'USD');
  const [isActive, setIsActive] = useState(initialData?.is_active ?? true);
  const [note, setNote] = useState(initialData?.note ?? '');
  const [aliases, setAliases] = useState(initialData?.aliases ?? []);
  const [aliasInput, setAliasInput] = useState('');
  const [pricingType, setPricingType] = useState(initType);
  const [fields, setFields] = useState(() => initFieldsFromConfig(cfg));
  const [advState, setAdvState] = useState(() => initAdvancedState(cfg));
  const [tieredTiers, setTieredTiers] = useState(() => initTieredState(cfg));

  const setField = (key) => (e) => setFields((prev) => ({ ...prev, [key]: e.target.value }));
  const setAdv = (key, val) => setAdvState((prev) => ({ ...prev, [key]: val }));

  const handleModelGroupChange = (val) => {
    setModelGroup(val);
    const { provider: parsed_provider, modelName: parsed_model } = parseModelGroup(val);
    setModelName(parsed_model);
    setProvider(parsed_provider);
  };

  const addAlias = () => {
    const val = aliasInput.trim();
    if (val && !aliases.includes(val)) {
      setAliases([...aliases, val]);
    }
    setAliasInput('');
  };

  const removeAlias = (a) => setAliases(aliases.filter((x) => x !== a));

  const handleAliasKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      addAlias();
    }
  };

  const handleSubmit = () => {
    const pricingConfig = buildPricingConfig(pricingType, fields, currency, advState, tieredTiers);
    onSubmit({
      model_name: modelName.trim(),
      model_group: modelGroup.trim() || null,
      provider: provider.trim() || null,
      model_family: 'generic',
      pricing_config: pricingConfig,
      currency,
      is_active: isActive,
      aliases: aliases.length > 0 ? aliases : null,
      note: note.trim() || null,
    });
  };

  const inputCls = 'border border-gray-300 rounded-lg px-3 py-2 text-sm w-full focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none';
  const labelCls = 'block text-xs font-medium text-gray-600 mb-1';

  return (
    <div className="space-y-4">
      {/* Model Group 快捷输入 */}
      {!isEdit && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <label className={labelCls}>
            <span className="flex items-center gap-1"><Layers size={12} /> 从 Model Group 解析</span>
          </label>
          <p className="text-xs text-gray-500 mb-2">
            输入 LiteLLM 的 model_group（如 <code className="bg-white px-1 rounded">openai-gpt-4.1</code>、<code className="bg-white px-1 rounded">road-claude-sonnet-4-6</code>），自动解析模型名和供应商
          </p>
          <input
            className={inputCls}
            value={modelGroup}
            onChange={(e) => handleModelGroupChange(e.target.value)}
            placeholder="如 openai-gpt-4.1、road-claude-sonnet-4-6、deepseek-r1"
          />
          {modelGroup && modelName && (
            <div className="mt-2 flex items-center gap-3 text-xs">
              <span className="text-gray-500">解析结果：</span>
              <span className="bg-white px-2 py-0.5 rounded border border-blue-200 font-mono text-blue-800">模型: {modelName}</span>
              {provider && <span className="bg-white px-2 py-0.5 rounded border border-blue-200 font-mono text-blue-800">供应商: {provider}</span>}
              {!provider && <span className="text-gray-400">供应商: (未识别)</span>}
            </div>
          )}
        </div>
      )}

      {/* 基础信息 */}
      <div className="grid grid-cols-2 gap-4">
        <div className="col-span-2">
          {isEdit ? (
            <>
              <label className={labelCls}>模型名称（规范化）</label>
              <div className="font-mono text-sm font-medium text-gray-900 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">{modelName}</div>
              {aliases.length > 0 && (
                <div className="mt-2">
                  <label className={labelCls}>原始名称</label>
                  <div className="flex flex-wrap gap-1.5">
                    {aliases.map((a) => (
                      <span key={a} className="inline-block bg-amber-50 text-amber-700 text-xs font-mono px-2 py-1 rounded-md">{a}</span>
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : (
            <>
              <label className={labelCls}>模型名称 <span className="text-red-500">*</span></label>
              <input className={inputCls} value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder="如 gpt-4.1、deepseek-r1" />
            </>
          )}
        </div>
        <div>
          <label className={labelCls}>供应商（可选）</label>
          <div className="relative">
            <input
              className={inputCls}
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder="如 openai、volcengine、road"
              list="provider-options"
            />
            <datalist id="provider-options">
              {KNOWN_PROVIDERS.map((p) => <option key={p} value={p} />)}
            </datalist>
          </div>
          <p className="text-xs text-gray-400 mt-1">留空表示该模型默认单价，填写后按供应商独立管理</p>
        </div>
        <div>
          <label className={labelCls}>货币</label>
          <select className={inputCls} value={currency} onChange={(e) => setCurrency(e.target.value)}>
            <option value="USD">USD（美元）</option>
            <option value="CNY">CNY（人民币）</option>
            <option value="元">元（人民币）</option>
          </select>
        </div>
        <div className="flex items-center gap-2 mt-5">
          <input type="checkbox" id="is_active" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} className="w-4 h-4 rounded accent-blue-600" />
          <label htmlFor="is_active" className="text-sm text-gray-700">启用</label>
        </div>
      </div>

      {/* 别名 */}
      <div>
        <label className={labelCls}>
          <span className="flex items-center gap-1"><Tag size={12} /> 别名（可选）</span>
        </label>
        <p className="text-xs text-gray-400 mb-2">模型名的其他叫法或火山引擎 endpoint，查价时自动匹配。按 Enter 或逗号添加。</p>
        <div className="flex flex-wrap gap-1.5 mb-2">
          {aliases.map((a) => (
            <span key={a} className="inline-flex items-center gap-1 bg-blue-50 text-blue-700 text-xs font-mono px-2 py-1 rounded-md">
              {a}
              <button type="button" onClick={() => removeAlias(a)} className="text-blue-400 hover:text-blue-700"><X size={12} /></button>
            </span>
          ))}
        </div>
        <input
          className={inputCls}
          value={aliasInput}
          onChange={(e) => setAliasInput(e.target.value)}
          onKeyDown={handleAliasKeyDown}
          onBlur={addAlias}
          placeholder="如 kimi-k2-0905、ep-20250530135709-c6bk7"
        />
      </div>

      <div>
        <label className={labelCls}>备注（可选）</label>
        <input className={inputCls} value={note} onChange={(e) => setNote(e.target.value)} placeholder="备注信息" />
      </div>

      {/* 定价配置 */}
      <div className="border-t border-gray-100 pt-4">
        <label className={labelCls}>定价类型 <span className="text-red-500">*</span></label>
        <div className="flex gap-2 flex-wrap mb-4">
          {PRICING_TYPE_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => setPricingType(opt.value)}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                pricingType === opt.value
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {/* ── standard ── */}
        {pricingType === 'standard' && (
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className={labelCls}>输入价格 / 1M tokens <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.input_per_1m} onChange={setField('input_per_1m')} placeholder="如 2.5" />
            </div>
            <div>
              <label className={labelCls}>输出价格 / 1M tokens <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.output_per_1m} onChange={setField('output_per_1m')} placeholder="如 10.0" />
            </div>
            <div>
              <label className={labelCls}>缓存读取 / 1M（可选）</label>
              <input type="number" step="any" className={inputCls} value={fields.cache_read_per_1m} onChange={setField('cache_read_per_1m')} placeholder="如 0.5" />
            </div>
          </div>
        )}

        {/* ── with_cache ── */}
        {pricingType === 'with_cache' && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={labelCls}>输入 / 1M <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.input_per_1m} onChange={setField('input_per_1m')} placeholder="如 3.0" />
            </div>
            <div>
              <label className={labelCls}>输出 / 1M <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.output_per_1m} onChange={setField('output_per_1m')} placeholder="如 15.0" />
            </div>
            <div>
              <label className={labelCls}>缓存写入 / 1M <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.cache_write_per_1m} onChange={setField('cache_write_per_1m')} placeholder="如 3.75" />
            </div>
            <div>
              <label className={labelCls}>缓存读取 / 1M <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.cache_read_per_1m} onChange={setField('cache_read_per_1m')} placeholder="如 0.3" />
            </div>
          </div>
        )}

        {/* ── with_thinking ── */}
        {pricingType === 'with_thinking' && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={labelCls}>输入 / 1M <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.input_per_1m} onChange={setField('input_per_1m')} placeholder="如 3.0" />
            </div>
            <div>
              <label className={labelCls}>输出 / 1M <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.output_per_1m} onChange={setField('output_per_1m')} placeholder="如 15.0" />
            </div>
            <div>
              <label className={labelCls}>思考 Token / 1M</label>
              <input type="number" step="any" className={inputCls} value={fields.thinking_per_1m} onChange={setField('thinking_per_1m')} placeholder="留空=与输出同价" />
            </div>
            <div>
              <label className={labelCls}>缓存读取 / 1M（可选）</label>
              <input type="number" step="any" className={inputCls} value={fields.cache_read_per_1m} onChange={setField('cache_read_per_1m')} placeholder="如 0.3" />
            </div>
          </div>
        )}

        {/* ── combined（缓存+思考组合）── */}
        {pricingType === 'combined' && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={labelCls}>输入 / 1M <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.input_per_1m} onChange={setField('input_per_1m')} placeholder="如 3.0" />
            </div>
            <div>
              <label className={labelCls}>输出 / 1M <span className="text-red-500">*</span></label>
              <input type="number" step="any" className={inputCls} value={fields.output_per_1m} onChange={setField('output_per_1m')} placeholder="如 15.0" />
            </div>
            <div>
              <label className={labelCls}>缓存写入 / 1M（可选）</label>
              <input type="number" step="any" className={inputCls} value={fields.cache_write_per_1m} onChange={setField('cache_write_per_1m')} placeholder="如 3.75" />
            </div>
            <div>
              <label className={labelCls}>缓存读取 / 1M（可选）</label>
              <input type="number" step="any" className={inputCls} value={fields.cache_read_per_1m} onChange={setField('cache_read_per_1m')} placeholder="如 0.3" />
            </div>
            <div className="col-span-2">
              <label className={labelCls}>思考 Token / 1M（可选）</label>
              <input type="number" step="any" className={inputCls} value={fields.thinking_per_1m} onChange={setField('thinking_per_1m')} placeholder="留空=与输出同价" />
            </div>
          </div>
        )}

        {/* ── tiered（简单阶梯）── */}
        {pricingType === 'tiered' && (
          <OldTierListEditor tiers={tieredTiers} onChange={setTieredTiers} />
        )}

        {/* ── advanced（高级定价）── */}
        {pricingType === 'advanced' && (
          <div className="space-y-4">
            {/* 输入/输出阶梯（必填）*/}
            <div className="grid grid-cols-2 gap-4">
              <TierListEditor label="输入 Token 阶梯" tiers={advState.inputTiers} onChange={(v) => setAdv('inputTiers', v)} required />
              <TierListEditor label="输出 Token 阶梯" tiers={advState.outputTiers} onChange={(v) => setAdv('outputTiers', v)} required />
            </div>

            {/* 思考 Token 阶梯（可选）*/}
            <div className="border-t border-gray-100 pt-3">
              <label className="flex items-center gap-2 text-sm mb-2 cursor-pointer">
                <input type="checkbox" checked={advState.thinkingTiersEnabled} onChange={(e) => setAdv('thinkingTiersEnabled', e.target.checked)} className="w-4 h-4 rounded accent-blue-600" />
                <span className="font-medium text-gray-700">启用思考 Token 阶梯</span>
                <span className="text-xs text-gray-400">（不启用则思考 Token 按输出价格计费）</span>
              </label>
              {advState.thinkingTiersEnabled && (
                <TierListEditor label="思考 Token 阶梯" tiers={advState.thinkingTiers} onChange={(v) => setAdv('thinkingTiers', v)} />
              )}
            </div>

            {/* 缓存读取阶梯（可选）*/}
            <div className="border-t border-gray-100 pt-3">
              <label className="flex items-center gap-2 text-sm mb-2 cursor-pointer">
                <input type="checkbox" checked={advState.cacheReadTiersEnabled} onChange={(e) => setAdv('cacheReadTiersEnabled', e.target.checked)} className="w-4 h-4 rounded accent-blue-600" />
                <span className="font-medium text-gray-700">启用缓存读取阶梯</span>
              </label>
              {advState.cacheReadTiersEnabled && (
                <TierListEditor label="缓存读取阶梯" tiers={advState.cacheReadTiers} onChange={(v) => setAdv('cacheReadTiers', v)} />
              )}
            </div>

            {/* 缓存写入设置 */}
            <div className="border-t border-gray-100 pt-3">
              <span className="block text-xs font-medium text-gray-600 mb-2">缓存写入设置</span>
              <label className="flex items-center gap-2 text-sm mb-2 cursor-pointer">
                <input type="checkbox" checked={advState.cacheWriteTiersEnabled} onChange={(e) => setAdv('cacheWriteTiersEnabled', e.target.checked)} className="w-4 h-4 rounded accent-blue-600" />
                <span className="font-medium text-gray-700">使用缓存写入阶梯</span>
                <span className="text-xs text-gray-400">（如 Claude Opus/Sonnet 4.6 按 200K 分档）</span>
              </label>
              {advState.cacheWriteTiersEnabled && (
                <TierListEditor label="缓存写入阶梯" tiers={advState.cacheWriteTiers} onChange={(v) => setAdv('cacheWriteTiers', v)} />
              )}
              {!advState.cacheWriteTiersEnabled && (
                <div className="grid grid-cols-2 gap-3 mt-2">
                  <div>
                    <label className={labelCls}>通用缓存写入 / 1M</label>
                    <input type="number" step="any" className={inputCls} value={advState.cache_write_per_1m} onChange={(e) => setAdv('cache_write_per_1m', e.target.value)} placeholder="如 3.75" />
                    <p className="text-xs text-gray-400 mt-0.5">DeepSeek/Kimi/doubao 等</p>
                  </div>
                  <div>
                    <label className={labelCls}>5分钟缓存写入 / 1M</label>
                    <input type="number" step="any" className={inputCls} value={advState.cache_write_5min_per_1m} onChange={(e) => setAdv('cache_write_5min_per_1m', e.target.value)} placeholder="Claude 5min" />
                  </div>
                  <div>
                    <label className={labelCls}>1小时缓存写入 / 1M</label>
                    <input type="number" step="any" className={inputCls} value={advState.cache_write_1h_per_1m} onChange={(e) => setAdv('cache_write_1h_per_1m', e.target.value)} placeholder="Claude 1h" />
                  </div>
                  <div>
                    <label className={labelCls}>显式缓存写入 / 1M</label>
                    <input type="number" step="any" className={inputCls} value={advState.cache_write_explicit_per_1m} onChange={(e) => setAdv('cache_write_explicit_per_1m', e.target.value)} placeholder="qwen3-max 显式" />
                  </div>
                  <div>
                    <label className={labelCls}>隐式缓存写入 / 1M</label>
                    <input type="number" step="any" className={inputCls} value={advState.cache_write_implicit_per_1m} onChange={(e) => setAdv('cache_write_implicit_per_1m', e.target.value)} placeholder="qwen3-max 隐式" />
                  </div>
                </div>
              )}
            </div>

            {/* 缓存存储费 */}
            <div className="border-t border-gray-100 pt-3">
              <span className="block text-xs font-medium text-gray-600 mb-2">缓存存储费（可选，Gemini 等）</span>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className={labelCls}>缓存存储 / 1M</label>
                  <input type="number" step="any" className={inputCls} value={advState.cache_storage_per_1m} onChange={(e) => setAdv('cache_storage_per_1m', e.target.value)} placeholder="旧版，按1小时计" />
                </div>
                <div>
                  <label className={labelCls}>缓存存储 / 1M / 小时</label>
                  <input type="number" step="any" className={inputCls} value={advState.cache_storage_per_1m_per_hour} onChange={(e) => setAdv('cache_storage_per_1m_per_hour', e.target.value)} placeholder="Gemini 按小时计费" />
                </div>
              </div>
            </div>

            {/* 组合阶梯（可选）*/}
            <div className="border-t border-gray-100 pt-3">
              <label className="flex items-center gap-2 text-sm mb-2 cursor-pointer">
                <input type="checkbox" checked={advState.comboTiersEnabled} onChange={(e) => setAdv('comboTiersEnabled', e.target.checked)} className="w-4 h-4 rounded accent-blue-600" />
                <span className="font-medium text-gray-700">启用组合阶梯</span>
                <span className="text-xs text-gray-400">（input×output 区间组合定价，如豆包 Seed 1.6）</span>
              </label>
              {advState.comboTiersEnabled && (
                <ComboTierListEditor tiers={advState.comboTiers} onChange={(v) => setAdv('comboTiers', v)} />
              )}
            </div>

            {/* Batch 折扣 */}
            <div className="border-t border-gray-100 pt-3">
              <label className={labelCls}>Batch 折扣系数（可选）</label>
              <input type="number" step="any" className={`${inputCls} max-w-xs`} value={advState.batch_discount} onChange={(e) => setAdv('batch_discount', e.target.value)} placeholder="如 0.5 = 半价，留空=无折扣" />
            </div>
          </div>
        )}
      </div>

      {submitError && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg text-sm">{submitError}</div>
      )}

      <div className="flex justify-end gap-3 pt-2">
        <button type="button" onClick={onCancel} className="px-5 py-2 rounded-lg text-sm text-gray-600 border border-gray-300 hover:bg-gray-50 transition-colors">
          取消
        </button>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={submitting}
          className="px-5 py-2 rounded-lg text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {submitting ? '保存中...' : (isEdit ? '保存修改' : '添加')}
        </button>
      </div>
    </div>
  );
}

function PricingTypeBadge({ type }) {
  const colors = {
    standard: 'bg-gray-100 text-gray-700',
    with_cache: 'bg-blue-50 text-blue-700',
    with_thinking: 'bg-purple-50 text-purple-700',
    advanced: 'bg-green-50 text-green-700',
    tiered: 'bg-yellow-50 text-yellow-700',
    combined: 'bg-orange-50 text-orange-700',
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${colors[type] || 'bg-gray-100 text-gray-700'}`}>
      {type}
    </span>
  );
}

function pricingSummary(config) {
  if (!config) return '-';
  const t = config.type;
  if (t === 'standard' || t === 'with_cache' || t === 'with_thinking') {
    const cur = config.currency || '';
    return `输入 ${config.input_per_1m ?? '-'} / 输出 ${config.output_per_1m ?? '-'} (${cur}/1M)`;
  }
  if (t === 'advanced') {
    const input = config.input_tiers?.[0]?.price_per_1m;
    const output = config.output_tiers?.[0]?.price_per_1m;
    const cur = config.currency || '';
    if (input != null && output != null) return `输入 ${input}～ / 输出 ${output}～ (${cur}/1M)`;
  }
  return t;
}

function Modal({ title, onClose, children, wide }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className={`bg-white rounded-2xl shadow-2xl w-full ${wide ? 'max-w-4xl' : 'max-w-2xl'} max-h-[90vh] overflow-y-auto mx-4`}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
          <h2 className="text-base font-semibold text-gray-900">{title}</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors">
            <X size={20} />
          </button>
        </div>
        <div className="px-6 py-5">{children}</div>
      </div>
    </div>
  );
}

function ModelManagementPanel() {
  const [prices, setPrices] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [editingPrice, setEditingPrice] = useState(null);
  const [deletingId, setDeletingId] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [providerFilter, setProviderFilter] = useState('');
  const [refreshing, setRefreshing] = useState(false);

  const fetchPrices = async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await axios.get(`${API_BASE}/model-prices`, { headers });
      setPrices(resp.data);
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchPrices(); }, []);

  const handleAdd = () => { setEditingPrice(null); setSubmitError(null); setShowForm(true); };
  const handleEdit = (p) => { setEditingPrice(p); setSubmitError(null); setShowForm(true); };
  const handleCloseForm = () => { setShowForm(false); setEditingPrice(null); };

  const handleSubmit = async (payload) => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      if (editingPrice) {
        await axios.put(`${API_BASE}/model-prices/${editingPrice.id}`, payload, { headers });
      } else {
        await axios.post(`${API_BASE}/model-prices`, payload, { headers });
      }
      handleCloseForm();
      await fetchPrices();
    } catch (err) {
      setSubmitError(err.response?.data?.detail || err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (id) => {
    try {
      await axios.delete(`${API_BASE}/model-prices/${id}`, { headers });
      setDeletingId(null);
      await fetchPrices();
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
      setDeletingId(null);
    }
  };

  const handleRefreshCache = async () => {
    setRefreshing(true);
    try {
      await axios.post(`${API_BASE}/model-prices/refresh-cache`, null, { headers });
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setRefreshing(false);
    }
  };

  // 搜索 + 供应商过滤
  const filteredPrices = prices.filter((p) => {
    if (providerFilter) {
      const prov = p.provider || '';
      if (providerFilter === '__none__') {
        if (prov) return false;
      } else if (prov !== providerFilter) {
        return false;
      }
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      return (
        p.model_name.toLowerCase().includes(q) ||
        (p.model_group || '').toLowerCase().includes(q) ||
        (p.provider || '').toLowerCase().includes(q) ||
        (p.aliases || []).some((a) => a.toLowerCase().includes(q))
      );
    }
    return true;
  });

  // 按供应商分组
  const providerGroups = [];
  const groupMap = new Map();
  for (const p of filteredPrices) {
    const key = p.provider || '（默认）';
    if (!groupMap.has(key)) {
      groupMap.set(key, []);
    }
    groupMap.get(key).push(p);
  }
  // 排序：默认组在前，其他按字母排序
  const sortedKeys = [...groupMap.keys()].sort((a, b) => {
    if (a === '（默认）') return -1;
    if (b === '（默认）') return 1;
    return a.localeCompare(b);
  });
  for (const key of sortedKeys) {
    providerGroups.push({ provider: key, items: groupMap.get(key) });
  }

  // 已有的供应商列表（用于筛选下拉）
  const existingProviders = [...new Set(prices.map((p) => p.provider).filter(Boolean))].sort();

  // 统计
  const activeCount = prices.filter((p) => p.is_active).length;
  const usdCount = prices.filter((p) => p.currency === 'USD').length;
  const cnyCount = prices.length - usdCount;

  return (
    <div className="space-y-5">
      {/* 头部 */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">模型定价管理</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            共 {prices.length} 条定价（{activeCount} 启用），{usdCount} 美元 / {cnyCount} 人民币
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleRefreshCache}
            disabled={refreshing}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm text-gray-600 border border-gray-300 hover:bg-gray-50 disabled:opacity-50 transition-colors"
            title="刷新定价缓存"
          >
            <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
            刷新缓存
          </button>
          <button
            onClick={handleAdd}
            className="flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            <Plus size={16} />
            添加模型
          </button>
        </div>
      </div>

      {error && <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg text-sm">{error}</div>}

      {/* 搜索栏 + 供应商筛选 */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            className="w-full border border-gray-300 rounded-lg pl-9 pr-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none"
            placeholder="搜索原始模型名、模型名、供应商、别名..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          {searchQuery && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-gray-400">
              {filteredPrices.length} / {prices.length}
            </span>
          )}
        </div>
        <select
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none min-w-[140px]"
          value={providerFilter}
          onChange={(e) => setProviderFilter(e.target.value)}
        >
          <option value="">全部供应商</option>
          <option value="__none__">（未指定）</option>
          {existingProviders.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </div>

      {/* 表格 */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {loading ? (
          <div className="text-center text-gray-400 py-12">加载中...</div>
        ) : prices.length === 0 ? (
          <div className="text-center text-gray-400 py-12">
            <Database size={40} className="mx-auto mb-3 opacity-30" />
            <p>暂无定价配置</p>
            <p className="text-xs mt-1">点击「添加模型」或「批量导入」开始配置定价</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 bg-gray-50">
                  <th className="px-4 py-3 text-left font-medium text-gray-600">原始模型名称</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">模型名称</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">供应商</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">货币</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">定价类型</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">定价摘要</th>
                  <th className="px-4 py-3 text-center font-medium text-gray-600">状态</th>
                  <th className="px-4 py-3 text-right font-medium text-gray-600">操作</th>
                </tr>
              </thead>
              <tbody>
                {providerGroups.map(({ provider: groupProvider, items }) => (
                  <React.Fragment key={groupProvider}>
                    {providerGroups.length > 1 && (
                      <tr className="bg-blue-50/60">
                        <td colSpan={8} className="px-4 py-2">
                          <span className="text-xs font-semibold text-blue-700">
                            {groupProvider === '（默认）' ? '默认定价（未指定供应商）' : `供应商：${groupProvider}`}
                          </span>
                          <span className="text-xs text-blue-500 ml-2">{items.length} 个模型</span>
                        </td>
                      </tr>
                    )}
                    {items.map((p) => (
                  <tr key={p.id} className="border-b border-gray-100 hover:bg-gray-50/50 transition-colors">
                    <td className="px-4 py-3">
                      <div className="font-mono text-sm font-medium text-gray-900">
                        {p.model_group || (p.provider ? `${p.provider}-${p.model_name}` : p.model_name)}
                      </div>
                      {p.aliases && p.aliases.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1">
                          {p.aliases.map((a) => (
                            <span key={a} className="inline-block bg-amber-50 text-amber-700 text-xs font-mono px-1.5 py-0.5 rounded" title={a}>
                              {a}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className="font-mono text-xs text-gray-500">{p.model_name}</span>
                    </td>
                    <td className="px-4 py-3">
                      {p.provider ? (
                        <span className="inline-block bg-blue-50 text-blue-700 text-xs font-medium px-2 py-0.5 rounded">{p.provider}</span>
                      ) : (
                        <span className="text-gray-300">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${
                        p.currency === 'USD' ? 'bg-green-50 text-green-700' : 'bg-orange-50 text-orange-700'
                      }`}>
                        {p.currency}
                      </span>
                    </td>
                    <td className="px-4 py-3"><PricingTypeBadge type={p.pricing_config?.type} /></td>
                    <td className="px-4 py-3 text-gray-500 text-xs max-w-[200px] truncate">{pricingSummary(p.pricing_config)}</td>
                    <td className="px-4 py-3 text-center">
                      <span className={`inline-block w-2 h-2 rounded-full ${p.is_active ? 'bg-green-500' : 'bg-gray-300'}`} title={p.is_active ? '启用' : '停用'} />
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => handleEdit(p)}
                          className="p-1.5 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-colors"
                          title="编辑"
                        >
                          <Pencil size={15} />
                        </button>
                        <button
                          onClick={() => setDeletingId(p.id)}
                          className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                          title="删除"
                        >
                          <Trash2 size={15} />
                        </button>
                      </div>
                    </td>
                  </tr>
                    ))}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 添加/编辑弹窗 */}
      {showForm && (
        <Modal title={editingPrice ? `编辑定价：${editingPrice.model_group || (editingPrice.provider ? `${editingPrice.provider}-${editingPrice.model_name}` : editingPrice.model_name)}` : '添加模型定价'} onClose={handleCloseForm} wide>
          <ModelPriceForm
            initialData={editingPrice}
            onSubmit={handleSubmit}
            onCancel={handleCloseForm}
            submitting={submitting}
            submitError={submitError}
          />
        </Modal>
      )}

      {/* 删除确认 */}
      {deletingId && (
        <Modal title="确认删除" onClose={() => setDeletingId(null)}>
          <p className="text-sm text-gray-600 mb-6">确定要删除该定价配置吗？删除后将回退至内置定价。</p>
          <div className="flex justify-end gap-3">
            <button onClick={() => setDeletingId(null)} className="px-5 py-2 rounded-lg text-sm text-gray-600 border border-gray-300 hover:bg-gray-50 transition-colors">
              取消
            </button>
            <button
              onClick={() => handleDelete(deletingId)}
              className="px-5 py-2 rounded-lg text-sm font-medium bg-red-600 text-white hover:bg-red-700 transition-colors"
            >
              确认删除
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ───────────────────────────────────────────────────
   主应用（左侧导航）
   ─────────────────────────────────────────────────── */
/* ───────────────────────────────────────────────────
   未定价模型
   ─────────────────────────────────────────────────── */
function UnpricedModelsPanel() {
  const defaults = getDefaultDates();
  const [start, setStart] = useState(defaults.start);
  const [end, setEnd] = useState(defaults.end);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null);

  const handleQuery = async () => {
    if (!start || !end) { setError('请选择日期范围'); return; }
    setLoading(true); setError(null); setData(null);
    try {
      const payload = { start: `${start}T00:00:00`, end: `${end}T23:59:59` };
      const resp = await axios.post(`${API_BASE}/analytics/unpriced-models`, payload, { headers });
      setData(resp.data);
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-4 items-end">
        <DateRangePicker start={start} end={end} onStartChange={setStart} onEndChange={setEnd} />
        <button onClick={handleQuery} disabled={loading}
          className="bg-blue-600 text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors h-[38px]">
          {loading ? '查询中...' : '查询'}
        </button>
      </div>
      {error && <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg text-sm">{error}</div>}
      {data && (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">未定价组合数</div>
              <div className="text-2xl font-bold text-amber-600 mt-1">{data.total}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">未定价总请求数</div>
              <div className="text-2xl font-bold text-red-600 mt-1">{formatNumber(data.total_requests)}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="text-xs text-gray-500">说明</div>
              <div className="text-sm text-gray-600 mt-1">这些模型+供应商组合有请求记录但缺少定价配置，无法计算费用</div>
            </div>
          </div>

          {data.items && data.items.length > 0 ? (
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
              <div className="px-5 py-4 border-b border-gray-200">
                <h3 className="font-semibold text-gray-800">未定价模型列表（共 {data.items.length} 条，按请求数降序）</h3>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 border-b border-gray-200">
                      <th className="px-4 py-3 text-left font-medium text-gray-600">#</th>
                      <th className="px-4 py-3 text-left font-medium text-gray-600">模型</th>
                      <th className="px-4 py-3 text-left font-medium text-gray-600">供应商</th>
                      <th className="px-4 py-3 text-right font-medium text-gray-600">请求数</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((item, i) => (
                      <tr key={`${item.model}-${item.provider}`} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="px-4 py-3 text-gray-400">{i + 1}</td>
                        <td className="px-4 py-3 font-mono text-sm font-medium">{item.model}</td>
                        <td className="px-4 py-3">{item.provider}</td>
                        <td className="px-4 py-3 text-right font-mono">{formatNumber(item.request_count)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <div className="bg-green-50 border border-green-200 text-green-700 px-4 py-3 rounded-lg text-sm">
              所有模型均已配置定价，无遗漏。
            </div>
          )}
        </>
      )}
    </div>
  );
}

const NAV_ITEMS = [
  { id: 'model', label: '按模型 / 供应商', icon: BarChart2 },
  { id: 'family', label: '按模型汇总', icon: Layers },
  { id: 'provider', label: '按供应商汇总', icon: Building2 },
  { id: 'apikey', label: '按 API Key', icon: KeyRound },
  { id: 'unpriced', label: '未定价模型', icon: AlertTriangle },
  { id: 'prices', label: '模型定价管理', icon: Database },
];

function App() {
  const [activePage, setActivePage] = useState('model');
  const [modelOptions, setModelOptions] = useState([]);
  const [providerOptions, setProviderOptions] = useState([]);

  useEffect(() => {
    axios.get(`${API_BASE}/analytics/options`, { headers }).then((resp) => {
      setModelOptions(resp.data.models || []);
      setProviderOptions(resp.data.providers || []);
    }).catch(console.error);
  }, []);

  const activeItem = NAV_ITEMS.find((n) => n.id === activePage);

  return (
    <div className="flex h-screen bg-gray-50 overflow-hidden">
      {/* 左侧导航 */}
      <aside className="w-56 flex-shrink-0 bg-white border-r border-gray-200 flex flex-col">
        <div className="px-5 py-5 border-b border-gray-100">
          <h1 className="text-base font-bold text-gray-900">Token 计费</h1>
          <p className="text-xs text-gray-400 mt-0.5">消费分析与定价管理</p>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1">
          <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider px-2 mb-2">消费分析</p>
          {NAV_ITEMS.slice(0, 5).map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                onClick={() => setActivePage(item.id)}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors text-left ${
                  activePage === item.id
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'
                }`}
              >
                <Icon size={16} className="flex-shrink-0" />
                {item.label}
              </button>
            );
          })}
          <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider px-2 mb-2 mt-5">管理</p>
          {NAV_ITEMS.slice(5).map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                onClick={() => setActivePage(item.id)}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors text-left ${
                  activePage === item.id
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'
                }`}
              >
                <Icon size={16} className="flex-shrink-0" />
                {item.label}
              </button>
            );
          })}
        </nav>
      </aside>

      {/* 主内容区 */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <header className="bg-white border-b border-gray-200 px-8 py-4 flex-shrink-0">
          <h2 className="text-xl font-semibold text-gray-900">{activeItem?.label}</h2>
        </header>
        <main className="flex-1 overflow-y-auto px-8 py-6">
          <div style={{ display: activePage === 'model' ? 'block' : 'none' }}>
            <ModelProviderPanel modelOptions={modelOptions} providerOptions={providerOptions} />
          </div>
          <div style={{ display: activePage === 'family' ? 'block' : 'none' }}>
            <FamilySummaryPanel />
          </div>
          <div style={{ display: activePage === 'provider' ? 'block' : 'none' }}>
            <ProviderSummaryPanel />
          </div>
          <div style={{ display: activePage === 'apikey' ? 'block' : 'none' }}>
            <ApiKeyPanel />
          </div>
          <div style={{ display: activePage === 'unpriced' ? 'block' : 'none' }}>
            <UnpricedModelsPanel />
          </div>
          <div style={{ display: activePage === 'prices' ? 'block' : 'none' }}>
            <ModelManagementPanel />
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;
