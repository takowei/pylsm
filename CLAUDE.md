# pylsm

一個從零手刻的 **LSM-tree 鍵值儲存引擎**（mini LevelDB/RocksDB 核心），作為作品集的「系統深度代表作」。設計與驗收標準見 `docs/BLUEPRINT.md`。

## 狀態（2026-06-27）

✅ **Phase 1 完成（親驗 14/14 測試綠）**：skiplist memtable + WAL（CRC framing）+ put/get/delete + 崩潰復原。
🚧 後續：Phase 2 SSTable flush → Phase 3 bloom filter → Phase 4 compaction + benchmark。

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
