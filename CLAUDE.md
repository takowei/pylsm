# pylsm

一個從零手刻的 **LSM-tree 鍵值儲存引擎**（mini LevelDB/RocksDB 核心），作為作品集的「系統深度代表作」。設計與驗收標準見 `docs/BLUEPRINT.md`。

## 狀態（2026-06-30）

✅ **Phase 1 完成（14/14 測試綠）**：skiplist memtable + WAL（CRC framing）+ put/get/delete + 崩潰復原。
✅ **Phase 2 完成（45/45 測試綠）**：SSTable（手刻格式：data blocks + sparse index + footer）+ memtable flush + multi-layer read + tombstone shadowing + crash-safe manifest。
✅ **Phase 3 完成（79/79 測試綠）**：手刻 Bloom filter（double-hashing, hashlib.sha256）+ SSTable 嵌入（footer 擴充至 28B）+ 讀路徑整合（bloom 拒絕則跳過 SSTable block 讀）。n=1000/p=0.01 實測 FPR=1.00%（符合理論值）。
✅ **Phase 4 完成（95/95 測試綠）**：Leveled compaction（heapq 多路歸併、L0→L1 非重疊、tombstone GC、崩潰安全 MANIFEST 原子更新）+ DBStats（WA/RA 定義式量測）+ benchmark harness（docs/PERFORMANCE.md 誠實數字）。
🚧 後續：Phase 5 打包（CLI + 效能報告整理）。

## 技術棧

Python 3.10+（純標準庫，無第三方執行期相依）、ruff lint、pytest。

## 目錄結構

```
pylsm/        ← 套件原始碼
  skiplist.py ← 有序 memtable（手刻 skiplist）
  wal.py      ← 預寫日誌（length-prefix + CRC32 framing，torn-tail 安全）
  db.py       ← KV API：put/get/delete + 開啟時 WAL 重放復原
tests/        ← pytest 測試（含 property oracle + 崩潰復原）
docs/         ← BLUEPRINT.md（設計藍圖 + 驗收閘）
```

## 常用指令

```bash
python -m pytest          # 跑測試
ruff check pylsm tests    # lint
ruff format pylsm tests   # 格式化
```

## 開發原則（驗收閘，見 BLUEPRINT）

每階段須過：正確性親驗（property 對照）、崩潰復原可證、效能數字不灌水、ruff 全綠、誠實標狀態、核心手刻。
