import { useEffect, useRef, useState } from 'react';
import { Wand2, Play, Loader2, AlertTriangle, Sheet, CheckCircle2, Server, Sparkles } from 'lucide-react';
import { apiGet, streamPostEvents, downloadPath } from '../hooks/useApi';
import type {
  AppConfig,
  CatalogInfo,
  CatalogsResponse,
  WorkspaceFilterValue,
  WorkspaceInfo,
  WorkspacesResponse,
} from '../types';
import CatalogFilter from './CatalogFilter';

// Stages the backend emits progress for, in the order they run.
const STAGES: { key: string; label: string }[] = [
  { key: 'catalogs', label: 'Catalog descriptions & tags' },
  { key: 'schemas', label: 'Schema descriptions & tags' },
  { key: 'entities', label: 'Entity descriptions, tags & column comments' },
  { key: 'relationships', label: 'Primary / foreign key statements' },
  { key: 'genie_agents', label: 'Genie Agent instructions' },
  { key: 'metric_views', label: 'Metric view definitions' },
];

type GenEvent =
  | { type: 'progress'; stage: string; done: number; total: number }
  | { type: 'complete'; download_token: string; counts: Record<string, number> }
  | { type: 'error'; error: string; reference?: string };

type Progress = { done: number; total: number };

export default function GenerateWizard({
  config,
  model,
  active,
}: {
  config: AppConfig;
  model: string;
  active: boolean;
}) {
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([]);
  const [wsFilter] = useState<WorkspaceFilterValue>(
    config.workspace_id
      ? { mode: 'include', workspace_ids: [config.workspace_id] }
      : { mode: 'include', workspace_ids: [] }
  );
  const [catalogs, setCatalogs] = useState<CatalogInfo[]>([]);
  const [catalogsAvailable, setCatalogsAvailable] = useState(true);
  const [catalogsLoading, setCatalogsLoading] = useState(false);
  const [catFilter, setCatFilter] = useState<string[]>([]);

  const [phase, setPhase] = useState<'idle' | 'running' | 'done' | 'error'>('idle');
  const [progress, setProgress] = useState<Record<string, Progress>>({});
  const [token, setToken] = useState<string | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    apiGet<WorkspacesResponse>('/workspaces').then((r) => setWorkspaces(r.workspaces || [])).catch(() => {});
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    const include = wsFilter.mode === 'include' && wsFilter.workspace_ids.length > 0;
    const qs = include
      ? `?workspace_ids=${encodeURIComponent(wsFilter.workspace_ids.join(','))}&mode=include`
      : `?mode=${wsFilter.mode}`;
    setCatalogsLoading(true);
    apiGet<CatalogsResponse>(`/catalogs${qs}`)
      .then((r) => { setCatalogs(r.catalogs || []); setCatalogsAvailable(r.available); })
      .catch(() => { setCatalogs([]); setCatalogsAvailable(false); })
      .finally(() => setCatalogsLoading(false));
  }, [wsFilter]);

  const scopedWorkspaceName =
    (workspaces.find((w) => w.is_current) || workspaces.find((w) => w.id === wsFilter.workspace_ids[0]))?.name ||
    wsFilter.workspace_ids[0] || null;

  async function run() {
    if (phase === 'running') return;
    setPhase('running');
    setError(null);
    setToken(null);
    setCounts({});
    setProgress({});
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      for await (const ev of streamPostEvents<GenEvent>(
        '/generate/stream',
        { workspace_filter: wsFilter, catalogs: catFilter },
        model,
        controller.signal
      )) {
        if (ev.type === 'progress') {
          setProgress((p) => ({ ...p, [ev.stage]: { done: ev.done, total: ev.total } }));
        } else if (ev.type === 'complete') {
          setToken(ev.download_token);
          setCounts(ev.counts || {});
          setPhase('done');
        } else if (ev.type === 'error') {
          setError(ev.error);
          setPhase('error');
        }
      }
      // Stream ended without a complete/error event.
      setPhase((prev) => (prev === 'running' ? 'idle' : prev));
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setError((e as Error).message);
        setPhase('error');
      }
    } finally {
      abortRef.current = null;
    }
  }

  const running = phase === 'running';
  const scopedWorkspaceLabel = (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 bg-gray-50 px-2.5 py-1.5 text-xs text-ink-600 max-w-[240px]">
      <Server size={13} className="text-ink-400 shrink-0" />
      <span className="truncate">Scoped to <span className="font-medium text-ink-800">{scopedWorkspaceName || 'the deployed workspace'}</span></span>
    </span>
  );

  return (
    <div className="min-w-0 max-w-4xl mx-auto space-y-6">
      <div className="card p-8">
        <div className="text-center">
          <div className="w-12 h-12 rounded-xl bg-databricks-50 flex items-center justify-center mx-auto mb-4">
            <Wand2 size={24} className="text-databricks-500" />
          </div>
          <h2 className="text-xl font-bold text-ink-900">Generate the missing metadata</h2>
          <p className="text-sm text-ink-600 mt-2 leading-relaxed">
            Uses your workspace's own Foundation Model API (model: <span className="font-medium">{model}</span>) to draft
            the descriptions, tags, column comments, Genie-agent instructions and metric-view definitions your estate is
            missing — plus ready-to-run primary/foreign-key statements. Everything downloads as an Excel workbook for
            review. <span className="font-medium">Nothing is applied to Unity Catalog.</span>
          </p>
        </div>

        <div className="mt-6 rounded-lg bg-databricks-50 border border-databricks-100 p-4 text-left">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold text-ink-900 mb-1.5">
            <Sparkles size={15} className="text-databricks-500" /> What you'll get
          </h3>
          <p className="text-sm text-ink-700 leading-relaxed">
            One workbook with a tab per artifact — <span className="font-medium">Catalog, Schema, Entity,
            Relationship_PrimaryKey, Relationship_ForeignKey, GenieAgent, MetricViews</span> — each row a suggested fix
            you can review and apply. PK/FK relationships are proposed heuristically; review before applying.
          </p>
        </div>

        <div className="mt-6 flex flex-col items-center gap-3">
          <div className="flex flex-wrap items-center justify-center gap-2">
            {scopedWorkspaceLabel}
            <CatalogFilter
              catalogs={catalogs}
              available={catalogsAvailable}
              loading={catalogsLoading}
              value={catFilter}
              onChange={setCatFilter}
              disabled={running}
            />
          </div>
          <p className="text-xs text-ink-400">
            Tip: scope to a catalog or schema for a focused, faster run. A very wide scope is capped and may truncate.
          </p>
          <button onClick={run} disabled={running} className="btn-primary inline-flex items-center gap-2">
            {running ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
            {running ? 'Generating…' : 'Generate metadata'}
          </button>
        </div>
      </div>

      {(running || phase === 'done') && (
        <div className="card p-6">
          <h3 className="text-sm font-semibold text-ink-700 mb-3">Generation progress</h3>
          <ul className="space-y-2">
            {STAGES.map((st) => {
              const pr = progress[st.key];
              const complete = pr && pr.total >= 0 && pr.done >= pr.total && (phase === 'done' || pr.total > 0);
              return (
                <li key={st.key} className="flex items-center gap-3 text-sm">
                  {phase === 'done' || complete ? (
                    <CheckCircle2 size={16} className="text-emerald-500 shrink-0" />
                  ) : pr ? (
                    <Loader2 size={16} className="animate-spin text-databricks-500 shrink-0" />
                  ) : (
                    <span className="w-4 h-4 rounded-full border border-gray-300 shrink-0" />
                  )}
                  <span className="flex-1 text-ink-700">{st.label}</span>
                  {pr && <span className="text-xs text-ink-400 tabular-nums">{pr.done}/{pr.total}</span>}
                </li>
              );
            })}
          </ul>

          {phase === 'done' && token && (
            <div className="mt-5 rounded-lg border border-emerald-200 bg-emerald-50 p-4 flex flex-col sm:flex-row sm:items-center gap-3">
              <CheckCircle2 size={18} className="text-emerald-600 shrink-0" />
              <div className="flex-1 text-sm text-ink-700">
                Generated{' '}
                {[
                  counts.catalog && `${counts.catalog} catalog`,
                  counts.schema && `${counts.schema} schema`,
                  counts.entity && `${counts.entity} entity`,
                  (counts.relationship_pk || counts.relationship_fk) &&
                    `${(counts.relationship_pk || 0) + (counts.relationship_fk || 0)} relationship`,
                  counts.genie_agent && `${counts.genie_agent} agent`,
                  counts.metric_views && `${counts.metric_views} metric-view`,
                ]
                  .filter(Boolean)
                  .join(', ') || 'no'}{' '}
                row(s).
              </div>
              <button
                onClick={() => downloadPath(`/generate/excel/${token}`, 'genie-ontology-readiness-generated.xlsx')}
                className="btn-primary inline-flex items-center gap-2 text-sm shrink-0"
              >
                <Sheet size={15} /> Download Excel
              </button>
            </div>
          )}
        </div>
      )}

      {phase === 'error' && error && (
        <p className="text-sm text-red-600 flex items-center gap-1.5">
          <AlertTriangle size={14} /> {error}
        </p>
      )}
    </div>
  );
}
