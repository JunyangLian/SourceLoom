import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import styles from "./styles.css?raw";

const mocks = vi.hoisted(() => ({
  health: vi.fn(),
  stats: vi.fn(),
  sources: vi.fn(),
  search: vi.fn(),
  freshness: vi.fn(),
  addSource: vi.fn(),
  scan: vi.fn(),
  ocr: vi.fn(),
  setState: vi.fn(),
  removeSource: vi.fn(),
  chatSessions: vi.fn(),
  chatSession: vi.fn(),
  createChatSession: vi.fn(),
  ask: vi.fn(),
  askStream: vi.fn(),
  renameChatSession: vi.fn(),
  deleteChatSession: vi.fn(),
  answerSettings: vi.fn(),
  saveAnswerSettings: vi.fn(),
  saveIngestSettings: vi.fn(),
  embeddingSettings: vi.fn(),
  saveEmbeddingSettings: vi.fn(),
  querySettings: vi.fn(),
  saveQuerySettings: vi.fn(),
  ocrSettings: vi.fn(),
  saveOcrSettings: vi.fn(),
  events: vi.fn(),
  jobs: vi.fn(),
  maintenance: vi.fn(),
  retryJob: vi.fn(),
  externalClients: vi.fn(),
  inventoryFileTypes: vi.fn(),
  sourceParseResults: vi.fn(),
  documentText: vi.fn(),
  updatesOverview: vi.fn(),
  checkUpdates: vi.fn(),
  rebuildFamilies: vi.fn(),
  documentFamilies: vi.fn(),
  documentFamily: vi.fn(),
  documentFamilyMembers: vi.fn(),
  familyMembersByAsset: vi.fn(),
  familyDiff: vi.fn(),
  projects: vi.fn(),
  project: vi.fn(),
  createProject: vi.fn(),
  updateProject: vi.fn(),
  deleteProject: vi.fn(),
  projectAssets: vi.fn(),
  projectKnowledge: vi.fn(),
  addProjectSource: vi.fn(),
  removeProjectSource: vi.fn(),
  addProjectAssets: vi.fn(),
}));

vi.mock("./api", () => ({
  api: mocks,
  formatTime: (value: string | null) => value ?? "—",
  formatSize: (value: number) => `${value} B`,
}));

vi.mock("./tauri", () => ({
  openFile: vi.fn(),
  pickDirectory: vi.fn(),
}));

import App from "./App";

const STATS = {
  files: { available: 4, missing: 0, offline: 0, stale: 1 },
  by_ext: [{ ext: "md", count: 4 }],
  sources: [
    {
      source_id: "src_1",
      label: "资料",
      root_uri: "D:\\资料",
      state: "active",
      health: "ok",
      last_scan_at: "2026-11-22T10:00:00+08:00",
      available: 4,
      stale: 1,
    },
  ],
  coverage: {
    directory_level: "L0",
    directory_indexed: 4,
    content_level: "L1",
    content_indexed: 2,
    content_pending: 0,
    content_empty: 0,
    content_failed: 0,
    content_unsupported: 0,
    low_quality: 0,
    ocr_indexed: 0,
    ocr_empty: 0,
    ocr_failed: 0,
    ocr_pending: 0,
    mixed_pdf: 0,
    ocr_policy: "auto",
    ocr_available: true,
    ocr_engine: "auto",
    ocr_languages: ["zh-Hans-CN"],
    ocr_message: null,
  },
  maintenance: {
    jobs: {},
    chunks: 10,
    artifacts: 4,
    db_bytes: 2048,
    running: {
      running: false,
      kind: null,
      source_id: null,
      started_at: null,
      message: null,
    },
    budget: { date: "2026-11-22", l1_used: 1, l1_limit: 500, l2_used: 0, l2_limit: 200 },
    vectors: 0,
    embedding: {
      provider: "none",
      base_url: "",
      model: "",
      allow_cloud: false,
      top_k: 6,
      has_key: false,
    },
  },
};

const UPDATES = {
  auto_parse: true,
  paused: false,
  profile: "balanced" as const,
  watcher_running: true,
  watch_enabled: true,
  last_sync_at: "2026-11-22T10:00:00+08:00",
  summary: {
    pending_l1: 2,
    pending_l2: 1,
    parsed_24h: 5,
    failed: 0,
    sources: 2,
    awaiting_policy: 0,
  },
  recent: [
    {
      name: "报告.md",
      path: "D:\\资料\\报告.md",
      source_id: "src_1",
      source_label: "资料",
      stage: "L1",
      state: "ok",
      quality: "good",
      chars: 12,
      created_at: "2026-11-22T10:00:00+08:00",
    },
  ],
  failures: [],
  generated_at: "2026-11-22T10:01:00+08:00",
};

const GLOBAL_PROJECT = {
  project_id: "prj_global",
  name: "全局项目",
  description: "数据页录入的文件夹自动归入全局项目",
  status: "active",
  kind: "global",
  retrieval_policy: "global",
  privacy_policy: "external_allowed",
  created_at: "2026-11-22T10:00:00+08:00",
  updated_at: "2026-11-22T10:00:00+08:00",
  stats: {
    assets: 0, primary: 0, linked: 0, excluded: 0, usable: 0, parsed: 0,
    embedded: 0, failed: 0, pending: 0, sources: 0, last_updated: null,
  },
  sources: [],
  source_count: 0,
  primary_count: 0,
  linked_count: 0,
  excluded_count: 0,
};

const SOURCE_CARD = {
  source_id: "src_1",
  root_uri: "D:\\资料",
  label: "资料",
  state: "active" as const,
  health: "ok",
  last_scan_at: null,
  l0_completed_at: null,
  excludes: "[]",
  stats: {
    counts: {},
    available: 4,
    stale: 0,
    missing: 0,
    offline: 0,
    last_run: null,
  },
  parse: { total: 10, parsed: 6, failed: 1, empty: 1, pending: 2, locked: 0, unsupported: 0 },
  scanning: false,
};

beforeEach(() => {
  window.localStorage.clear();
  mocks.health.mockResolvedValue({ status: "ok", version: "0.1.0", db: "test.db" });
  mocks.stats.mockResolvedValue(STATS);
  mocks.sources.mockResolvedValue({ items: [] });
  mocks.search.mockResolvedValue({
    query: "",
    total: 0,
    strong: 0,
    items: [],
    index_level: "L1",
    content_hits: 0,
    truncated: false,
    stale: false,
    rewritten: null,
    vector_used: false,
    vector_model: null,
    vector_error: null,
  });
  mocks.freshness.mockResolvedValue({
    stale: false,
    stale_files: 0,
    pending_jobs: 0,
    failed_jobs: 0,
    sources: [],
  });
  mocks.chatSessions.mockResolvedValue({ items: [] });
  mocks.answerSettings.mockResolvedValue({
    provider: "extractive",
    base_url: "",
    model: "",
    allow_cloud: false,
    top_k: 6,
    has_key: false,
  });
  mocks.embeddingSettings.mockResolvedValue({
    provider: "none",
    base_url: "",
    model: "",
    allow_cloud: false,
    top_k: 6,
    has_key: false,
  });
  mocks.querySettings.mockResolvedValue({ custom: [], builtin_groups: 3 });
  mocks.ocrSettings.mockResolvedValue({ policy: "auto", engines: [] });
  mocks.events.mockResolvedValue({ items: [] });
  mocks.jobs.mockResolvedValue({ items: [] });
  mocks.externalClients.mockResolvedValue({ items: [] });
  mocks.updatesOverview.mockResolvedValue(UPDATES);
  mocks.checkUpdates.mockResolvedValue({ started: [] });
  mocks.documentFamilies.mockResolvedValue({ items: [] });
  mocks.projects.mockResolvedValue({ items: [GLOBAL_PROJECT], total: 1 });
  mocks.project.mockResolvedValue(GLOBAL_PROJECT);
  mocks.projectAssets.mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
  mocks.projectKnowledge.mockResolvedValue({
    summaries: [], entities: [], relations: [], questions: [],
  });
  mocks.askStream.mockImplementation(async (question, options, handlers) => {
    handlers.onStatus?.("retrieval", "正在检索本地资料…");
    const result = await mocks.ask(question, options);
    if (result) {
      handlers.onDelta?.(result.answer);
      handlers.onDone?.(result);
    }
  });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function renderApp() {
  const utils = render(<App />);
  await settle();
  return utils;
}

async function renderChat() {
  const utils = await renderApp();
  fireEvent.click(screen.getByRole("button", { name: /^对话/ }));
  await settle();
  return utils;
}

describe("页面整合", () => {
  it("主导航包含检索与四个独立模块", async () => {
    const { container } = await renderApp();
    const labels = [...container.querySelectorAll(".nav .nav-label")].map(
      (element) => element.textContent,
    );
    expect(labels).toEqual(["检索", "数据", "项目", "对话", "更新"]);
  });

  it("项目页展示全局项目与项目内外部接口", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^项目/ }));
    await settle();
    expect(await screen.findByRole("heading", { name: "项目" })).toBeTruthy();
    expect((await screen.findAllByText("全局项目")).length).toBeGreaterThan(0);
    expect(await screen.findByText("外部接口")).toBeTruthy();
    expect(await screen.findByText(/这里只读展示/)).toBeTruthy();
  });

  it("默认进入检索工作台", async () => {
    await renderApp();

    expect(await screen.findByRole("heading", { name: "检索" })).toBeTruthy();
    expect(await screen.findByPlaceholderText("搜索文件名、正文或语义…")).toBeTruthy();
    expect(screen.getByRole("button", { name: "检索" })).toBeTruthy();
  });

  it("检索工作台提示索引落后", async () => {
    mocks.freshness.mockResolvedValue({
      stale: true,
      stale_files: 3,
      pending_jobs: 2,
      failed_jobs: 1,
      sources: [],
    });

    await renderApp();

    expect(await screen.findByText(/索引未完全同步：3 个文件/)).toBeTruthy();
    expect(screen.getByText(/2 个解析任务待处理/)).toBeTruthy();
    expect(screen.getByText(/1 个任务失败/)).toBeTruthy();
  });

  it("检索工作台展示证据区间与过期标记", async () => {
    mocks.search.mockResolvedValue({
      query: "样本量",
      total: 2,
      strong: 1,
      items: [
        {
          location_id: "loc_1",
          asset_id: "ast_1",
          name: "报告.md",
          path: "D:\\资料\\报告.md",
          directory: "D:\\资料",
          rel_path: "资料/报告.md",
          ext: "md",
          size: 12,
          mtime: "2026-11-22T10:00:00+08:00",
          availability: "AVAILABLE",
          index_level: "L1",
          semantic: false,
          match: "content",
          snippet: "样本量 42",
          page: 3,
          sheet: null,
          bbox: null,
          range: { kind: "page", page: 3 },
          stale: true,
          source_id: "src_1",
          source_label: "资料",
          weak: false,
        },
        {
          location_id: "loc_2",
          asset_id: "ast_2",
          name: "草稿.md",
          path: "D:\\资料\\草稿.md",
          directory: "D:\\资料",
          rel_path: "资料/草稿.md",
          ext: "md",
          size: 8,
          mtime: null,
          availability: "AVAILABLE",
          index_level: "L1",
          semantic: false,
          match: "name",
          snippet: "样本量草稿",
          page: null,
          sheet: null,
          bbox: null,
          range: null,
          stale: false,
          source_id: "src_1",
          source_label: "资料",
          weak: true,
        },
      ],
      index_level: "L1",
      content_hits: 1,
      truncated: false,
      stale: true,
      rewritten: null,
      vector_used: true,
      vector_model: "bge-small",
      vector_error: null,
    });

    await renderApp();
    fireEvent.change(screen.getByPlaceholderText("搜索文件名、正文或语义…"), {
      target: { value: "样本量" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await settle();

    expect(await screen.findByText("报告.md")).toBeTruthy();
    expect(screen.getByText("第 3 页")).toBeTruthy();
    expect(screen.getByText("索引可能过期")).toBeTruthy();
    expect(screen.getByText(/引用前请打开原文件确认/)).toBeTruthy();
    expect(screen.getByText(/已折叠 1 条弱相关/)).toBeTruthy();
    expect(screen.queryByText("草稿.md")).toBeNull();
    expect(mocks.search).toHaveBeenCalledWith("样本量", {
      limit: 50,
      sourceId: undefined,
      ext: undefined,
      projectId: undefined,
      scope: undefined,
      mtimeDays: undefined,
    });
  });

  it("检索工作台支持时间过滤与按需加载更多", async () => {
    const base = {
      index_level: "L1",
      content_hits: 1,
      truncated: false,
      stale: false,
      rewritten: null,
      vector_used: false,
      vector_model: null,
      vector_error: null,
    };
    const first = {
      location_id: "loc_1",
      asset_id: "ast_1",
      name: "报告一.md",
      path: "D:\\资料\\报告一.md",
      directory: "D:\\资料",
      rel_path: "资料/报告一.md",
      ext: "md",
      size: 12,
      mtime: "2026-11-22T10:00:00+08:00",
      availability: "AVAILABLE",
      index_level: "L1",
      semantic: false,
      match: "content" as const,
      snippet: "第一段证据",
      page: null,
      sheet: null,
      bbox: null,
      range: { kind: "text" as const, startOffset: 10, endOffset: 20 },
      stale: false,
      source_id: "src_1",
      source_label: "资料",
      weak: false,
    };
    const second = {
      ...first,
      location_id: "loc_2",
      asset_id: "ast_2",
      name: "报告二.md",
      path: "D:\\资料\\报告二.md",
      rel_path: "资料/报告二.md",
      snippet: "第二段证据",
    };
    mocks.search
      .mockResolvedValueOnce({ ...base, query: "样本量", total: 2, strong: 2, items: [first] })
      .mockResolvedValueOnce({ ...base, query: "样本量", total: 2, strong: 2, items: [second] });

    await renderApp();
    fireEvent.change(screen.getByPlaceholderText("搜索文件名、正文或语义…"), {
      target: { value: "样本量" },
    });
    fireEvent.change(screen.getByLabelText("时间范围"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await settle();

    expect(await screen.findByText("报告一.md")).toBeTruthy();
    expect(screen.getByText("正文 10–20 字")).toBeTruthy();
    expect(mocks.search).toHaveBeenLastCalledWith("样本量", {
      limit: 50,
      sourceId: undefined,
      ext: undefined,
      projectId: undefined,
      scope: undefined,
      mtimeDays: 30,
    });

    fireEvent.click(await screen.findByRole("button", { name: /加载更多/ }));
    await settle();

    expect(await screen.findByText("报告二.md")).toBeTruthy();
    expect(mocks.search).toHaveBeenLastCalledWith("样本量", {
      limit: 50,
      offset: 1,
      sourceId: undefined,
      ext: undefined,
      projectId: undefined,
      scope: undefined,
      mtimeDays: 30,
    });
  });

  it("数据页直接展示数据源", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^数据/ }));
    await settle();

    expect(await screen.findByRole("heading", { name: "数据" })).toBeTruthy();
    expect(await screen.findByText("添加文件夹")).toBeTruthy();
  });

  it("数据页展示文档家族并进入版本对比", async () => {
    mocks.documentFamilies.mockResolvedValue({
      items: [
        {
          family_id: "fam_1",
          title: "毕业论文",
          confidence: "high",
          size: 2,
          members: [
            { asset_id: "ast_1", version_label: "初稿", similarity: 1, path: "D:\\论文初稿.md" },
            { asset_id: "ast_2", version_label: "终稿", similarity: 0.9, path: "D:\\论文终稿.md" },
          ],
        },
      ],
    });
    mocks.documentFamily.mockResolvedValue({
      family: {
        family_id: "fam_1",
        title: "毕业论文",
        confidence: "high",
        rules_version: "family.v1",
        created_at: "2026-11-22T10:00:00+08:00",
        updated_at: "2026-11-22T10:00:00+08:00",
      },
      members: [
        {
          asset_id: "ast_1",
          location_id: "loc_1",
          path: "D:\\论文初稿.md",
          name: "论文初稿.md",
          rel_path: "论文初稿.md",
          version_label: "初稿",
        },
        {
          asset_id: "ast_2",
          location_id: "loc_2",
          path: "D:\\论文终稿.md",
          name: "论文终稿.md",
          rel_path: "论文终稿.md",
          version_label: "终稿",
        },
      ],
      content_groups: [],
    });
    mocks.familyDiff.mockResolvedValue({
      family_id: "fam_1",
      family_title: "毕业论文",
      left: { asset_id: "ast_1", name: "论文初稿.md", path: "D:\\论文初稿.md", version_label: "初稿" },
      right: { asset_id: "ast_2", name: "论文终稿.md", path: "D:\\论文终稿.md", version_label: "终稿" },
      summary: { left_groups: 1, right_groups: 1, shared: 1, removed: 0, added: 0, modified: 0 },
      shared: [
        {
          content_group_id: "fcg_1",
          text: "研究背景",
          sources: [
            {
              asset_id: "ast_1",
              name: "论文初稿.md",
              path: "D:\\论文初稿.md",
              version_label: "初稿",
            },
          ],
        },
      ],
      removed: [],
      added: [],
      modified: [],
      truncated: { shared: false, removed: false, added: false, modified: false },
    });

    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^数据/ }));
    await settle();

    expect(await screen.findByText("毕业论文")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
    await settle();

    expect(await screen.findByText("版本成员")).toBeTruthy();
    expect(await screen.findByText("版本差异")).toBeTruthy();
    await waitFor(() =>
      expect(mocks.familyDiff).toHaveBeenCalledWith("fam_1", "ast_1", "ast_2"),
    );
  });

  it("深度解析空闲时进入盘点与策略页", async () => {
    mocks.sources.mockResolvedValue({ items: [SOURCE_CARD] });
    mocks.inventoryFileTypes.mockResolvedValue({
      items: [],
      policies: {},
      confirmed: false,
      total_files: 0,
      total_bytes: 0,
      effective_files: 0,
      effective_bytes: 0,
      system_excluded_files: 0,
      system_excluded_bytes: 0,
      generated_at: "2026-11-22T10:00:00+08:00",
      rules_version: "inventory.v1",
    });
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^数据/ }));
    await settle();

    fireEvent.click(await screen.findByRole("button", { name: "深度解析" }));
    expect(await screen.findByText("盘点与策略")).toBeTruthy();
    expect(await screen.findByText(/返回数据源/)).toBeTruthy();
  });

  it("解析中悬停变为暂停并暂停数据源", async () => {
    mocks.sources.mockResolvedValue({
      items: [{ ...SOURCE_CARD, scanning: true }],
    });
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^数据/ }));
    await settle();

    const button = await screen.findByRole("button", { name: "解析中" });
    fireEvent.mouseEnter(button);
    const stop = await screen.findByRole("button", { name: "暂停解析" });
    fireEvent.click(stop);
    await waitFor(() => expect(mocks.setState).toHaveBeenCalledWith("src_1", "paused"));
  });

  it("深度解析进度条显示在扫描图标左边", async () => {
    mocks.sources.mockResolvedValue({ items: [SOURCE_CARD] });
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^数据/ }));
    await settle();

    const bar = await screen.findByRole("progressbar", { name: "深度解析进度" });
    expect(bar.getAttribute("aria-valuenow")).toBe("80");
    expect(bar.textContent).toContain("80%");
    const scan = screen.getByRole("button", { name: "扫描" });
    expect(bar.compareDocumentPosition(scan) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("解析结果按钮打开该数据源的结果页", async () => {
    mocks.sources.mockResolvedValue({ items: [SOURCE_CARD] });
    mocks.sourceParseResults.mockResolvedValue({
      source_id: "src_1",
      summary: { total: 2, parsed: 1, failed: 1, empty: 0, pending: 0, locked: 0, unsupported: 0 },
      items: [
        {
          location_id: "loc_1",
          version_id: "ver_1",
          name: "报告.md",
          path: "D:\\资料\\报告.md",
          rel_path: "资料/报告.md",
          ext: "md",
          size: 10,
          status: "ok",
          stage: "L1",
          chars: 12,
          pages: 0,
          quality: "good",
          message: null,
          error: null,
          attempts: 0,
          updated_at: "2026-11-22T10:00:00+08:00",
        },
        {
          location_id: "loc_2",
          version_id: "ver_2",
          name: "坏文档.docx",
          path: "D:\\资料\\坏文档.docx",
          rel_path: "资料/坏文档.docx",
          ext: "docx",
          size: 10,
          status: "failed",
          stage: "L1",
          chars: 0,
          pages: 0,
          quality: null,
          message: null,
          error: "文件损坏，无法解析",
          attempts: 3,
          updated_at: "2026-11-22T10:01:00+08:00",
        },
      ],
      total: 2,
      limit: 100,
      offset: 0,
      generated_at: "2026-11-22T10:02:00+08:00",
    });
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^数据/ }));
    await settle();

    fireEvent.click(await screen.findByRole("button", { name: "解析结果" }));
    expect(await screen.findByText("报告.md")).toBeTruthy();
    expect(await screen.findByText("坏文档.docx")).toBeTruthy();
    expect(await screen.findByText("解析失败")).toBeTruthy();
    expect(await screen.findByText(/返回数据源/)).toBeTruthy();
  });

  it("解析结果可应用内预览正文，不依赖系统文件关联", async () => {
    mocks.sources.mockResolvedValue({ items: [SOURCE_CARD] });
    mocks.sourceParseResults.mockResolvedValue({
      source_id: "src_1",
      summary: { total: 1, parsed: 1, failed: 0, empty: 0, pending: 0, locked: 0, unsupported: 0 },
      items: [
        {
          location_id: "loc_1",
          version_id: "ver_1",
          name: "报告.md",
          path: "D:\\资料\\报告.md",
          rel_path: "资料/报告.md",
          ext: "md",
          size: 10,
          status: "ok",
          stage: "L1",
          chars: 12,
          pages: 0,
          quality: "good",
          message: null,
          error: null,
          attempts: 0,
          updated_at: "2026-11-22T10:00:00+08:00",
        },
      ],
      total: 1,
      limit: 100,
      offset: 0,
      generated_at: "2026-11-22T10:02:00+08:00",
    });
    mocks.documentText.mockResolvedValue({
      location_id: "loc_1",
      name: "报告.md",
      path: "D:\\资料\\报告.md",
      rel_path: "资料/报告.md",
      ext: "md",
      chars: 18,
      truncated: false,
      text: "# 报告\n\n样本量 42",
    });
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^数据/ }));
    await settle();

    fireEvent.click(await screen.findByRole("button", { name: "解析结果" }));
    fireEvent.click(await screen.findByRole("button", { name: "预览" }));

    const dialog = await screen.findByRole("dialog", { name: "报告.md 的正文预览" });
    expect(within(dialog).getByText(/样本量 42/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("更新作为独立模块", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^更新/ }));
    await settle();

    expect(await screen.findByRole("heading", { name: "更新" })).toBeTruthy();
    expect(await screen.findByText("持续自动解析")).toBeTruthy();
    expect(await screen.findByText("最近解析活动")).toBeTruthy();
  });

  it("更新页可以关闭持续自动解析", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /^更新/ }));
    await settle();

    fireEvent.click(await screen.findByRole("switch", { name: "持续自动解析" }));
    await waitFor(() =>
      expect(mocks.saveIngestSettings).toHaveBeenCalledWith({ auto_parse: false }),
    );
  });
});

describe("固定对话输入框", () => {
  it("同一文档的多个版本在引用中归为一组", async () => {
    mocks.createChatSession.mockResolvedValue({
      session_id: "chat_1",
      title: "新对话",
      created_at: "2026-11-23T00:00:00+08:00",
      updated_at: "2026-11-23T00:00:00+08:00",
      message_count: 0,
    });
    mocks.familyMembersByAsset.mockResolvedValue({
      family_id: "fam_1",
      title: "毕业论文",
      confidence: "high",
      members: [
        {
          asset_id: "ast_1",
          location_id: "loc_1",
          path: "D:\\论文初稿.md",
          name: "论文初稿.md",
          rel_path: "论文初稿.md",
          version_label: "初稿",
        },
        {
          asset_id: "ast_2",
          location_id: "loc_2",
          path: "D:\\论文终稿.md",
          name: "论文终稿.md",
          rel_path: "论文终稿.md",
          version_label: "终稿",
        },
      ],
    });
    mocks.ask.mockResolvedValue({
      question: "我想看看论文写了什么",
      answer: "论文包含研究背景。",
      refused: false,
      reason: null,
      provider: "extractive",
      model: null,
      citations: [
        {
          n: 1,
          chunk_id: "chunk_1",
          asset_id: "ast_1",
          location_id: "loc_1",
          name: "论文初稿.md",
          path: "D:\\论文初稿.md",
          page: null,
          sheet: null,
          snippet: "研究背景",
          ocr: false,
          semantic: false,
          version_id: "ver_1",
          mtime: null,
          bbox: null,
          family_id: "fam_1",
          family_size: 30,
          family_title: "毕业论文",
          version_label: "初稿",
          sources: [],
        },
        {
          n: 2,
          chunk_id: "chunk_2",
          asset_id: "ast_2",
          location_id: "loc_2",
          name: "论文终稿.md",
          path: "D:\\论文终稿.md",
          page: null,
          sheet: null,
          snippet: "研究结论",
          ocr: false,
          semantic: false,
          version_id: "ver_2",
          mtime: null,
          bbox: null,
          family_id: "fam_1",
          family_size: 30,
          family_title: "毕业论文",
          version_label: "终稿",
          sources: [],
        },
        {
          n: 3,
          chunk_id: "chunk_3",
          asset_id: "ast_3",
          location_id: "loc_3",
          name: "写作规范.doc",
          path: "D:\\写作规范.doc",
          page: null,
          sheet: null,
          snippet: "排版要求",
          ocr: false,
          semantic: false,
          version_id: "ver_3",
          mtime: null,
          bbox: null,
          family_id: null,
          family_size: 1,
          family_title: "",
          version_label: "",
          sources: [],
        },
      ],
      evidence_count: 3,
      warning: null,
      plan: {
        intent: "comprehensive",
        label: "综合探索",
        budget: 10,
        per_family: 2,
        per_asset: 1,
        min_families: 5,
        output_mode: "topic_grouped_summary",
        deduplicate_by: ["document_family", "content_similarity"],
        required_coverage: ["论文", "毒素基因"],
        reason: "问题需要跨资料归纳",
        candidates: 18,
        used: 3,
        families: 2,
        facets: [
          { name: "论文", covered: true },
          { name: "毒素基因", covered: false },
        ],
        missing: ["毒素基因"],
        reranked: true,
        rerank_kept: 5,
        rerank_error: null,
      },
      coverage: {
        content_indexed: 3,
        content_pending: 0,
        content_empty: 0,
        content_failed: 0,
        ocr_indexed: 0,
        ocr_pending: 0,
      },
    });

    await renderChat();
    fireEvent.change(await screen.findByPlaceholderText("找点啥？"), {
      target: { value: "我想看看论文写了什么" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("毕业论文")).toBeTruthy();
    expect(await screen.findByText("30 个关联版本")).toBeTruthy();
    expect(await screen.findByText(/归并为 1 个文档家族/)).toBeTruthy();
    expect(screen.getByText("写作规范.doc")).toBeTruthy();
    expect(screen.getByText("综合探索")).toBeTruthy();
    expect(screen.getByText(/候选 18 · 相关 5 · 采用 3 · 覆盖 2 个家族/)).toBeTruthy();
    expect(screen.getByText("模型重排")).toBeTruthy();
    expect(screen.getByText(/证据缺口：毒素基因/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /30 个关联版本/ }));
    const dialog = await screen.findByRole("dialog", { name: "毕业论文 的关联版本" });
    expect(within(dialog).getAllByText("论文初稿.md").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText("论文终稿.md").length).toBeGreaterThan(0);
  });

  it("输入框与发送按钮位于消息列表之后的 composer 中", async () => {
    const { container } = await renderChat();

    const composer = await waitFor(() => {
      const element = container.querySelector(".chat-composer");
      if (!element) throw new Error("composer 未渲染");
      return element as HTMLElement;
    });
    const messages = container.querySelector(".chat-messages");
    expect(messages).toBeTruthy();
    expect(
      messages!.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(composer).getByPlaceholderText("找点啥？")).toBeTruthy();
    expect(within(composer).getByRole("button", { name: "提问" })).toBeTruthy();
  });

  it("等待回答时立即显示问题、检索进度与已用时间", async () => {
    let resolveAsk: (value: unknown) => void = () => undefined;
    mocks.createChatSession.mockResolvedValue({
      session_id: "s1",
      title: "新会话",
      created_at: "2026-11-24T10:00:00+08:00",
    });
    mocks.ask.mockImplementation(
      () => new Promise((resolve) => {
        resolveAsk = resolve;
      }),
    );

    const { container } = await renderChat();
    fireEvent.change(await screen.findByPlaceholderText("找点啥？"), {
      target: { value: "样本量是多少" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("正在检索本地资料…")).toBeTruthy();
    expect(container.querySelector(".chat-pending-time")).toBeTruthy();
    expect(screen.getByText("样本量是多少")).toBeTruthy();
    expect(screen.getByRole("button", { name: /检索中/ })).toBeTruthy();

    await act(async () => {
      resolveAsk({
        question: "样本量是多少",
        answer: "样本量是 42。",
        refused: false,
        reason: null,
        provider: "extractive",
        model: null,
        citations: [],
        evidence_count: 0,
        warning: null,
        coverage: {
          content_indexed: 1,
          content_pending: 0,
          content_empty: 0,
          content_failed: 0,
          ocr_indexed: 0,
          ocr_pending: 0,
        },
      });
    });
    await settle();

    expect(await screen.findByText("样本量是 42。")).toBeTruthy();
    expect(screen.queryByText("正在检索本地资料…")).toBeNull();
  });

  it("流式回答边生成边显示正文", async () => {
    mocks.createChatSession.mockResolvedValue({
      session_id: "s1",
      title: "新会话",
      created_at: "2026-11-24T10:00:00+08:00",
    });
    mocks.askStream.mockImplementation(async (_question, _options, handlers) => {
      handlers.onStatus?.("generation", "正在生成回答…");
      handlers.onDelta?.("第一段");
      handlers.onDelta?.("第二段");
    });

    await renderChat();
    fireEvent.change(await screen.findByPlaceholderText("找点啥？"), {
      target: { value: "样本量是多少" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("第一段第二段")).toBeTruthy();
  });

  it("样式表把 composer 固定在视口底部", () => {
    const block = styles.match(/\.chat-composer\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(block).toContain("position: sticky;");
    expect(block).toContain("bottom: 0;");
  });

  it("切回对话时滚动到最新消息", async () => {
    const { container } = await renderChat();
    const main = container.querySelector(".main") as HTMLElement;
    const setScrollTop = vi.fn();
    Object.defineProperty(main, "scrollTop", {
      configurable: true,
      get: () => 0,
      set: setScrollTop,
    });
    Object.defineProperty(main, "scrollHeight", { configurable: true, value: 2400 });

    fireEvent.click(screen.getByRole("button", { name: /^数据/ }));
    await settle();
    setScrollTop.mockClear();

    fireEvent.click(screen.getByRole("button", { name: /^对话/ }));
    await waitFor(() => expect(setScrollTop).toHaveBeenCalledWith(2400));
  });
});
