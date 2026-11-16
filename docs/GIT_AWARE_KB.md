# Git 版本视图：代码 / 图谱 / 笔记三层联动

> 分支：`feat/git-aware-kb`。本文同时覆盖两件事：
>
> - **A. 锚点回溯**：代码改了之后，知识库里的引用还能回到当初那一版（P0）。
> - **B. 版本视图**：主动切到某个分支/提交，代码、知识图谱、笔记同时跟随这一版（P1–P3）。
>
> 参考实现：`D:\DSH-better-sidebar-main\DSH-better-sidebar-main` 的 `src/git.ts`
> （单进程 git、有界输出、`show rev:path`）、`src/client/editor-git-gutter.ts`
> （blame 按需取）、`docs/plans/2026-10-16-git-graph.md`（只读、有界分页、显式过期的取舍）。

---

## 0. 一句话架构

> 引入一个全局的 **Revision Scope**：`worktree`（默认，就是今天的行为）或 `rev=完整 commit sha`。
> 代码、图谱、符号索引、笔记状态全部按这个 scope 取数；git 是唯一事实源，
> 版本内容一律**只读从 git 读**，绝不 checkout、绝不写用户的仓库。

---

## 1. 需求拆解

用户要的是三件事同时跟随 git：

| 层 | 今天 | 目标 |
|---|---|---|
| **代码** | 只能看工作区；引用跳转按 `path:line` | 切到某分支/提交，看到**那一版的代码**（只读） |
| **图谱** | 只有工作区一张图，随索引失效重建 | 每一版有**自己那张代码图谱**，切版本即切图 |
| **笔记** | 内容快照 + 裸坐标，不知道来自哪一版 | 每条笔记知道自己写于哪一版；切版本时**引用状态跟着重算**，并区分「当时已存在 / 此后新增」 |

关键的用户动作是「**切换**」，所以不是三个独立功能，而是一个共享的 scope 贯穿三层。
如果每层各自维护「当前版本」，迟早会出现「代码是 v1、图谱是 HEAD」的错位——这是本设计要
从结构上排除的失败模式。

---

## 2. 现状（已核实的代码事实）

- `code_anchors.commit_sha` 列存在，但 `anchors.create_for_citation()` 从不写，永远 `NULL`；
  `docs/ARCHITECTURE.md:58` 声称记录了 commit，与实现漂移。
- `GET /projects/{id}/anchors` 前端零消费，锚点是「后端有、没有入口」的半成品。
- `git_service.py` 只有 status/diff/`file_at_head`；`_run()` 失败返回空串并丢掉 stderr，
  **没有「按 revision 取文件」的能力**。
- `notegen._read_range()` 从磁盘读当前内容写进 `## 关联代码`，标题只写 `### path:start-end`。
- 图谱与符号索引是**工作区独占**的：`graphs` 主键 `(project_id, scope)`，
  `symbol_defs/refs` 主键带 `project_id`，失效由索引器在语料变化时删除（epoch 防并发）。
- **但管线是可复用的**：`graph.builder.build_graph(sources=[(rel, bytes)])` 与
  `build_symbol_index(sources=...)` 接受内存里的源码字节，
  `graph.ast.resolve.resolve(extractions, with_index=True)` 一次产出图 + 符号索引。
  这意味着「某一版的图谱」可以完全从 git blob 算出来，不必落盘、不必 checkout。
- 检索索引（`chunks` + FTS + 向量）与图谱是两套：检索只服务「当前能看到的代码」。

---

## 3. 核心概念：Revision Scope

```ts
type Scope =
  | { kind: 'worktree' }                                   // 默认，可编辑
  | { kind: 'rev'; sha: string; shaShort: string;
      ref?: { kind: 'branch' | 'tag' | 'commit'; name: string } }  // 只读
```

不变量：

1. **一个应用只有一个 scope**（`AppState.revision`），所有面板从它派生，不各自持有。
2. **scope 只由完整 sha 标识**。分支/标签只是「入口」，点选时立刻解析成 sha
   （`git rev-parse --verify <ref>^{commit}`）。之后分支再前进，界面**不会**悄悄换成新内容，
   而是提示「`main` 已前进到 `def5678`」并提供刷新按钮。理由：用户正在读的那一版不能被背后换掉。
3. **`worktree` 与 `rev` 是互斥的两套取数路径**，不是「rev 覆盖 worktree」。工作区路径的
   索引、图谱、锚点逻辑**一行不改**，新逻辑平行挂载，避免把现有行为拖进版本语义。

---

## 4. 决策

**D1｜版本视图默认只读，不 checkout 工作区。**
「切换到某个分支」在本产品里先实现为「查看那个分支的版本」。真正的 `git checkout`
会改动用户仓库（未提交改动可能丢失、构建产物可能失效），必须作为独立的、逐次确认的写操作，
不混进浏览动作里。第一期完全不提供 checkout。

**D2｜取版本内容用 `git archive` 流式读，不建 worktree、不逐个 `git show`。**
- `git worktree add --detach` 会在用户 `.git/worktrees` 里留下元数据、并复制整棵树到磁盘，
  崩溃后要手工清理 —— 违背「不动用户仓库」。
- 逐个 `git show rev:path` 是 N 个进程，几千个文件就是几千次 spawn。
- `git archive --format=tar <sha>` 是**一个**进程、流式 tar；Python `tarfile` 边读边丢，
  只保留 `config_for(rel) is not None` 且未命中忽略规则的成员字节。
  内存上界由「单文件 1.5 MB + 总字节上限」控制，超限中止并显式报错。

**D3｜版本图谱是「不可变缓存」，不需要失效，只需要淘汰。**
一个 commit 的内容永远不会变，所以 `(project, sha)` 的图谱一旦建成就是事实，
不存在工作区图谱那套 epoch/指纹/重算。唯一的生命周期是**容量淘汰**（按最近使用）。
这比工作区图谱简单一个量级，也是本设计最重要的简化。

**D4｜符号索引按版本另行存储，导航接口带 `rev`。**
F12/Ctrl+Click 在版本视图里必须跳到**那一版**的定义，不能用工作区的 `symbol_defs`
（行号会错位）。新表按 `(project_id, rev)` 隔离；工作区表原样保留。

**D5｜笔记不按版本分叉，只按版本重算「引用状态」。**
每个 commit 复制一份笔记会立刻产生「同一知识点 N 份、改一处要改 N 处」的维护灾难，
而且笔记是用户资产，不是代码的派生物。所以：
- 笔记正文只有一份（可编辑）；
- 每条笔记记录 `base_commit`（写它时的 HEAD）与 `base_dirty`；
- 在 scope 里查看时，**按该 scope 重新解析每条引用**（工作区 → 同文件搬家 → 该 rev 的 blob → 快照），
  并额外回答「这条笔记在当时存在吗」（`base_commit` 是否为当前 rev 的祖先）。

**D6｜版本视图里的检索降级为「当前版本内搜索」或明确不可用。**
向量/FTS 索引只描述工作区。第一期：版本视图下**隐藏语义检索**，只保留「在当前版本的文件树里
按文件名/路径搜」。不伪造一套历史索引——建索引的成本（embedding）与收益完全不成比例。

**D7｜笔记图谱按 scope 过滤，不重建。**
笔记图谱（`scope='notes'`）的节点是笔记、边是「同文件夹/同项目/共享引用」。
版本视图下只需按「该版本可见的笔记 id 集合」过滤节点与边，再按需重算 `degree`。
不新起一套图谱存储。

**D8｜有界：文件数、字节数、缓存条目三重上限，超限显式报错。**
照搬 DSH 的原则：宁可直接失败并说清楚，也不要卡住或静默给半张图。

**D9｜所有 git 操作数都当数据，不当选项。**
`rev` 先过白名单正则 + `rev-parse --verify`；`path` 走 `paths.resolve_in_project`；
命令统一 `--end-of-options`，pathspec 统一 repo-relative POSIX + `--literal-pathspecs`。

**D10｜失败一律降级为「这一版读不到」，绝不炸主界面。**
浅克隆缺对象、rebase 后 sha 不可达、仓库损坏、git 不在 PATH：都返回可读的降级结果
（空图/空树/`exists=false`），并给一句人话原因。

---

## 5. 数据模型

### 5.1 A 部分（锚点回溯）

```sql
ALTER TABLE code_anchors ADD COLUMN worktree_dirty  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE code_anchors ADD COLUMN resolved_commit TEXT;
ALTER TABLE code_anchors ADD COLUMN resolved_status TEXT;

ALTER TABLE notes ADD COLUMN base_commit TEXT;
ALTER TABLE notes ADD COLUMN base_dirty  INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS note_refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL,
    path        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    symbol      TEXT,
    commit_sha  TEXT,
    snippet     TEXT,
    code_hash   TEXT,
    status      TEXT NOT NULL DEFAULT 'unchanged',
    resolved_commit TEXT,
    checked_at  TEXT,
    created_at  TEXT NOT NULL
);
```

### 5.2 B 部分（版本视图）

**实际落地**：版本图谱复用现有 `graphs` 表，scope 写成 `code@<完整 sha>`（见
`app/graph/revision_store.py`），而不是另建 `revision_graphs`。理由：`graphs` 已经有
`payload/schema_version/node_count/edge_count/built_at`，主键 `(project_id, scope)` 天然隔离；
版本图不可变，用 `built_at` 做 LRU 淘汰即可。若坚持独立表，只是把同一份 payload 换个表名。

```sql
-- 版本图谱：与工作区图谱共用 graphs，scope = 'code@<sha>'。
-- 缓存条目，不是事实：删掉只影响下次重建速度。
-- 工作区图谱的 scope 仍是 'code'，两者永不互相失效。

-- 版本符号索引（导航用）。
CREATE TABLE IF NOT EXISTS revision_symbol_defs (
    project_id     INTEGER NOT NULL,
    rev            TEXT    NOT NULL,
    node_id        TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    normalized     TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    start_line     INTEGER NOT NULL,
    start_col      INTEGER NOT NULL DEFAULT 1,
    end_line       INTEGER NOT NULL,
    end_col        INTEGER NOT NULL DEFAULT 1,
    parent_node_id TEXT,
    signature      TEXT    NOT NULL DEFAULT '',
    doc            TEXT    NOT NULL DEFAULT '',
    language       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, rev, node_id)
);

CREATE TABLE IF NOT EXISTS revision_symbol_refs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL,
    rev            TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    start_line     INTEGER NOT NULL,
    start_col      INTEGER,
    end_line       INTEGER,
    end_col        INTEGER,
    name           TEXT    NOT NULL,
    normalized     TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    from_node_id   TEXT,
    target_node_id TEXT,
    confidence     TEXT    NOT NULL DEFAULT 'EXTRACTED'
);
```

`graph_layouts` 直接复用：`scope` 写成 `code@<rev>`，`layout_key` 仍是拓扑指纹。
版本图谱不可变，布局也就一次算完、永久命中。

---

## 6. 版本内容读取（`services/revisions.py`）

```python
def resolve_rev(root: Path, ref: str) -> tuple[str, str] | None:
    """ref（分支/标签/短 sha/HEAD）→ (全 sha, 规范化 ref 名)；非法或不存在返回 None。"""

def read_sources(root: Path, sha: str, *, max_bytes: int) -> tuple[list[tuple[str, bytes]], dict]:
    """git archive --format=tar <sha> | tarfile 流式读取，返回 (源码列表, 统计)。

    - 跳过 ignored 目录、二进制后缀、无 extractor 的文件；
    - 单文件 > 1.5 MB 跳过（与 graph builder 的既有上限一致）；
    - 累计字节 > max_bytes → 抛 RevisionTooLarge（显式错误，不返回残图）。
    """

def read_file(root: Path, sha: str, rel: str) -> bytes | None:
    """单个文件在某版本的内容：git show <sha>:<rel>，失败 None。"""

def list_tree(root: Path, sha: str, path: str, depth: int) -> list[dict]:
    """版本文件树：git ls-tree -r -z --name-only <sha>，按前缀聚合，有界。"""

def is_ancestor(root: Path, older: str, newer: str) -> bool:
    """git merge-base --is-ancestor，用于笔记可见性判断。"""
```

`git archive` 的一个坑：它会应用 `.gitattributes` 的 `export-ignore`。这对「看源码」是**正确**行为
（被 export-ignore 的本来就不该出现在导出里），但要在文档里写清楚，避免以后有人以为是 bug。

---

## 7. 三层实现

### 7.1 代码层

**文件树**：`GET /projects/{id}/tree?rev=` 从 `ls-tree -r` 聚合（按目录前缀），
分页/深度与现有 `get_tree` 对齐；结果按 `(project, sha, path)` 做内存 LRU（树是不可变的）。

**文件内容**：`GET /projects/{id}/file?rev=` 走 `read_file`，
返回形状与现有 `GET /file` 一致（`content/size/binary/truncated/language`），
另外加 `sha`、`exists`。`language` 复用 `detect_language`。

**编辑器**：`EditorTab` 的 file 变体加 `rev?: string`；
`CodePane` 在 `rev` 存在时 `readOnly: true`，顶部横幅「历史版本 `abc1234` · 只读」+
「回到工作区」；保存快捷键被禁用，`PUT /file` 在没有 `rev` 时才允许。

**引用跳转**：`reveal(path, start, end)` 在 rev scope 下打开 `path@rev` 标签；
在 worktree scope 下保持今天的行为，并额外给出「查看历史版本」入口（来自锚点/笔记的解析结果）。

**检索**：版本视图隐藏语义检索，仅保留文件树过滤（D6）。

### 7.2 图谱层

```python
def get_or_build_revision_graph(project_id, root, sha) -> tuple[payload, built: bool]:
    row = load(project_id, sha)                 # 单次主键查询
    if row and row.schema_version == SCHEMA_VERSION:
        touch(project_id, sha); return payload, False
    sources, stats = revisions.read_sources(root, sha, max_bytes=CAP)
    graph, index = builder.build_graph(sources), builder.build_symbol_index(sources)
    store_revision_graph(project_id, sha, graph, index)   # 同一事务写图 + 符号索引
    evict(project_id)                           # 按 last_access_at 淘汰，超出条数上限
    return graph, True
```

要点：

- **复用现有 AST 管线**，不改 resolver、不改 schema 语义；`SCHEMA_VERSION` 变化时版本图谱
  一并失效（版本号对上才算命中）。
- **构建是同步请求还是后台 job？** 第一期实现为**同步构建**（`revision_store.get_or_build`）：
  构建只在缓存缺失时发生一次，`revisions.read_sources` 已有 120 s 看门狗与 80 MiB/3000 文件上限，
  超限直接 400 而不是卡死；之后都是单条主键查询。代价是首次打开某版本图谱会等几秒。若日后需要
  进度条，再把同一函数挂到 `jobs`（`kind='rev-graph'`）即可，数据层不用改。
- **布局**：`scope='code@<sha>'`，`layout_key` 用 `topology_key(graph)`，一次算完长期命中。
- **节点跳转**（`/graph/locate`、`/graph/node`、F12）在 rev scope 下读 `revision_symbol_defs`；
  工作区仍读 `symbol_defs`。两套查询函数的签名只差一个 `rev`，不复制整份 resolver。
- **淘汰**：每项目保留最近 `N=8` 个版本图（可配），超出按 `last_access_at` 删；
  连同 `revision_symbol_*` 一起删。删的是缓存，不影响任何事实。
- **不做**：跨版本的图 diff（那是「架构演进视图」，另一个产品）。

### 7.3 笔记层

**可见性**（scope 决定看到哪些笔记）：

| 关系 | 展示 |
|---|---|
| `base_commit` 是当前 rev 的祖先（含相等） | 正常显示，标「当时已存在」 |
| `base_commit` 不是祖先（该笔记写于更新的一版） | 降级显示（半透明）、标「此后新增」 |
| `base_commit` 为 `NULL`（旧笔记，或当时无提交） | 正常显示，标「未记录版本」 |
| 当前 scope 是 worktree | 全部正常显示（今天的行为） |

**引用状态**（每条 `note_ref` 在 scope 下重算）：

```
worktree scope : 工作区 hash → moved → 记录 commit → 快照
rev scope      : 该 rev 的 blob hash → 该 rev 内搬家 → 记录 commit → 快照
```

比 A 部分多一级「当前 rev 的 blob」，因为版本视图里「当前」就是那个 commit。
结果仍是那五个状态词（`unchanged/moved/historical/modified/deleted`），
前端只加一句「在此版本中的对应代码已不同」的提示，不换一套词表。

**笔记图谱**：版本视图下按可见笔记 id 过滤 `scope='notes'` 的 payload，
边只保留两端都可见的，`degree` 重算；不触发重建。

**导出**：`POST /notes/{id}/export` 在正文里带上 `base_commit` 短 sha；
若用户从版本视图导出，附一行「引用按 abc1234 解析」。

---

## 8. API 清单

新增（全部只读）：

```
GET  /api/projects/{id}/git/branches
→ {current, detached, branches: [{name, sha, sha_short, is_remote}], tags: [...]}

GET  /api/projects/{id}/git/commits?limit=30&skip=0&path=
→ {commits: [{sha, sha_short, subject, author, author_time, parents}], truncated}

GET  /api/projects/{id}/git/rev?ref=
→ {sha, sha_short, kind: branch|tag|commit, name}   // 入口 ref → 钉住的 sha；不存在 404

GET  /api/projects/{id}/git/tree?rev=&path=&depth=
→ [{name, path, is_dir, size?, children}]           // 版本文件树

GET  /api/projects/{id}/git/file?rev=&path=
→ {path, rev, sha, content, size, truncated, binary, exists, language}

GET  /api/projects/{id}/graph?rev=
→ {nodes, edges, communities, stats, layout_key, positions, built, truncated?}
   building 中返回 202 + job_id（复用 jobs）

POST /api/projects/{id}/graph/build?rev=            // 显式重建某版本图谱

GET  /api/projects/{id}/symbols?q=&rev=
GET  /api/projects/{id}/definition?path=&line=&column=&rev=
GET  /api/projects/{id}/references?path=&line=&column=&rev=
GET  /api/projects/{id}/hover?path=&line=&column=&rev=

GET  /api/notes?rev=&project_id=
→ Note[] + {visible_at_rev: bool, base_commit}

GET  /api/notes/{id}?rev=
→ Note + {refs: [NoteRef]，按 rev 解析后的状态}
```

改动（向后兼容）：`GET /anchors` 增字段；`POST /anchors/refresh` 增 `historical` 计数；
`GET /git/status` 增 `head_short`。

---

## 9. 前端状态模型

```ts
// AppState 新增（唯一事实源）
revision: Scope                       // 默认 {kind:'worktree'}
setRevision: (scope: Scope) => void   // 切换时清空 citationHighlight、重置导航栈

// EditorTab 扩展
| { kind:'file'; path: string; rev?: string; highlight: Range | null }

// 取数 hook 统一带 scope
useGraph(scope)  useFileTree(scope)  useNotes(scope)  useFile(path, scope)
```

切换 scope 时必须做的四件事（写进同一个函数，防止漏）：

1. 关掉/重建当前代码标签（同一路径的 worktree 标签不能直接复用为 rev 标签）；
2. 清 `citationHighlight` 与导航历史（行号属于旧 scope）；
3. 图谱标签重新取数（带新 scope），画布保留布局但换 payload；
4. 笔记面板重新取数（可见性 + 引用状态）。

UI：

- `TopBar` 增加版本选择器：`工作区` / 分支列表 / 最近提交 / 手输 sha；选中后显示
  `main · abc1234` 标签，可一键「回到工作区」。
- 进入 rev scope 后，整页顶部一条细横幅（只读提示），不做换肤。
- 分支前进时提示「`main` 已前进到 `def5678`」，提供「切到最新」而**不自动切换**。

---

## 10. 边界与坑

| 场景 | 处置 |
|---|---|
| 工作区有未提交改动，同时切到 rev | 允许；rev 视图完全不看工作区。回到工作区时原样（含未保存草稿） |
| 引用时工作区脏 | `worktree_dirty=1`；回溯优先 `HEAD`，文案写「引用时有未提交改动」 |
| 仓库还没有第一个提交 | `commit_sha=None`；`git/log` 空列表；版本选择器只显示「工作区」 |
| 分支名/ref 含中文或空格 | `rev-parse --verify` 全文匹配，不做字符串拼接 |
| 短 sha 歧义 | `rev-parse --verify` 失败即 404，不猜 |
| rev 以 `-` 开头 | `--end-of-options` + 正则白名单 |
| 文件含 `[` `*` | `--literal-pathspecs` |
| 中文路径 | `core.quotePath=false`；一律 repo-relative POSIX |
| 浅克隆缺对象 | `git archive`/`show` 失败 → 明确「此版本对象不在本地」，不返回空图冒充 |
| sha 被 GC / rebase 掉 | 版本图谱读取失败 → 标 stale 并提示，不删缓存（可能只是暂时不可达） |
| 仓库巨大（> 3000 源文件 / > 80 MB） | 超过上限显式 422「项目过大，暂不支持版本图谱」，并允许只看代码 |
| 版本图谱构建中途失败 | 不写库；job 标 error；下次重试 |
| 并发两个请求同一版本 | 每 `(project, rev)` 一把进程内锁，第二个等待后读缓存 |
| `.gitattributes export-ignore` | `git archive` 会跳过，视为正常；文档写明 |
| 子模块 | `git archive` 不含子模块内容；树里显示但内容为空，标「子模块未包含」 |
| 二进制/大文件 | 单文件 1.5 MB 上限；`binary=true` 不返回正文 |
| 笔记 base_commit 缺失 | 归为「未记录版本」，正常显示 |
| 版本视图里编辑笔记正文 | 允许（笔记不属于 git）；但引用状态按 scope 只读展示 |

---

## 11. 性能预算（目标，非实测）

| 动作 | 目标 | 手段 |
|---|---|---|
| 版本文件树首屏 | < 300 ms（千级文件） | `ls-tree` 一次 + 目录聚合 + 内存 LRU |
| 单个文件读取 | < 100 ms | `show` 单进程，2 MiB 上限 |
| 版本图谱冷启动 | < 8 s（千级文件） | `git archive` 流式 + 现有 AST 管线（同步构建，仅首次） |
| 版本图谱热读 | 单条主键查询 | `graphs` 的 `code@<sha>` 行 |
| 版本切换（切回已看过的版本） | < 200 ms | 图/树/布局都在缓存里 |
| 存储 | 每版本图 ≈ 工作区图大小 × N(8) | 淘汰上限，可配 |

---

## 12. 分期

| 期 | 内容 | 用户能看到什么 |
|---|---|---|
| **P0** ✅ | `git_service` 重写（环境/超时/上限/`rev-parse`/`show`/`log`）、迁移、锚点四级解析、`note_refs`、A 部分接口、pytest | 后端可验证；引用能回溯到旧 commit |
| **P1** ✅ | `revisions.read_sources/list_tree/read_file`、`?rev=` 的 tree/file、只读版本标签页、TopBar 版本切换器 | 切到某版，看到那一版的代码 |
| **P2** ✅ | 版本图谱（`code@<sha>`）+ `?rev=` 图谱 + 版本符号索引（F12/引用/hover 全部带 `rev`） | 切版本，图谱随之切换，跳转也在那一版里 |
| **P3** ✅ | 笔记可见性/引用状态/笔记图谱过滤 | 笔记跟着 git：知道「当时有没有这条」「引用当时是什么」 |
| **P4（不做）** | 真正的 `checkout`（显式确认、要求工作区干净、不自动 stash） | 用户主动改工作区——按用户决定不实现 |
| **P5** ✅ | 只读「源代码管理」侧栏、`/git/changes`、`/git/pair`、Monaco 双栏 diff、笔记引用跳转高亮 | 与「文件 / 对话 / 笔记」并列的第四个 tab；看每个提交改了什么、点开两版对照 |

顺序的理由：P0 是「引用可回溯」的地基；P1 给出「版本」这个可见概念；
P2 才把图谱接上（成本最高，且依赖 P1 的取数层）；P3 是三层里最轻的收尾。

---

## 13. 明确不做

- 不做完整 SCM 面板的**写**操作（暂存/提交/推送/拉取/冲突解决）；只读的源代码管理面板见 §15；
- 不在浏览动作里 checkout / stash / reset；
- 不把历史版本灌进 FTS/向量索引；
- 不为每个版本复制一份笔记；
- 不做跨版本图谱 diff；提交时间线只画只读泳道图，不提供分支切换/合并操作；
- 不在 SQLite 里复制 git 历史；
- 不自动刷新正在查看的版本（分支前进只提示）。

---

## 14. 验收

**后端**

- `test_git_service.py`：`rev-parse`（分支/短 sha/非法/含 `-`）、`show`（命中/不存在/二进制/超限）、
  `log`（limit/skip/path）、非仓库。
- `test_revisions.py`：两提交仓库 → `read_sources` 在 HEAD 与 HEAD~1 得到不同文件集；
  忽略目录/二进制被跳过；总字节超限抛明确错误；`list_tree` 深度与路径前缀正确；`is_ancestor`。
- `test_revision_graph.py`：两个提交的图节点不同；`(project, rev)` 命中缓存不重建；
  工作区改动**不影响**已建版本图；`?rev=` 的 F12 在旧版本里报 `file_not_found`；未知 ref 报 404。
- `test_note_scope.py`：`base_commit` 晚于所看版本 → `note_state='later'` 且不隐藏；
  引用在 rev 下 `unchanged` / `historical` / `deleted`；未知 rev 404；笔记图谱过滤掉「未来」的笔记。
- `test_git_service.py` / `test_revisions.py` / `test_anchor_history.py` / `test_note_versioning.py`
  分别覆盖 P0 的 git 调用、版本读取、锚点四级解析、笔记版本基线。

**前端**

- 版本切换器的四步重置都发生（代码标签/高亮/图谱/笔记）；
- revision 标签只读、不可保存、同路径可与工作区标签共存；
- 图谱在切换后请求带 `rev`，画布不残留上一版节点。

**手工**

- 在本仓库自身切到 `9b60831`（master 头）与一个更早的提交，对比图节点数量与文件内容；
- 切回工作区后未保存草稿仍在。

---

## 15. 只读源代码管理面板（P5）

仿 VS Code SCM 的只读视图，作为侧栏第四个 tab（与「文件 / 对话 / 笔记」并列）。

**口径（用户确认）**

- 只读：不暂存、不提交、不丢弃，任何看起来会写仓库的按钮都不放；
- 历史提交的变更集 = **该提交 vs 第一父提交**（root commit 全部算 A）；
- 工作区变更集 = 工作区（含 untracked）vs `HEAD`；
- 面板结构 = 上方「更改」列表 + 下方提交时间线；点提交即切换全局 revision scope；
- 笔记引用点击 = 先看当前工作区：同一文件里能精确匹配笔记保存的代码片段，就打开最新文件并定位到重算后的行号；
  片段已不存在时，才切到笔记写作时的 commit，打开该版本的整份文件并高亮旧行。

**后端**

- `app/services/scm.py`
  - `changes(root, rev=None)`：porcelain/`diff-tree` 解析 A/M/D/R/C/U、`+/−` 行数、二进制标记、2 MB 截断；
  - 合并提交显式 diff 第一父提交：`diff-tree <first-parent> <sha>`，否则 Git 对 merge 默认不出文件；
  - `file_pair(root, rev, path)`：返回两端的文本（不是 patch），供 Monaco `DiffEditor` 使用。
- `GET /api/projects/{id}/git/changes?rev=`（缺省为工作区）
- `GET /api/projects/{id}/git/pair?path=&rev=`（缺省为 `HEAD ↔ 工作区`）
- `GET /api/projects/{id}/git/commits?topo=true`：时间线用拓扑序，父提交总在子提交之后，泳道图不会断线。
- 测试：`tests/test_scm.py`（提交 vs 父提交、root commit、合并提交 vs 第一父提交、工作区 M/A/D、两端内容、未知 rev 404、API 形状）。

**前端**

- `Sidebar.tsx` 第四个 tab `源代码`；`ScmPanel.tsx` 渲染更改与时间线；
- `ScmGraph.tsx` 按 VS Code `scmHistory.ts` 的几何重画提交泳道：22px 行高、11px 泳道、
  5px 曲线半径、4px 圆点；输入/输出泳道逐行推进，合并提交的第二父提交向右下弯出；
- 时间线行内只保留「泳道 + subject」；作者、绝对/相对时间、sha、改动文件数与 `+/−`
  放到 hover 卡片，减少每行信息密度；
- `DiffPane.tsx` 用 `@monaco-editor/react` 的 `DiffEditor` 做双栏只读 diff，标题为
  `文件 (旧sha) ↔ 文件 (新sha)`；
- `AppState.tsx` 增加 diff 标签、`ScmFocus`、`openDiff` / `focusScm` / `openRefCode`；
  切 revision 时关闭 file/diff 标签（它们是快照），保留 note/graph；
- `NoteReader.tsx` 的 `note.refs` 变为可点击列表，点击后按 §16 的规则打开最新文件或历史提交文件；
- 样式：`styles/scm.css`。


---

## 16. 笔记引用跳转（最新优先，缺失回历史）

**口径（用户确认）**

1. 引用在最新版本仍存在：打开**当前工作区文件**并高亮，行号按最新文件重算；
2. 引用在最新版本已删除或已改写：切到**笔记写作时的 commit**，打开该版本的整份文件并高亮旧行；
3. “仍存在”的判定 = **同一文件里能精确匹配原代码片段**（`code_hash` / `snippet`），不是只看文件是否存在。

**后端**

- `note_versions.resolve_refs_worktree(root, refs)`：
  1. 工作区同文件原范围 `code_hash`/`snippet` 命中 → `unchanged`；
  2. 同文件全文搜索命中 → `moved`，返回新行号；
  3. 工作区不命中、但 `commit_sha` 的 blob 在旧范围命中 → `historical`，`resolved_commit = commit_sha`；
  4. 两边都不命中：文件还在 → `modified`；文件也不在 → `deleted`。
- `GET /api/notes/{id}` 不带 `rev` 时走上面的工作区解析；带 `rev` 时仍在指定 commit 内解析。
- 测试：`tests/test_note_scope.py` 覆盖 unchanged / moved / historical / deleted / 两边都不命中。

**前端**

- `AppState.openRefCode` 打开 file 标签（不是 diff），并把 SCM 面板聚焦到对应 commit/file；
- 目标 revision 由引用状态决定：
  - `unchanged` / `moved` → 当前 scope（工作区为 `null`，历史视图为当前 `rev`）；
  - 其余 → `resolved_commit || commit_sha`；
- `NoteReader` 行内对当前引用显示「最新 / 当前版本」，历史引用显示短 sha；
- 跳转后编辑器用代码卡片同款 `frameDecorations` 画出整段范围框；
- 同一 scope 下重复点击不会关闭其它 file 标签；跨 scope 才重置 file/diff 标签。
