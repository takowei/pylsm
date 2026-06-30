# pylsm — 設計藍圖與驗收閘

> 一個從零手刻的 **LSM-tree 鍵值儲存引擎**（mini LevelDB/RocksDB 核心），用來展現系統底層的 CS 硬底子：
> 資料結構、磁碟 I/O、預寫日誌與崩潰復原、SSTable、Bloom filter、分層壓實、讀寫放大分析。
>
> 定位：作品集中的「系統深度代表作」。智識核心（skiplist、WAL framing、compaction、bloom）**全部手刻**，不套現成 KV 套件。

---

## 為什麼做這個

現有作品集（PriceWatch 全端、量化回測、靜態分析工具）能秀後端工程、統計嚴謹與資安，**但缺一個證明「我懂系統底層」的代表作**。LSM-tree 引擎是業界與學界公認最能展現這點的題目：它逼你正面處理「記憶體 vs 磁碟」「順序寫 vs 隨機讀」「持久性 vs 效能」的核心取捨。

---

## 架構總覽

```
        put/delete                         get
            │                               │
            ▼                               ▼
   ┌──────────────┐   滿了 flush    讀取順序：新 → 舊
   │  WAL (append) │ ──────────┐    memtable → immutable memtables
   └──────────────┘           │              → SSTable L0 → L1 → ...
            │                  ▼
   ┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
   │  memtable     │ ──▶ │ immutable     │ ──▶ │ SSTables (磁碟)    │
   │  (skiplist)   │     │ memtable      │     │ + bloom + 區塊索引 │
   └──────────────┘     └──────────────┘     └──────────────────┘
                                                       │ compaction
                                                       ▼ (分層合併)
```

- **寫入**：先 append WAL（保證持久性）→ 再寫 memtable（skiplist，記憶體內有序）。memtable 達門檻 → 轉為 immutable → flush 成 SSTable，WAL 隨之輪替。
- **讀取**：memtable → immutable memtable → SSTable 由新到舊；命中即回（含 tombstone 判定刪除）。
- **刪除**：寫入 tombstone 標記（LSM 不就地刪除），壓實時才真正回收。
- **崩潰復原**：開啟時重放 WAL 把未 flush 的寫入還原回 memtable。

---

## 分階段交付（每階段都要過驗收閘才算完成）

| Phase | 內容                                                                                | 狀態 |
| ----- | ----------------------------------------------------------------------------------- | ---- |
| **1** | skiplist memtable + WAL（length-prefix + CRC32 framing）+ put/get/delete + 崩潰復原 | 完成 |
| **2** | SSTable 落地（有序區塊 + 稀疏索引 + footer）+ memtable flush + 多層讀取合併         | 完成 |
| 3     | Bloom filter（降低不存在鍵的磁碟讀）+ 讀路徑整合                                    | 規劃 |
| 4     | Leveled compaction + 讀寫放大量測 + benchmark（throughput / 放大比）                | 規劃 |
| 5     | 打包：CLI、README、效能報告（誠實數字、附量測方法）                                 | 規劃 |

---

## 驗收閘（review gate — 取自 founder 決策標準，每階段逐條檢查）

一個階段「完成」必須**全部**通過，否則打回：

1. **正確性親驗，不靠宣稱**：核心邏輯有單元測試；隨機操作以 `dict` 為 oracle 做 **property-based 對照測試**（隨機 put/get/delete 序列，引擎結果必須逐一等於 oracle）。
2. **崩潰復原可證**：以「寫入 → 不正常關閉 → 重新開啟」測試證明已 ack 的寫入不遺失；WAL 尾端截斷（torn write）必須被 CRC 偵測並安全丟棄，不得讀到壞資料。
3. **效能數字不灌水**：任何 benchmark 都附「機器、資料量、量測方法」，標明是記憶體還是落盤；不可把記憶體數字講成磁碟效能。讀寫放大用定義式量測，不憑感覺。
4. **乾淨程式碼**：ruff format + lint 全綠；函式短、命名見名知意；不留死碼。
5. **誠實狀態**：README/藍圖只標已驗證的為「完成」；未做的標「規劃」；已知限制明列（caveat）。
6. **手刻核心**：skiplist、WAL framing、SSTable 格式、bloom、compaction 為自己實作，不引入現成嵌入式 KV。

---

## 非目標（範圍紀律，避免發散）

- 不做分散式/複寫/交易隔離（那是另一個題目）；先把單機儲存引擎做到正確且可量測。
- 不追求贏過 RocksDB 的絕對效能；目標是**展現對機制的掌握與誠實的量測**，不是造一個生產資料庫。
