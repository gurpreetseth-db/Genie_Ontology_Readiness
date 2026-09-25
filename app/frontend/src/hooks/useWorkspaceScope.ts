// Shared workspace + catalog scope, selected ONCE and used by every tab (Assess,
// Plan — implicitly, via the scorecard it's handed — and Generate). Previously
// Scorecard and GenerateWizard each held their own copy of this state and fetched
// /workspaces + /catalogs independently, so picking catalogs on one tab had no
// effect on the other. Lifting it here (owned by App, passed down as props) makes
// it one selection that both consumers read and write.
import { useEffect, useMemo, useState } from 'react';
import { apiGet } from './useApi';
import type {
  AppConfig,
  CatalogInfo,
  CatalogsResponse,
  WorkspaceFilterValue,
  WorkspaceInfo,
  WorkspacesResponse,
} from '../types';

export function useWorkspaceScope(config: AppConfig) {
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([]);
  const [wsFilter, setWsFilter] = useState<WorkspaceFilterValue>(
    config.workspace_id
      ? { mode: 'include', workspace_ids: [config.workspace_id] }
      : { mode: 'include', workspace_ids: [] }
  );
  const [catalogs, setCatalogs] = useState<CatalogInfo[]>([]);
  const [catalogsAvailable, setCatalogsAvailable] = useState(true);
  const [catalogsLoading, setCatalogsLoading] = useState(false);
  const [catFilter, setCatFilter] = useState<string[]>([]);

  useEffect(() => {
    apiGet<WorkspacesResponse>('/workspaces')
      .then((r) => {
        setWorkspaces(r.workspaces || []);
        // Seed the default selection to the current workspace if the config didn't.
        if (!config.workspace_id && r.current_workspace_id) {
          setWsFilter({ mode: 'include', workspace_ids: [r.current_workspace_id] });
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refetch the catalog options whenever the workspace selection changes, so the
  // catalog filter always reflects the catalogs bound to the chosen workspaces.
  useEffect(() => {
    const include = wsFilter.mode === 'include' && wsFilter.workspace_ids.length > 0;
    const qs = include
      ? `?workspace_ids=${encodeURIComponent(wsFilter.workspace_ids.join(','))}&mode=include`
      : `?mode=${wsFilter.mode}`;
    setCatalogsLoading(true);
    apiGet<CatalogsResponse>(`/catalogs${qs}`)
      .then((r) => {
        setCatalogs(r.catalogs || []);
        setCatalogsAvailable(r.available);
        setCatFilter([]); // reset to no filter (empty = assess all) for the new workspace scope
      })
      .catch(() => {
        setCatalogs([]);
        setCatalogsAvailable(false);
      })
      .finally(() => setCatalogsLoading(false));
  }, [wsFilter]);

  // The workspace the assessment is scoped to (the deployed workspace, today —
  // there is no multi-workspace picker in the UI, just this read-only label).
  const scopedWorkspaceName = useMemo(() => {
    const id = wsFilter.workspace_ids[0];
    const w = workspaces.find((ws) => ws.is_current) || (id ? workspaces.find((ws) => ws.id === id) : undefined);
    return w?.name || id || null;
  }, [workspaces, wsFilter]);

  return {
    workspaces,
    wsFilter,
    setWsFilter,
    catalogs,
    catalogsAvailable,
    catalogsLoading,
    catFilter,
    setCatFilter,
    scopedWorkspaceName,
  };
}

export type WorkspaceScope = ReturnType<typeof useWorkspaceScope>;
