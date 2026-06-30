# pylsm-db — 設計藍圖：MVCC + mini SQL 查詢引擎

> 這份文件是 `docs/BLUEPRINT.md`（LSM-tree KV 引擎五階段）的延伸。  
> 前五個 Phase 已完成（105/105 測試綠）。這裡規劃在 KV 之上疊 MVCC 多版本
> 與 mini SQL 查詢引擎，把 pylsm 升級成一個 mini relational database。
>
> **寫作原則**：每個 Phase 都有鎖死的範圍邊界與驗收閘；「非目標」比「目標」更重要。

---

## 架構總覽

```
┌─────────────────────────────────────────────────┐
│  Phase DB-3/DB-4：SQL 查詢引擎（tokenizer + parser + executor）   │
│  支援 CREATE TABLE / INSERT / SELECT WHERE / DELETE WHERE      │
├─────────────────────────────────────────────────┤
│  Phase DB-2：Table 目錄 + Row 編碼                              │
│  (table_name, pk) → KV key；row tuple → KV value               │
├─────────────────────────────────────────────────┤
│  Phase DB-1（本次）：MVCC 多版本 + Snapshot Isolation              │
│  encode(user_key, seq) → bytes KV key；snapshot 讀             │
├─────────────────────────────────────────────────┤
│  底層：pylsm KV 引擎（Phase 1-5，已完成）                         │
│  skiplist + WAL + SSTable + bloom + compaction                  │
└─────────────────────────────────────────────────┘
```

---

## SQL 語法子集（鎖死，不做擴充直到顯式修改此文件）

### 第一版支援（Phase DB-3 起）

```sql
CREATE TABLE t (col1 INTEGER, col2 TEXT, PRIMARY KEY (col1));

INSERT INTO t VALUES (1, 'hello');

SELECT col1, col2 FROM t WHERE col1 = 1;
SELECT * FROM t WHERE col2 = 'x' AND col1 > 0;

DELETE FROM t WHERE col1 = 1;
```

**型別**：`INTEGER`（Python int）、`TEXT`（Python str，UTF-8）。

**WHERE 條件**：

- 單欄比較：`col op literal`，op ∈ `= < > <= >= !=`
- 多條件：只支援 `AND`（不支援 `OR`、`NOT`）
- 字面值：整數、單引號字串

### 第一版**不做**（硬性非目標）

- JOIN（任何形式）
- 聚合（COUNT、SUM、AVG、MIN、MAX、GROUP BY）
- 子查詢
- ORDER BY、LIMIT、OFFSET
- NULL 值（欄位預設 NOT NULL，不支援 NULL 字面值）
- 索引（除主鍵外的二級索引）
- UPDATE 語句
- 事務 BEGIN/COMMIT/ROLLBACK（MVCC 提供隱式單操作原子性）
- ALTER TABLE、DROP TABLE
- 型別轉換、函式呼叫

> **範圍爆炸防護**：任何超出上列範圍的需求，必須明確修改此文件的「支援」區塊後才能實作。

---

## 分階段交付

| Phase | 內容                                          | 驗收閘 | 狀態     |
| ----- | --------------------------------------------- | ------ | -------- |
| DB-1  | MVCC 多版本 + Snapshot Isolation              | 見下方 | **完成** |
| DB-2  | Table 目錄 + Row 編碼（CREATE TABLE, INSERT） | 見下方 | 規劃     |
| DB-3  | Tokenizer + Parser（手刻 recursive-descent）  | 見下方 | 規劃     |
| DB-4  | 查詢執行器（SELECT / DELETE WHERE）           | 見下方 | 規劃     |

---

## Phase DB-1：MVCC 多版本 + Snapshot Isolation（已完成）

### 設計決策

**物理 key 編碼**

每個使用者寫入 `(user_key, seq)` 對應到一個唯一的底層 KV 物理 key：

```
physical_key = u32be(len(user_key)) | user_key | u64be(MAX_U64 - seq)
```

- `u32be` / `u64be`：big-endian，確保字典序與語義序一致
- `MAX_U64 - seq`（倒序）：相同 user_key 下，seq 越大 → 物理 key 越小 →
  在順向掃描中「最新版本排最前」
- 長度前綴 `u32be(len(user_key))`：避免不同長度 user_key 間的邊界模糊；
  支援任意二進位 user_key（含 `\x00` bytes）

**MVCC value 編碼**

```
live value  →  b'\x00' + value_bytes
tombstone   →  b'\x01'
```

底層 KV 的 `delete()` / tombstone 機制**完全不使用**；MVCC 刪除只寫一個 dead-tag 版本。

**Sequence 持久化**

全域序號 `_seq` 存入底層 KV 的特殊 meta key
`b"\xff\xff\xff\xff__mvcc_seq__"`（前 4 bytes decode 為 klen=4,294,967,295，
與任何合法 MVCC 物理 key 不衝突）。

寫入順序：先寫 meta key（WAL first），再寫資料 key，確保崩潰後 seq 恢復值
≥ 已提交資料的最大 seq，不會重複使用舊 seq。

**Snapshot Isolation 讀**

```
MVCC.get(user_key, snapshot_seq)
  1. start = encode(user_key, snapshot_seq)
  2. DB._scan_from(start) — 多層歸併掃描
  3. 取第一筆 prefix 符合 user_key 的條目
  4. live-tag → 回傳 value；dead-tag → 回傳 None；無條目 → 回傳 None
```

寫在 snapshot 之後（seq > snapshot_seq）的版本，其物理 key 比 start 小，
在順向掃描中**排在 start 之前，不會被掃到** → 自然隔離。

**DB.\_scan_from 實作**

多層歸併（heapq）：active memtable（優先級最高）→ L0 newest-first →
L1, L2, … 。相同物理 key 只取最新來源；KV tombstone 過濾掉。
時間複雜度：O(N log S)，N = 總條目數，S = 來源數（level 數量）。

**Compaction 相容性**

MVCC 版本全部是獨立物理 key，compaction 的「相同 key 只保留最新版本」
規則在 MVCC 物理 key 空間下等於「每個版本都保留」（各版本 key 不重複）。
版本 GC（移除過舊版本）是後續優化，Phase DB-1 保守保留所有版本。

**已知限制**

- 版本不做 GC：每次寫入都新增一個版本，長期執行會累積舊版本佔空間。
- 序號持久化：每次 MVCC 寫入額外呼叫一次底層 KV put()（寫 meta key），
  相當於 2× WAL 寫入；測試場景下可接受，生產需批次化。
- 無並發：與底層 KV 相同，single-threaded only。

### 驗收閘（DB-1）

| 閘  | 要求                                                                         | 狀態 |
| --- | ---------------------------------------------------------------------------- | ---- |
| 1   | 正確性：多版本讀寫、snapshot isolation、tombstone 跨版本，全有測試且綠燈     | ✅   |
| 2   | 崩潰復原：重開後 MVCC seq 正確恢復，舊 snapshot 仍可讀（seq counter 不倒退） | ✅   |
| 3   | 既有 105 個 KV 測試全部繼續通過（MVCC 不破壞底層 KV 語義）                   | ✅   |
| 4   | 乾淨程式碼：ruff format + lint 全綠；函式短、命名見名知意                    | ✅   |
| 5   | 誠實狀態：已驗證的標完成；已知限制（GC、seq 持久化開銷）明列                 | ✅   |
| 6   | 手刻核心：MVCC 編碼、snapshot、scan 歸併均自行實作，不引入額外套件           | ✅   |

---

## Phase DB-2：Table 目錄 + Row 編碼（規劃）

### 設計決策（草稿）

**Schema 儲存**

Table schema 存入 MVCC 下的特殊 key 空間（`__schema__/<table_name>`），
value 為 JSON-encoded 欄位定義（name, type, primary key 欄位名）。

**Row 編碼**

```
user_key  = "<table_name>/<pk_value_encoded>"
user_value = msgpack-or-json-encoded row dict
```

- `pk_value_encoded`：整數用 zero-padded 十進位（排序穩定）；文字 URL-encode 去 `/`
- Row value：Phase DB-2 先用 JSON（可讀、無外部依賴）；後續可換成更緊湊格式

**操作**

- `CREATE TABLE`：寫入 schema key（若已存在 → 報錯）
- `INSERT`：根據 schema 驗證型別，再寫 row KV

### 驗收閘（DB-2）

1. CREATE TABLE + INSERT + 原始 KV get() 可驗證 row 存在
2. 重複 CREATE TABLE 報錯，型別不符報錯
3. 所有 DB-1 測試繼續通過
4. ruff 全綠

---

## Phase DB-3：Tokenizer + Parser（規劃）

### 設計決策（草稿）

**Tokenizer**

Hand-written character-by-character tokenizer，token 類型：
`KEYWORD` / `IDENT` / `INTEGER_LIT` / `STRING_LIT` / `OP` / `LPAREN` /
`RPAREN` / `COMMA` / `SEMICOLON` / `EOF`

**Parser**

Recursive-descent parser，每個語法規則對應一個 `_parse_<rule>()` 方法。
回傳 AST（純 dataclass，無外部依賴）：

```python
@dataclasses.dataclass
class SelectStmt:
    columns: list[str]  # ["*"] or ["col1", "col2"]
    table: str
    where: Condition | None

@dataclasses.dataclass
class Condition:
    column: str
    op: str  # "=" | "<" | ">" | "<=" | ">=" | "!="
    literal: int | str
    and_next: "Condition | None"
```

**非目標（Parser 範圍）**

- 不支援 OR、NOT、括號分組 WHERE
- 不解析 JOIN、聚合、子查詢

### 驗收閘（DB-3）

1. 手刻 tokenizer：所有 token 類型有單元測試
2. Parser roundtrip：parse(sql) → AST；AST 正確反映語義
3. 錯誤 SQL 回傳清楚的錯誤訊息，不崩潰
4. 所有 DB-1/DB-2 測試繼續通過

---

## Phase DB-4：查詢執行器（規劃）

### 設計決策（草稿）

**SELECT 執行**

```
AST SelectStmt
  → executor._scan_table(table_name)        # MVCC scan
  → filter: eval_condition(row, where_ast)  # 純 Python
  → project: pick columns                   # list comprehension
  → return: list[dict]
```

**DELETE 執行**

```
AST DeleteStmt
  → executor._scan_table(table_name)
  → filter matching rows
  → MVCC.delete(row_key) for each
```

**Table scan**

透過 MVCC scan_from(`<table_name>/`) 掃描該 table 的所有 row。

### 驗收閘（DB-4）

1. SQL roundtrip：write via INSERT → read via SELECT → results match
2. DELETE WHERE 後 SELECT 不再回傳刪除的 row
3. Snapshot isolation 透過 SQL 可展示：SELECT 不受 snapshot 後的 INSERT 影響
4. 所有 DB-1/DB-2/DB-3 測試繼續通過

---

## 六條驗收閘（延伸自 BLUEPRINT.md）

每個 Phase 都必須通過這六條，否則打回：

1. **正確性親驗，不靠宣稱**：核心邏輯有單元測試；新語義有 property-based oracle。
2. **崩潰復原可證**：涉及持久化的 Phase 有「寫入 → 不正常關閉 → 重新開啟」測試。
3. **效能數字不灌水**：任何 benchmark 都附「機器、資料量、量測方法」，明標已知限制。
4. **乾淨程式碼**：ruff format + lint 全綠；函式短、命名見名知意；不留死碼。
5. **誠實狀態**：文件只標已驗證的為「完成」；已知限制明列。
6. **手刻核心**：tokenizer、parser、MVCC 編碼、scan 歸併均自行實作；不引入嵌入式 DB 或 parser 套件。
