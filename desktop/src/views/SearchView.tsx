import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, formatTime } from "../api";
import type {
  IndexFreshness,
  Project,
  RetrievalScope,
  SearchItem,
  SearchResponse,
  Source,
} from "../api";
import { openFile } from "../tauri";
import Icon from "../Icon";

const LIMITS = [20, 50, 100];

const TIME_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "不限时间" },
  { value: "7", label: "近 7 天" },
  { value: "30", label: "近 30 天" },
  { value: "90", label: "近 90 天" },
  { value: "365", label: "近 1 年" },
];

const SCOPE_TEXT: Record<RetrievalScope, string> = {
  project: "项目主资料",
  expanded: "项目 + 关联",
  global: "全局资料",
};

const MATCH_LABEL: Record<SearchItem["match"], string> = {
  name: "文件名",
  content: "正文",
  both: "文件名 + 正文",
  semantic: "语义",
};

interface AppliedFilters {
  limit: number;
  sourceId: string;
  ext: string;
  projectId: string;
  scope: RetrievalScope;
  mtimeDays: number | null;
}

function rangeLabel(item: SearchItem): string | null {
  if (item.range?.kind === "page") return `第 ${item.range.page} 页`;
  if (item.range?.kind === "text") {
    return `正文 ${item.range.startOffset}–${item.range.endOffset} 字`;
  }
  if (item.page != null) return `第 ${item.page} 页`;
  if (item.sheet) return `工作表 ${item.sheet}`;
  return null;
}

function itemKey(item: SearchItem): string {
  return `${item.location_id}:${item.asset_id}`;
}

function evidenceText(query: string, items: SearchItem[]): string {
  const lines = [`# 检索：${query}`, ""];
  items.forEach((item, index) => {
    lines.push(`## ${index + 1}. ${item.name}`);
    if (item.rel_path) lines.push(`- 路径：${item.rel_path}`);
    const range = rangeLabel(item);
    if (range) lines.push(`- 定位：${range}`);
    lines.push(`- 类型：${MATCH_LABEL[item.match]}${item.semantic ? " · 语义" : ""}`);
    if (item.stale) lines.push("- 注意：索引可能过期，请打开原文件确认");
    if (item.snippet) lines.push(`- 证据：${item.snippet.replace(/\s+/g, " ").trim()}`);
    lines.push("");
  });
  return lines.join("\n");
}

export default function SearchView() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [limit, setLimit] = useState(50);
  const [sourceId, setSourceId] = useState("");
  const [ext, setExt] = useState("");
  const [projectId, setProjectId] = useState("");
  const [scope, setScope] = useState<RetrievalScope>("project");
  const [mtimeDays, setMtimeDays] = useState("");
  const [showWeak, setShowWeak] = useState(false);
  const [groupFamilies, setGroupFamilies] = useState(true);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [items, setItems] = useState<SearchItem[]>([]);
  const [applied, setApplied] = useState<AppliedFilters | null>(null);
  const [copied, setCopied] = useState(false);
  const [sources, setSources] = useState<Source[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [freshness, setFreshness] = useState<IndexFreshness | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let alive = true;
    api.sources()
      .then((data) => {
        if (alive) setSources(data.items);
      })
      .catch(() => undefined);
    api.projects()
      .then((data) => {
        if (alive) setProjects(data.items);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    let alive = true;
    api.freshness(projectId || undefined)
      .then((data) => {
        if (alive) setFreshness(data);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [projectId]);

  const activeProject = projects.find((project) => project.project_id === projectId);
  const globalSelected = activeProject?.kind === "global";

  useEffect(() => {
    if (globalSelected && scope !== "global") setScope("global");
  }, [globalSelected, scope]);

  const filters = useCallback((): AppliedFilters => ({
    limit,
    sourceId: sourceId.trim(),
    ext: ext.trim(),
    projectId,
    scope: globalSelected ? "global" : scope,
    mtimeDays: mtimeDays ? Number(mtimeDays) : null,
  }), [limit, sourceId, ext, projectId, scope, globalSelected, mtimeDays]);

  const runSearch = useCallback(async (text: string) => {
    const value = text.trim();
    if (!value) return;
    const next = filters();
    setLoading(true);
    setError(null);
    setCopied(false);
    try {
      const data = await api.search(value, {
        limit: next.limit,
        sourceId: next.sourceId || undefined,
        ext: next.ext || undefined,
        projectId: next.projectId || undefined,
        scope: next.projectId ? next.scope : undefined,
        mtimeDays: next.mtimeDays ?? undefined,
      });
      setSubmitted(value);
      setResult(data);
      setItems(data.items);
      setApplied(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [filters]);

  const loadMore = useCallback(async () => {
    if (!applied || !submitted || loadingMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const data = await api.search(submitted, {
        limit: applied.limit,
        offset: items.length,
        sourceId: applied.sourceId || undefined,
        ext: applied.ext || undefined,
        projectId: applied.projectId || undefined,
        scope: applied.projectId ? applied.scope : undefined,
        mtimeDays: applied.mtimeDays ?? undefined,
      });
      const known = new Set(items.map(itemKey));
      const fresh = data.items.filter((item) => !known.has(itemKey(item)));
      setItems((current) => [...current, ...fresh]);
      setResult((current) => (current ? { ...current, stale: current.stale || data.stale } : data));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingMore(false);
    }
  }, [applied, submitted, items, loadingMore]);

  const copyEvidence = useCallback(async () => {
    if (!result || items.length === 0) return;
    const text = evidenceText(submitted, items);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      window.alert(text);
    }
  }, [result, items, submitted]);

  const visible = useMemo(
    () => (showWeak ? items : items.filter((item) => !item.weak)),
    [items, showWeak],
  );
  const hiddenWeak = items.length - visible.length;
  const canLoadMore = Boolean(result && items.length < result.total);
  const freshnessNotes = freshness
    ? [
      freshness.stale_files ? `${freshness.stale_files} 个文件已改动待重新索引` : "",
      freshness.pending_jobs ? `${freshness.pending_jobs} 个解析任务待处理` : "",
      freshness.failed_jobs ? `${freshness.failed_jobs} 个任务失败` : "",
    ].filter(Boolean)
    : [];

  const groups = useMemo(() => {
    if (!groupFamilies) {
      return visible.map((item) => ({ key: itemKey(item), primary: item, members: [item] }));
    }
    const map = new Map<string, { key: string; primary: SearchItem; members: SearchItem[] }>();
    for (const item of visible) {
      const key = item.family_id ?? itemKey(item);
      const existing = map.get(key);
      if (existing) {
        existing.members.push(item);
      } else {
        map.set(key, { key, primary: item, members: [item] });
      }
    }
    return [...map.values()];
  }, [visible, groupFamilies]);

  return (
    <div className="view search-workbench">
      <header className="view-head view-head-stack">
        <div>
          <p className="eyebrow">检索工作台</p>
          <h1>检索</h1>
          <p className="sub">
            直接查看命中的原始段落、页码与证据区间，每条结果都能打开原文件核对。
            需要归纳成回答时再切到「对话」。
          </p>
        </div>
      </header>

      <section className="panel search-panel">
        <form
          className="search-form"
          onSubmit={(event) => {
            event.preventDefault();
            void runSearch(query);
          }}
        >
          <div className="search-input-row">
            <Icon name="search" size={18} />
            <input
              ref={inputRef}
              type="search"
              className="search-input"
              aria-label="检索词"
              placeholder="搜索文件名、正文或语义…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button className="primary" type="submit" disabled={loading || !query.trim()}>
              {loading ? "检索中…" : "检索"}
            </button>
          </div>

          <div className="search-filters">
            <label>
              项目
              <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
                <option value="">不限定项目（全局）</option>
                {projects.map((project) => (
                  <option key={project.project_id} value={project.project_id}>
                    {project.kind === "global" ? `${project.name}（全部资料）` : project.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              范围
              <select
                value={globalSelected ? "global" : scope}
                disabled={!projectId || globalSelected}
                aria-label="检索范围"
                onChange={(event) => setScope(event.target.value as RetrievalScope)}
              >
                {Object.entries(SCOPE_TEXT).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              数据源
              <select value={sourceId} onChange={(event) => setSourceId(event.target.value)}>
                <option value="">全部数据源</option>
                {sources.map((source) => (
                  <option key={source.source_id} value={source.source_id}>
                    {source.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              类型
              <input
                type="text"
                value={ext}
                placeholder="如 pdf"
                aria-label="文件类型"
                onChange={(event) => setExt(event.target.value)}
              />
            </label>
            <label>
              时间
              <select
                value={mtimeDays}
                aria-label="时间范围"
                onChange={(event) => setMtimeDays(event.target.value)}
              >
                {TIME_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              数量
              <select
                value={limit}
                aria-label="结果数量"
                onChange={(event) => setLimit(Number(event.target.value))}
              >
                {LIMITS.map((value) => (
                  <option key={value} value={value}>
                    {value} 条
                  </option>
                ))}
              </select>
            </label>
            <label className="search-check">
              <input
                type="checkbox"
                checked={showWeak}
                onChange={(event) => setShowWeak(event.target.checked)}
              />
              显示弱相关
            </label>
            <label className="search-check">
              <input
                type="checkbox"
                checked={groupFamilies}
                onChange={(event) => setGroupFamilies(event.target.checked)}
              />
              按文档家族归并
            </label>
          </div>
        </form>
      </section>

      {freshness?.stale && (
        <div className="alert warn">
          索引未完全同步：{freshnessNotes.join("，")}。当前结果可能缺少最新内容。
        </div>
      )}

      {error && <div className="alert error">{error}</div>}

      {result && (
        <section className="view-section" aria-label="检索结果">
          <div className="search-meta">
            <span>
              「{submitted}」共 {result.total} 条 · 强相关 {result.strong} 条 ·
              正文命中 {result.content_hits} 段
            </span>
            <span className="chip subtle">
              {result.vector_used ? "全文 + 语义" : "仅全文"}
            </span>
            {result.vector_model && <span className="chip subtle">{result.vector_model}</span>}
            {result.truncated && <span className="chip warn">候选已截断</span>}
            {result.rewritten && <span className="chip subtle">改写：{result.rewritten}</span>}
            {hiddenWeak > 0 && (
              <span className="chip subtle">已折叠 {hiddenWeak} 条弱相关</span>
            )}
            {groupFamilies && groups.length < visible.length && (
              <span className="chip subtle">
                归并为 {groups.length} 个文档家族
              </span>
            )}
            <button
              className="ghost small icon-label"
              type="button"
              disabled={items.length === 0}
              onClick={() => void copyEvidence()}
            >
              <Icon name="copy" size={14} />
              {copied ? "已复制" : "复制结构化证据"}
            </button>
          </div>

          {result.vector_error && <div className="alert warn">语义检索不可用：{result.vector_error}</div>}
          {result.stale && (
            <div className="alert warn">
              部分结果对应的文件在索引后已更新或暂不可用，引用前请打开原文件确认。
            </div>
          )}

          {visible.length === 0 ? (
            <div className="empty">
              <div className="empty-title">
                {result.total === 0 ? "没有找到匹配内容" : "全部是弱相关结果"}
              </div>
              <p>
                {result.total === 0
                  ? "换个说法，或去掉项目、数据源与类型限制再试一次。"
                  : "这些结果与问题的相关度较低，勾选「显示弱相关」后再查看。"}
              </p>
            </div>
          ) : (
            <ol className="search-results">
              {groups.map((group) => {
                const item = group.primary;
                const range = rangeLabel(item);
                return (
                  <li
                    key={group.key}
                    className={`search-result${item.weak ? " weak" : ""}`}
                  >
                    <div className="search-result-head">
                      <span className="search-result-name">{item.name}</span>
                      <span className="chip subtle">{MATCH_LABEL[item.match]}</span>
                      {range && <span className="chip subtle">{range}</span>}
                      {item.weak && <span className="chip warn">弱相关</span>}
                      {item.stale && (
                        <span
                          className="chip warn"
                          title="文件在索引后已更新或暂不可用，建议重新索引"
                        >
                          索引可能过期
                        </span>
                      )}
                      {item.semantic && <span className="chip accent">语义</span>}
                      {item.version_label && (
                        <span className="chip subtle">{item.version_label}</span>
                      )}
                      {(item.family_size ?? 1) > 1 && (
                        <span className="chip subtle">
                          同族 {item.family_size} 个版本
                          {group.members.length > 1 ? ` · 命中 ${group.members.length} 段` : ""}
                        </span>
                      )}
                      <span className="search-result-actions">
                        <span className="muted small-text">
                          {item.source_label}
                          {item.mtime ? ` · ${formatTime(item.mtime)}` : ""}
                        </span>
                        <button
                          className="ghost small icon-label"
                          type="button"
                          onClick={() => void openFile(item.path)}
                        >
                          <Icon name="arrow-up-right" size={14} />
                          打开
                        </button>
                      </span>
                    </div>
                    {item.snippet && <p className="search-snippet">{item.snippet}</p>}
                    <div className="search-result-path">{item.rel_path || item.path}</div>
                    {group.members.length > 1 && (
                      <ul className="search-result-members">
                        {group.members.slice(1).map((member) => (
                          <li key={itemKey(member)}>
                            <button
                              className="link-button"
                              type="button"
                              onClick={() => void openFile(member.path)}
                            >
                              {member.name}
                            </button>
                            <span className="muted small-text">
                              {rangeLabel(member) ?? ""}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {item.family_versions && item.family_versions.length > 1 && (
                      <div className="search-result-family">
                        关联版本：
                        {item.family_versions
                          .slice(0, 6)
                          .map((version) => (
                            <button
                              key={`${version.asset_id}-${version.version_label}`}
                              className="link-button"
                              type="button"
                              disabled={!version.path}
                              onClick={() => version.path && void openFile(version.path)}
                            >
                              {version.name ?? version.path ?? "—"}
                              {version.version_label ? ` · ${version.version_label}` : ""}
                            </button>
                          ))}
                        {item.family_versions.length > 6
                          ? ` 等 ${item.family_versions.length} 个`
                          : ""}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          )}

          {canLoadMore && (
            <div className="search-more">
              <button
                className="ghost"
                type="button"
                disabled={loadingMore}
                onClick={() => void loadMore()}
              >
                {loadingMore ? "加载中…" : `加载更多（已显示 ${items.length} / ${result.total}）`}
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
